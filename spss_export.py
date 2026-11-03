"""Export confirmed synthetic responses to SPSS without changing form automation."""

from __future__ import annotations

from datetime import date
import json
import math
import os
from pathlib import Path
import tempfile


class SpssExportError(ValueError):
    """A readable export error safe to show in the web interface."""


MAX_ROWS = 50_000
MAX_LOG_BYTES = 200 * 1024 * 1024
MAX_LINE_BYTES = 2 * 1024 * 1024
MAX_CELLS = 2_000_000
MAX_TEXT_BYTES = 32 * 1024 * 1024


def _label(value, limit=255):
    return str(value).replace("\x00", "").encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def export_codebook(schema):
    """Return the deterministic variable map used by :func:`export_spss`."""
    fields = schema.get("fields")
    if not isinstance(fields, list) or not fields:
        raise SpssExportError("Không có cấu trúc câu hỏi để xuất SPSS.")
    specs = [{"name": "case_id", "label": "Số lượt gửi đã được xác nhận", "field_id": None,
              "kind": "case_id", "measure": "scale", "value_labels": {}, "string": False}]
    field_ids = set()
    for number, field in enumerate(fields, 1):
        field_id = field.get("id")
        if not isinstance(field_id, str) or field_id in field_ids:
            raise SpssExportError("Cấu trúc câu hỏi bị thiếu hoặc trùng mã; hãy đọc lại form.")
        field_ids.add(field_id)
        kind = field.get("kind")
        title = str(field.get("question", field_id))
        if field.get("row"):
            title += " — " + str(field["row"])
        options = field.get("options") or []
        if not isinstance(options, list) or any(not isinstance(option, str) for option in options):
            raise SpssExportError("Danh sách đáp án không hợp lệ: " + title)
        if len(options) != len(set(options)):
            raise SpssExportError("Có đáp án trùng nhau nên không thể mã hóa SPSS: " + title)
        base = {"field_id": field_id, "kind": kind, "page": field.get("page", 0),
                "question": str(field.get("question", "")), "row": field.get("row"),
                "options": options, "string": False, "measure": "nominal"}
        name = f"q{number:03d}"
        if kind in ("multiple", "grid_multiple"):
            if not options:
                raise SpssExportError("Câu hỏi chọn nhiều không có đáp án: " + title)
            for index, option in enumerate(options, 1):
                specs.append({**base, "name": f"{name}_{index:02d}", "label": _label(title + " — " + option),
                              "option": option, "value_labels": {0: "Không chọn", 1: "Đã chọn"}})
        elif options:
            codes = list(range(1, len(options) + 1))
            if kind == "scale":
                try:
                    numeric = [float(option) for option in options]
                    if len(set(numeric)) == len(numeric) and all(math.isfinite(value) for value in numeric):
                        codes = numeric
                except ValueError:
                    pass
            specs.append({**base, "name": name, "label": _label(title),
                          "measure": "ordinal" if kind in ("scale", "grid") else "nominal",
                          "codes": dict(zip(options, codes)),
                          "value_labels": {code: _label(option, 120) for option, code in zip(options, codes)}})
        elif kind in ("text", "paragraph", "date", "time"):
            specs.append({**base, "name": name, "label": _label(title), "string": True, "value_labels": {}})
        else:
            raise SpssExportError("Chưa hỗ trợ mã hóa kiểu câu hỏi SPSS: " + title)
    if len(specs) > 10_000:
        raise SpssExportError("Biểu mẫu quá nhiều biến để xuất SPSS trên máy chủ này.")
    return specs


def _log_events(path, max_bytes):
    """Read only the initial file snapshot; tolerate a live writer's partial last line."""
    try:
        with Path(path).open("rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            if size > max_bytes:
                raise SpssExportError("Nhật ký vượt giới hạn xuất SPSS. Hãy chia thành các đợt nhỏ hơn.")
            while handle.tell() < size:
                remaining = size - handle.tell()
                raw = handle.readline(min(MAX_LINE_BYTES + 1, remaining))
                if len(raw) > MAX_LINE_BYTES:
                    raise SpssExportError("Một bản ghi nhật ký quá lớn để xuất SPSS.")
                try:
                    event = json.loads(raw.decode("utf-8-sig"))
                except (ValueError, UnicodeError) as exc:
                    if handle.tell() == size and not raw.endswith(b"\n"):
                        break
                    raise SpssExportError("Nhật ký bị lỗi; không thể xuất SPSS chính xác.") from exc
                if not isinstance(event, dict):
                    raise SpssExportError("Nhật ký không có định dạng bản ghi hợp lệ.")
                yield event
    except OSError as exc:
        raise SpssExportError("Chưa đọc được nhật ký kết quả để xuất SPSS.") from exc


def _temporal(value, kind):
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        raise SpssExportError("Giá trị ngày/giờ trong nhật ký không hợp lệ.")
    try:
        if kind == "date":
            if "year" in value:
                result = date(int(value["year"]), int(value["month"]), int(value["day"])).isoformat()
            else:
                date(2000, int(value["month"]), int(value["day"]))
                result = f'{int(value["month"]):02d}-{int(value["day"]):02d}'
            if "hour" not in value:
                return result
            return result + "T" + _temporal(value, "time")
        hour, minute, second = (int(value.get(part, 0)) for part in ("hour", "minute", "second"))
        if hour < 0 or not 0 <= minute < 60 or not 0 <= second < 60:
            raise ValueError("invalid time")
        return f"{hour:02d}:{minute:02d}:{second:02d}"
    except (ValueError, TypeError, KeyError) as exc:
        raise SpssExportError("Giá trị ngày/giờ trong nhật ký không hợp lệ.") from exc


def _row_values(specs, filled, index, branching):
    answers, report = filled.get("answers"), filled.get("report") or {}
    if not isinstance(answers, dict) or not isinstance(report, dict):
        raise SpssExportError("Nhật ký thiếu câu trả lời đã điền; không xuất dữ liệu phỏng đoán.")
    visited_fields = report.get("field_ids")
    visited_pages = report.get("page_indices")
    if visited_fields is not None and not isinstance(visited_fields, list):
        raise SpssExportError("Nhật ký thiếu danh sách câu hỏi thực tế đã điền.")
    if branching and visited_fields is None and not isinstance(visited_pages, list):
        raise SpssExportError("Nhật ký thiếu các trang đã đi qua nên chưa xuất được form phân nhánh.")
    values = [float(index)]
    for spec in specs[1:]:
        field_id = spec["field_id"]
        skipped = (field_id not in visited_fields if visited_fields is not None else
                   visited_pages is not None and spec["page"] not in visited_pages)
        value = answers.get(field_id)
        if skipped or value is None or value == "":
            values.append("" if spec["string"] else float("nan"))
        elif "option" in spec:
            if not isinstance(value, list) or any(option not in spec["options"] for option in value):
                raise SpssExportError("Câu trả lời chọn nhiều trong nhật ký không hợp lệ.")
            values.append(float(spec["option"] in value))
        elif "codes" in spec:
            if not isinstance(value, str) or value not in spec["codes"]:
                raise SpssExportError("Đáp án không khớp cấu trúc form: " + spec["question"])
            values.append(float(spec["codes"][value]))
        else:
            if spec["kind"] in ("date", "time"):
                text = _temporal(value, spec["kind"])
            elif isinstance(value, str):
                text = value
            else:
                raise SpssExportError("Câu trả lời văn bản trong nhật ký không hợp lệ.")
            if "\x00" in text or len(text.encode("utf-8")) > 32767:
                raise SpssExportError("Câu trả lời văn bản vượt giới hạn của file SPSS.")
            values.append(text)
    return values


def export_spss(schema, log_path, output_path, *, max_rows=MAX_ROWS, max_log_bytes=MAX_LOG_BYTES):
    """Write confirmed responses as UTF-8 SAV with labels and synthetic-data notes.

    The output file may already exist. An atomic replacement is made only after a
    complete successful write. Preview, uncertain and duplicate confirmations are
    excluded. Limits protect small cloud instances; they do not limit job size.
    """
    if type(max_rows) is not int or max_rows <= 0 or type(max_log_bytes) is not int or max_log_bytes <= 0:
        raise SpssExportError("Giới hạn xuất SPSS phải là số nguyên dương.")
    specs = export_codebook(schema)
    columns = {spec["name"]: [] for spec in specs}
    pending, seen, uncertain = {}, set(), set()
    text_bytes = 0
    for event in _log_events(log_path, max_log_bytes):
        kind, index = event.get("event"), event.get("index")
        if kind not in ("filled", "confirmed", "unconfirmed"):
            continue
        if type(index) is not int or index <= 0:
            raise SpssExportError("Nhật ký có số lượt không hợp lệ.")
        if kind == "filled":
            pending[index] = {"answers": event.get("answers"), "report": event.get("report")}
            if len(pending) > 32:
                raise SpssExportError("Nhật ký có quá nhiều lượt chưa xác nhận; hãy kiểm tra kết quả trước.")
        elif kind == "unconfirmed":
            if index in seen:
                raise SpssExportError("Nhật ký có kết quả xác nhận mâu thuẫn; hãy kiểm tra form trước.")
            pending.pop(index, None)
            uncertain.add(index)
        elif index not in seen and index not in uncertain:
            filled = pending.pop(index, None)
            if filled is None:
                raise SpssExportError("Nhật ký xác nhận thiếu câu trả lời tương ứng; chưa xuất được SPSS.")
            if len(seen) >= max_rows or (len(seen) + 1) * len(specs) > MAX_CELLS:
                raise SpssExportError("Dữ liệu vượt giới hạn xuất SPSS trên máy chủ. Hãy chia thành các đợt nhỏ hơn.")
            values = _row_values(specs, filled, index, bool(schema.get("branching")))
            text_bytes += sum(len(value.encode("utf-8")) for value in values if isinstance(value, str))
            if text_bytes > MAX_TEXT_BYTES:
                raise SpssExportError("Dữ liệu văn bản quá lớn để xuất SPSS trên máy chủ này.")
            for spec, value in zip(specs, values):
                columns[spec["name"]].append(value)
            seen.add(index)
        elif kind == "confirmed":
            pending.pop(index, None)
    if not seen:
        raise SpssExportError("Chưa có lượt gửi được xác nhận. Điền thử và lượt chưa rõ kết quả không xuất thành dữ liệu SPSS.")
    try:
        import pandas as pd
        import pyreadstat
    except ImportError as exc:
        raise SpssExportError("Chưa có thư viện SPSS. Chạy: python -m pip install -r requirements.txt") from exc
    frame = pd.DataFrame(columns)
    for spec in specs:
        if spec["string"]:
            frame[spec["name"]] = frame[spec["name"]].astype(object)
    target = Path(output_path)
    temporary = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=".kdautoform-spss-", suffix=".sav", dir=target.parent)
        os.close(descriptor)
        temporary = Path(name)
        pyreadstat.write_sav(
            frame, str(temporary), file_label="KDautoform - synthetic form responses",
            column_labels={spec["name"]: spec["label"] for spec in specs},
            variable_value_labels={spec["name"]: spec["value_labels"] for spec in specs if spec["value_labels"]},
            variable_measure={spec["name"]: spec["measure"] for spec in specs},
            note=["Dữ liệu mô phỏng do KDautoform tạo.",
                  "Synthetic responses; not answers from real survey participants.",
                  "Confirmed submissions only; skipped questions are missing.",
                  "Form provider: " + _label(schema.get("provider", "google"), 50),
                  "Multiple choice: 0/1 columns. Dates and times: ISO strings."],
            row_compress=True,
        )
        os.replace(temporary, target)
        return target
    except Exception as exc:
        raise SpssExportError("Không ghi được file SPSS. Hãy thử đợt nhỏ hơn hoặc kiểm tra dung lượng máy chủ.") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

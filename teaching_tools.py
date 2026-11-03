"""Small, transparent helpers for teaching with synthetic survey data.

Presets change response probabilities only. Descriptive summaries describe the
confirmed synthetic submissions; they do not promise reliability, significance,
or results that can replace a real research sample.
"""
from __future__ import annotations

import copy
import csv
from datetime import datetime, timezone
import io
import json
import math
import os
from pathlib import Path
import re
import tempfile
import unicodedata
import zipfile

from spss_export import (
    MAX_CELLS, MAX_LOG_BYTES, MAX_ROWS, MAX_TEXT_BYTES,
    SpssExportError, _log_events, _row_values, export_codebook,
)


class TeachingToolsError(ValueError):
    """A readable error for the educational tools."""


def _fold(value):
    value = str(value).lower().replace("đ", "d")
    return " ".join(unicodedata.normalize("NFD", value).encode("ascii", "ignore").decode().split())


def _demographic(field):
    # A statement about a school/lecturer is still an attitude item if its
    # answers explicitly use agreement levels.
    if any("dong y" in _fold(option) or re.search(r"\b(?:agree|disagree)\b", _fold(option))
           for option in field.get("options") or []):
        return False
    text = _fold(field.get("question", "") + " " + str(field.get("row") or "")).replace("gioi tinh", "gender")
    return bool(re.search(
        r"\b(gender|sex|age|full name|first name|last name|date of birth|birth year|school|university|faculty|major|income|salary|occupation|student year|"
        r"ho va ten|ho ten|nam sinh|ngay sinh|noi sinh|tuoi|truong|chuyen nganh|khoa|nam hoc|sinh vien nam|thu nhap|nghe nghiep|dia chi)\b", text))


def _ordered_scores(field):
    """Recognize actual scale values or explicit agreement labels, never infer categories."""
    options = field.get("options") or []
    if len(options) < 2:
        return None
    if field.get("kind") in ("scale", "grid"):
        try:
            numbers = [float(value) for value in options]
        except (ValueError, TypeError):
            pass
        else:
            if all(math.isfinite(value) for value in numbers) and len(set(numbers)) == len(numbers):
                return numbers
    scores = []
    for option in options:
        label = _fold(option)
        if any(term in label for term in ("trung lap", "binh thuong", "phan van", "neutral", "neither agree")):
            score = 3
        elif "khong dong y" in label or "disagree" in label:
            score = 1 if "hoan toan" in label or "rat khong" in label or "strongly" in label else 2
        elif "dong y" in label or re.search(r"\bagree\b", label):
            score = 5 if "hoan toan" in label or "rat dong y" in label or "strongly" in label else 4
        else:
            return None
        scores.append(score)
    return scores if len(set(scores)) == len(scores) else None


def _apportion(weights):
    total = sum(weights)
    ideal = [weight * 100 / total for weight in weights]
    values = [math.floor(value) for value in ideal]
    order = sorted(range(len(values)), key=lambda index: ideal[index] - values[index], reverse=True)
    for index in order[:100 - sum(values)]:
        values[index] += 1
    return values


def ratio_preset(schema, config, preset):
    """Copy a configuration and apply an interpretable probability preset.

    ``balanced`` balances supported categorical items. ``bell`` and ``positive``
    apply only to recognized agreement/scale items, ordered by their meaning.
    Demographics, multiselect, text settings and profile settings are preserved.
    The caller should still run its normal configuration validator afterwards.
    """
    preset = {"neutral": "bell", "mildly_positive": "positive"}.get(preset, preset)
    if preset not in ("balanced", "bell", "positive"):
        raise TeachingToolsError("Mẫu phân bổ không hợp lệ. Chọn cân bằng, trung lập hoặc đồng ý nhẹ.")
    if not isinstance(config, dict) or not isinstance(schema, dict) or not isinstance(schema.get("fields"), list):
        raise TeachingToolsError("Chưa có cấu hình câu hỏi hợp lệ để áp dụng mẫu phân bổ.")
    result = copy.deepcopy(config)
    for field in schema["fields"]:
        options = field.get("options") or []
        if (not options or field.get("kind") in ("multiple", "grid_multiple")
                or _demographic(field) or field.get("id") not in result):
            continue
        scores = _ordered_scores(field)
        if preset == "balanced":
            weights = [1] * len(options)
        elif scores is not None:
            low, high = min(scores), max(scores)
            if not math.isfinite(high - low):
                raise TeachingToolsError("Thang điểm vượt phạm vi số có thể tính toán.")
            center = .5 if preset == "bell" else .65
            weights = [math.exp(-.5 * (((score - low) / (high - low) - center) / .29) ** 2)
                       for score in scores]
        else:
            continue
        values = _apportion(weights)
        result[field["id"]]["rates"] = dict(zip(options, values))
    return result


def _integer(value, label):
    if type(value) is not int or value < 0:
        raise TeachingToolsError("Số đếm thống kê không hợp lệ: " + label)
    return value


def summary_report(schema, job_snapshot):
    """Return bounded descriptive statistics without names or individual answers.

    ``counts`` must contain confirmed-response option frequencies. Pass
    ``answered_counts`` for exact missing counts, especially for multiselect.
    Standard deviation uses the sample denominator n-1. Ordinal averages are
    descriptive summaries of the displayed coding, not an inferential result.
    """
    confirmed = _integer(job_snapshot.get("confirmed", 0), "phản hồi đã xác nhận")
    if job_snapshot.get("mode") == "check":
        confirmed = 0
        counts, answered = {}, {}
    else:
        counts, answered = job_snapshot.get("counts") or {}, job_snapshot.get("answered_counts") or {}
    if not isinstance(counts, dict) or not isinstance(answered, dict):
        raise TeachingToolsError("Không đọc được số đếm của các câu hỏi.")
    result = {"synthetic": True, "confirmed": confirmed,
              "status": job_snapshot.get("status"), "standard_deviation": "sample_n_minus_1",
              "description": "Thống kê mô tả dữ liệu mô phỏng đã được xác nhận; không phải suy luận từ mẫu người tham gia thật.",
              "fields": []}
    for field in schema.get("fields", []):
        options = field.get("options") or []
        if not options:
            continue
        identifier = field["id"]
        raw = counts.get(identifier) or {}
        if not isinstance(raw, dict) or any(option not in options for option in raw):
            raise TeachingToolsError("Số đếm chứa đáp án không thuộc câu hỏi: " + field.get("question", identifier))
        values = {option: _integer(raw.get(option, 0), option) for option in options}
        if any(value > confirmed for value in values.values()):
            raise TeachingToolsError("Số đếm đáp án vượt số phản hồi đã xác nhận.")
        multi = field.get("kind") in ("multiple", "grid_multiple")
        valid_n = (_integer(answered[identifier], identifier) if identifier in answered else
                   None if multi else sum(values.values()))
        if valid_n is not None and (valid_n > confirmed or (not multi and valid_n != sum(values.values()))
                                    or any(value > valid_n for value in values.values())):
            raise TeachingToolsError("Số câu đã trả lời không khớp tần suất đáp án.")
        denominator = valid_n if valid_n is not None else confirmed
        item = {"id": identifier, "question": field.get("question", ""), "row": field.get("row"),
                "kind": field.get("kind"), "valid_n": valid_n,
                "missing": confirmed - valid_n if valid_n is not None else None,
                "percentage_base": "answered" if valid_n is not None else "confirmed",
                "frequencies": [{"option": option, "count": count,
                                 "percent": round(count / denominator * 100, 4) if denominator else 0}
                                for option, count in values.items()],
                "mean": None, "stddev": None, "score_map": None}
        ordered = None if multi else _ordered_scores(field)
        if ordered is not None:
            # Use the exact exported coding for descriptive means. Presets still
            # use semantic ordering, but reports must agree with SAV/CSV even
            # when the form lists positive answers before negative answers.
            codes = export_codebook({"fields": [field]})[1]["codes"]
            scores = [codes[option] for option in options]
            item["score_map"] = codes
        else:
            scores = None
        if scores is not None and valid_n:
            try:
                mean = sum(score * (count / valid_n) for score, count in zip(scores, values.values()))
                variance = (sum(count * (score - mean) ** 2 for score, count in zip(scores, values.values())) / (valid_n - 1)
                            if valid_n > 1 else None)
                item.update(mean=round(mean, 6) if math.isfinite(mean) else None,
                            stddev=round(math.sqrt(variance), 6) if variance is not None and math.isfinite(variance) else None)
            except ArithmeticError:
                pass
        result["fields"].append(item)
    return result


def _csv_cell(value):
    if isinstance(value, float):
        if math.isnan(value):
            return ""
        return int(value) if value.is_integer() else value
    if isinstance(value, str) and value.lstrip(" \t\r\n").startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def _csv_writer(archive, name):
    return io.TextIOWrapper(archive.open(name, "w"), encoding="utf-8-sig", newline="")


def export_csv_bundle(schema, log_path, output_path, *, max_rows=MAX_ROWS, max_log_bytes=MAX_LOG_BYTES):
    """Stream confirmed coded rows + codebook + descriptive report into a ZIP.

    CSV coding matches the SPSS exporter, including missing branch values. The
    output is atomically replaced only after successful completion. Formula-like
    text is prefixed with an apostrophe for opening the CSV safely in Excel.
    No pandas/pyreadstat import or extra in-memory dataset is needed.
    """
    if type(max_rows) is not int or max_rows <= 0 or type(max_log_bytes) is not int or max_log_bytes <= 0:
        raise TeachingToolsError("Giới hạn xuất dữ liệu phải là số nguyên dương.")
    target, temporary = Path(output_path), None
    try:
        specs = export_codebook(schema)
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=".kdautoform-csv-", suffix=".zip", dir=target.parent)
        os.close(descriptor)
        temporary = Path(name)
        pending, seen, uncertain, counts, answered = {}, set(), set(), {}, {}
        text_bytes, header = 0, {}
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=3) as archive:
            with _csv_writer(archive, "responses.csv") as handle:
                writer = csv.writer(handle)
                writer.writerow([spec["name"] for spec in specs])
                for event in _log_events(log_path, max_log_bytes):
                    kind, index = event.get("event"), event.get("index")
                    if kind == "run" and not header:
                        header = {key: event[key] for key in ("seed", "schedule", "provider", "count", "mode", "reference_date", "engine") if key in event}
                        continue
                    if kind not in ("filled", "confirmed", "unconfirmed"):
                        continue
                    if type(index) is not int or index <= 0:
                        raise TeachingToolsError("Nhật ký có số lượt không hợp lệ.")
                    if kind == "filled":
                        pending[index] = {"answers": event.get("answers"), "report": event.get("report")}
                        if len(pending) > 32:
                            raise TeachingToolsError("Nhật ký có quá nhiều lượt chưa xác nhận.")
                    elif kind == "unconfirmed":
                        if index in seen:
                            raise TeachingToolsError("Nhật ký có kết quả xác nhận mâu thuẫn; hãy kiểm tra form trước.")
                        pending.pop(index, None)
                        uncertain.add(index)
                    elif index not in seen and index not in uncertain:
                        filled = pending.pop(index, None)
                        if filled is None:
                            raise TeachingToolsError("Thiếu câu trả lời tương ứng với lượt đã xác nhận.")
                        if len(seen) >= max_rows or (len(seen) + 1) * len(specs) > MAX_CELLS:
                            raise TeachingToolsError("Dữ liệu vượt giới hạn xuất CSV; hãy chia thành đợt nhỏ hơn.")
                        values = _row_values(specs, filled, index, bool(schema.get("branching")))
                        text_bytes += sum(len(value.encode("utf-8")) for value in values if isinstance(value, str))
                        if text_bytes > MAX_TEXT_BYTES:
                            raise TeachingToolsError("Dữ liệu văn bản quá lớn để xuất CSV trên máy chủ này.")
                        writer.writerow([_csv_cell(value) for value in values])
                        seen.add(index)
                        answers, report = filled["answers"], filled.get("report") or {}
                        visited_fields, visited_pages = report.get("field_ids"), report.get("page_indices")
                        for field in schema["fields"]:
                            identifier = field["id"]
                            skipped = (identifier not in visited_fields if visited_fields is not None else
                                       visited_pages is not None and field.get("page", 0) not in visited_pages)
                            value = answers.get(identifier)
                            if skipped or value in (None, ""):
                                continue
                            answered[identifier] = answered.get(identifier, 0) + 1
                            if field.get("options"):
                                frequencies = counts.setdefault(identifier, {})
                                for option in value if isinstance(value, list) else [value]:
                                    frequencies[option] = frequencies.get(option, 0) + 1
                    elif kind == "confirmed":
                        pending.pop(index, None)
            if not seen:
                raise TeachingToolsError("Chưa có phản hồi được xác nhận để xuất CSV. Điền thử không tạo dữ liệu đã gửi.")
            with _csv_writer(archive, "codebook.csv") as handle:
                writer = csv.writer(handle)
                writer.writerow(["variable", "label", "field_id", "kind", "measure", "value_labels"])
                for spec in specs:
                    writer.writerow([_csv_cell(value) for value in (
                        spec["name"], spec["label"], spec.get("field_id") or "", spec["kind"], spec["measure"],
                        json.dumps(spec["value_labels"], ensure_ascii=False))])
            snapshot = {"confirmed": len(seen), "counts": counts, "answered_counts": answered, "status": "snapshot", "mode": "send"}
            archive.writestr("summary.json", json.dumps(summary_report(schema, snapshot), ensure_ascii=False, indent=2))
            metadata = {"synthetic": True, "confirmed": len(seen), "provider": schema.get("provider", "google"),
                        "title": schema.get("title", ""), "created_at": datetime.now(timezone.utc).isoformat(),
                        "source": header, "formula_text_escaped": True,
                        "description": "Dữ liệu mô phỏng do KDautoform tạo; chỉ bao gồm phản hồi đã xác nhận."}
            archive.writestr("metadata.json", json.dumps(metadata, ensure_ascii=False, indent=2))
            archive.writestr("README.txt", "KDautoform — dữ liệu mô phỏng để kiểm thử và giảng dạy.\n"
                            "responses.csv: mỗi hàng là một lượt đã xác nhận; mã hóa giống SPSS.\n"
                            "codebook.csv: nhãn biến, nhãn giá trị. summary.json: thống kê mô tả.\n"
                            "Câu bị bỏ qua theo nhánh được để thiếu. CSV dùng UTF-8 BOM.\n"
                            "Văn bản bắt đầu bằng ký tự công thức được thêm dấu nháy đơn khi mở trong Excel.\n")
        os.replace(temporary, target)
        return target
    except SpssExportError as exc:
        raise TeachingToolsError(str(exc)) from exc
    except (OSError, zipfile.BadZipFile) as exc:
        raise TeachingToolsError("Không ghi được gói CSV. Kiểm tra dung lượng và đường dẫn lưu.") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

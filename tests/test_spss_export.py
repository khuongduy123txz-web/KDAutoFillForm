import importlib.util
import json
import math
from pathlib import Path
import tempfile
import unittest

from spss_export import SpssExportError, export_codebook, export_spss


SPSS_AVAILABLE = importlib.util.find_spec("pyreadstat") is not None and importlib.util.find_spec("pandas") is not None


class SpssExportTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.log = Path(self.folder.name) / "job.jsonl"
        self.output = Path(self.folder.name) / "responses.sav"
        self.schema = {"provider": "google", "branching": True, "fields": [
            {"id": "entry.1", "question": "Họ và tên", "kind": "text", "options": [], "page": 0},
            {"id": "entry.2", "question": "Bạn có đồng ý?", "kind": "single", "options": ["Không", "Có"], "page": 0},
            {"id": "entry.3", "question": "Công cụ đã dùng", "kind": "multiple", "options": ["Excel", "SPSS"], "page": 0},
            {"id": "entry.4", "question": "Đánh giá", "row": "Mức độ hữu ích", "kind": "grid", "options": ["Thấp", "Cao"], "page": 1},
            {"id": "entry.5", "question": "Ngày sinh", "kind": "date", "options": [], "page": 0},
            {"id": "entry.6", "question": "Đánh giá từ 0 đến 2", "kind": "scale", "options": ["0", "1", "2"], "page": 0},
        ]}

    def filled(self, index=1, pages=None, field_ids=None):
        report = {"page_indices": [0] if pages is None else pages}
        if field_ids is not None:
            report["field_ids"] = field_ids
        return {"event": "filled", "index": index, "report": report, "answers": {
            "entry.1": "Nguyễn Thị Mai", "entry.2": "Có", "entry.3": ["SPSS"],
            "entry.4": "Cao", "entry.5": {"year": 2001, "month": 2, "day": 3}, "entry.6": "0"}}

    def write_log(self, *events):
        self.log.write_text("".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events), encoding="utf-8")

    @unittest.skipUnless(SPSS_AVAILABLE, "Install requirements.txt for SPSS round-trip tests")
    def test_roundtrip_unicode_labels_codes_multiselect_and_skipped_branch(self):
        import pyreadstat
        self.write_log({"event": "run", "synthetic": True}, self.filled(), {"event": "confirmed", "index": 1})
        self.output.touch()
        self.assertEqual(export_spss(self.schema, self.log, self.output), self.output)
        frame, meta = pyreadstat.read_sav(self.output)
        row = frame.iloc[0]
        self.assertEqual(row["q001"], "Nguyễn Thị Mai")
        self.assertEqual(row["q002"], 2)
        self.assertEqual((row["q003_01"], row["q003_02"]), (0, 1))
        self.assertTrue(math.isnan(row["q004"]))
        self.assertEqual(row["q005"], "2001-02-03")
        self.assertEqual(row["q006"], 0)
        self.assertEqual(meta.column_names_to_labels["q004"], "Đánh giá — Mức độ hữu ích")
        self.assertEqual(meta.variable_value_labels["q002"], {1.0: "Không", 2.0: "Có"})
        self.assertEqual(meta.variable_measure["q006"], "ordinal")
        self.assertTrue(any("mô phỏng" in note for note in meta.notes))

    @unittest.skipUnless(SPSS_AVAILABLE, "Install requirements.txt for SPSS round-trip tests")
    def test_exact_visited_fields_override_page_for_microsoft_branch(self):
        import pyreadstat
        self.schema["provider"] = "microsoft"
        self.write_log(self.filled(pages=[0, 1], field_ids=["entry.1", "entry.3", "entry.6"]),
                       {"event": "confirmed", "index": 1})
        export_spss(self.schema, self.log, self.output)
        frame, _ = pyreadstat.read_sav(self.output)
        self.assertTrue(math.isnan(frame.iloc[0]["q002"]))
        self.assertTrue(math.isnan(frame.iloc[0]["q004"]))
        self.assertEqual(frame.iloc[0]["q005"], "")

    @unittest.skipUnless(SPSS_AVAILABLE, "Install requirements.txt for SPSS round-trip tests")
    def test_only_confirmed_once_and_live_partial_last_line_is_ignored(self):
        import pyreadstat
        self.write_log(self.filled(1), {"event": "confirmed", "index": 1},
                       {"event": "confirmed", "index": 1}, self.filled(2),
                       {"event": "unconfirmed", "index": 2}, self.filled(3))
        with self.log.open("ab") as handle:
            handle.write(b'{"event":"conf')
        export_spss(self.schema, self.log, self.output)
        frame, _ = pyreadstat.read_sav(self.output)
        self.assertEqual(len(frame), 1)
        self.assertEqual(frame["case_id"].tolist(), [1])

    def test_preview_or_only_uncertain_responses_cannot_export(self):
        for events in ((self.filled(),), (self.filled(), {"event": "unconfirmed", "index": 1})):
            with self.subTest(events=events):
                self.write_log(*events)
                with self.assertRaisesRegex(SpssExportError, "Chưa có lượt gửi"):
                    export_spss(self.schema, self.log, self.output)
                self.assertFalse(self.output.exists())

    def test_unknown_option_is_rejected_instead_of_wrong_codes(self):
        for field, value in (("entry.2", "Khác"), ("entry.3", ["Không tồn tại"])):
            with self.subTest(field=field):
                event = self.filled()
                event["answers"][field] = value
                self.write_log(event, {"event": "confirmed", "index": 1})
                with self.assertRaises(SpssExportError):
                    export_spss(self.schema, self.log, self.output)

    def test_missing_branch_report_or_confirmation_answers_is_rejected(self):
        event = self.filled()
        event["report"] = {}
        self.write_log(event, {"event": "confirmed", "index": 1})
        with self.assertRaisesRegex(SpssExportError, "phân nhánh"):
            export_spss(self.schema, self.log, self.output)
        self.write_log({"event": "confirmed", "index": 1})
        with self.assertRaisesRegex(SpssExportError, "thiếu câu trả lời"):
            export_spss(self.schema, self.log, self.output)

    def test_size_row_and_corrupt_log_guards_preserve_existing_target(self):
        self.output.write_bytes(b"existing output")
        self.write_log(self.filled(1), {"event": "confirmed", "index": 1},
                       self.filled(2), {"event": "confirmed", "index": 2})
        with self.assertRaisesRegex(SpssExportError, "giới hạn"):
            export_spss(self.schema, self.log, self.output, max_rows=1)
        with self.assertRaisesRegex(SpssExportError, "giới hạn"):
            export_spss(self.schema, self.log, self.output, max_log_bytes=10)
        self.log.write_bytes(b"not json\n")
        with self.assertRaisesRegex(SpssExportError, "Nhật ký bị lỗi"):
            export_spss(self.schema, self.log, self.output)
        self.assertEqual(self.output.read_bytes(), b"existing output")

    def test_variable_names_are_unique_ascii_and_labels_keep_grid_rows(self):
        specs = export_codebook(self.schema)
        names = [spec["name"] for spec in specs]
        self.assertEqual(len(names), len(set(names)))
        for name in names:
            self.assertRegex(name, r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
        self.assertEqual(specs[5]["label"], "Đánh giá — Mức độ hữu ích")


if __name__ == "__main__":
    unittest.main()

import copy
import csv
import io
import json
import math
from pathlib import Path
import tempfile
import unittest
import zipfile

from teaching_tools import TeachingToolsError, export_csv_bundle, ratio_preset, summary_report


LABELS = ["Hoàn toàn đồng ý", "Đồng ý", "Trung lập", "Không đồng ý", "Hoàn toàn không đồng ý"]


def schema_fixture():
    return {"provider": "google", "title": "Bài thực hành thống kê", "branching": True, "fields": [
        {"id": "name", "question": "Họ và tên", "kind": "text", "options": [], "page": 0},
        {"id": "gender", "question": "Giới tính", "kind": "single", "options": ["Nam", "Nữ"], "page": 0},
        {"id": "agree", "question": "Tôi thấy công cụ hữu ích", "kind": "single", "options": LABELS[:], "page": 0},
        {"id": "tools", "question": "Công cụ đã dùng", "kind": "multiple", "options": ["Excel", "SPSS"], "page": 1},
        {"id": "score", "question": "Điểm đánh giá", "kind": "scale", "options": [str(i) for i in range(11)], "page": 1},
        {"id": "color", "question": "Màu yêu thích", "kind": "dropdown", "options": ["Đỏ", "Xanh", "Vàng"], "page": 0},
        {"id": "comment", "question": "Ý kiến thêm", "kind": "paragraph", "options": [], "page": 0},
    ]}


class RatioPresetTests(unittest.TestCase):
    def setUp(self):
        self.schema = schema_fixture()
        self.config = {"name": {"mode": "auto", "value": ""},
                       "gender": {"rates": {"Nam": 99, "Nữ": 1}},
                       "agree": {"rates": dict(zip(LABELS, [20] * 5))},
                       "tools": {"rates": {"Excel": 90, "SPSS": 60}},
                       "score": {"rates": {str(i): 10 if i < 10 else 0 for i in range(11)}},
                       "color": {"rates": {"Đỏ": 80, "Xanh": 10, "Vàng": 10}},
                       "comment": {"mode": "fixed", "value": "Nội dung giảng viên đặt sẵn", "profile": {"age": 22}}}

    def test_presets_copy_and_preserve_demographics_multiselect_and_text(self):
        original = copy.deepcopy(self.config)
        for preset in ("balanced", "bell", "positive"):
            result = ratio_preset(self.schema, self.config, preset)
            for key in ("name", "gender", "tools", "comment"):
                self.assertEqual(result[key], original[key])
            self.assertEqual(sum(result["agree"]["rates"].values()), 100)
            self.assertEqual(sum(result["score"]["rates"].values()), 100)
            result["comment"]["profile"]["age"] = 99
        self.assertEqual(self.config, original)

    def test_bell_and_positive_follow_semantics_not_visual_option_order(self):
        bell = ratio_preset(self.schema, self.config, "bell")["agree"]["rates"]
        self.assertEqual(max(bell, key=bell.get), "Trung lập")
        positive = ratio_preset(self.schema, self.config, "positive")["agree"]["rates"]
        mean = sum(positive[label] * score for label, score in zip(LABELS, [5, 4, 3, 2, 1])) / 100
        self.assertGreater(mean, 3.1)
        self.assertLess(mean, 4)
        self.assertEqual(ratio_preset(self.schema, self.config, "positive")["color"], self.config["color"])
        balanced = ratio_preset(self.schema, self.config, "balanced")["color"]["rates"]
        self.assertEqual(sorted(balanced.values()), [33, 33, 34])

    def test_school_attitude_is_not_mistaken_for_school_demographics(self):
        self.schema["fields"][2]["question"] = "Trường cung cấp hỗ trợ học tập đầy đủ"
        result = ratio_preset(self.schema, self.config, "positive")
        self.assertNotEqual(result["agree"], self.config["agree"])

    def test_invalid_preset_is_explicit(self):
        with self.assertRaises(TeachingToolsError):
            ratio_preset(self.schema, self.config, "guarantee_significance")


class SummaryTests(unittest.TestCase):
    def test_sample_statistics_and_missing_counts_use_actual_confirmed_rows(self):
        schema = schema_fixture()
        snapshot = {"mode": "send", "confirmed": 4, "status": "completed",
                    "counts": {"agree": {"Đồng ý": 2, "Trung lập": 1}, "tools": {"Excel": 2, "SPSS": 1}},
                    "answered_counts": {"agree": 3, "tools": 2}}
        report = summary_report(schema, snapshot)
        self.assertTrue(report["synthetic"])
        agreement = next(item for item in report["fields"] if item["id"] == "agree")
        self.assertEqual((agreement["valid_n"], agreement["missing"]), (3, 1))
        self.assertAlmostEqual(agreement["mean"], 7 / 3, places=5)
        self.assertAlmostEqual(agreement["stddev"], math.sqrt(1 / 3), places=5)
        multi = next(item for item in report["fields"] if item["id"] == "tools")
        self.assertEqual((multi["valid_n"], multi["missing"]), (2, 2))
        self.assertEqual([row["percent"] for row in multi["frequencies"]], [100, 50])
        self.assertIsNone(multi["mean"])
        self.assertNotIn("name", [item["id"] for item in report["fields"]])
        json.dumps(report, allow_nan=False)

    def test_reversed_agreement_report_uses_the_same_codes_as_sav_and_csv(self):
        from spss_export import export_codebook
        schema = schema_fixture()
        report = summary_report(schema, {"confirmed": 3, "counts": {"agree": {"Đồng ý": 3}}, "answered_counts": {"agree": 3}})
        agreement = next(item for item in report["fields"] if item["id"] == "agree")
        sav_codes = next(item["codes"] for item in export_codebook(schema) if item.get("field_id") == "agree")
        self.assertEqual(agreement["score_map"], sav_codes)
        self.assertEqual(agreement["mean"], sav_codes["Đồng ý"])
        self.assertEqual(agreement["mean"], 2)
        self.assertEqual(agreement["stddev"], 0)

    def test_preview_is_not_reported_as_real_data_and_unknown_multi_n_is_not_guessed(self):
        schema = schema_fixture()
        preview = summary_report(schema, {"mode": "check", "confirmed": 1, "counts": {"agree": {"Đồng ý": 1}}})
        self.assertEqual(preview["confirmed"], 0)
        self.assertTrue(all(item["mean"] is None for item in preview["fields"]))
        report = summary_report(schema, {"confirmed": 4, "counts": {"tools": {"Excel": 2, "SPSS": 1}}})
        multi = next(item for item in report["fields"] if item["id"] == "tools")
        self.assertIsNone(multi["valid_n"])
        self.assertIsNone(multi["missing"])
        self.assertEqual(multi["percentage_base"], "confirmed")

    def test_mismatched_counts_and_nonfinite_statistics_do_not_silently_publish_bad_data(self):
        with self.assertRaises(TeachingToolsError):
            summary_report(schema_fixture(), {"confirmed": 1, "counts": {"agree": {"Đồng ý": 2}}})
        schema = {"fields": [{"id": "extreme", "kind": "scale", "question": "Extreme", "options": ["-1e308", "1e308"]}]}
        result = summary_report(schema, {"confirmed": 2, "counts": {"extreme": {"-1e308": 1, "1e308": 1}}})
        json.dumps(result, allow_nan=False)
        self.assertIsNone(result["fields"][0]["stddev"])


class CsvBundleTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.schema = schema_fixture()
        self.log = Path(self.folder.name) / "job.jsonl"
        self.target = Path(self.folder.name) / "responses.zip"

    def filled(self, index):
        visited = ["name", "gender", "agree", "color", "comment"]
        if index % 2:
            visited += ["tools", "score"]
        return {"event": "filled", "index": index,
                "report": {"field_ids": visited},
                "answers": {"name": "=2+3" if index == 1 else "Nguyễn Thị Mai", "gender": "Nữ",
                            "agree": "Đồng ý", "tools": ["SPSS"], "score": str(index % 11),
                            "color": "Đỏ", "comment": "  +SUM(A1:A2)"}}

    def write(self, events, tail=""):
        self.log.write_text("".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events) + tail, encoding="utf-8")

    def test_streams_200_confirmed_rows_with_labels_missing_branch_and_safe_excel_text(self):
        events = [{"event": "run", "seed": 42, "sampler_version": "web-2",
                   "schedule": {"mode": "random", "min_seconds": 30, "max_seconds": 180}}]
        for index in range(1, 201):
            events += [self.filled(index), {"event": "confirmed", "index": index}]
        events += [self.filled(201), {"event": "unconfirmed", "index": 201}, self.filled(202),
                   {"event": "confirmed", "index": 200}]
        self.write(events, '{"event":"partial')
        self.assertEqual(export_csv_bundle(self.schema, self.log, self.target), self.target)
        with zipfile.ZipFile(self.target) as archive:
            content = archive.read("responses.csv")
            self.assertTrue(content.startswith(b"\xef\xbb\xbf"))
            rows = list(csv.DictReader(io.StringIO(content.decode("utf-8-sig"))))
            self.assertEqual(len(rows), 200)
            self.assertEqual(rows[0]["q001"], "'=2+3")
            self.assertEqual(rows[0]["q007"], "'  +SUM(A1:A2)")
            self.assertEqual((rows[0]["q004_01"], rows[0]["q004_02"]), ("0", "1"))
            self.assertEqual((rows[1]["q004_01"], rows[1]["q004_02"], rows[1]["q005"]), ("", "", ""))
            codebook = list(csv.DictReader(io.StringIO(archive.read("codebook.csv").decode("utf-8-sig"))))
            self.assertIn("Họ và tên", [item["label"] for item in codebook])
            report = json.loads(archive.read("summary.json"))
            agreement = next(item for item in report["fields"] if item["id"] == "agree")
            self.assertEqual(agreement["mean"], float(rows[0]["q003"]))
            tools = next(item for item in report["fields"] if item["id"] == "tools")
            self.assertEqual((tools["valid_n"], tools["missing"]), (100, 100))
            self.assertEqual(report["confirmed"], 200)
            metadata = json.loads(archive.read("metadata.json"))
            self.assertTrue(metadata["synthetic"])
            self.assertEqual(metadata["source"]["seed"], 42)
            self.assertEqual(metadata["source"]["sampler_version"], "web-2")
            self.assertEqual(metadata["source"]["schedule"]["min_seconds"], 30)

    def test_preview_only_missing_confirmation_and_size_limits_preserve_existing_file(self):
        self.target.write_bytes(b"existing")
        self.write([self.filled(1)])
        with self.assertRaises(TeachingToolsError):
            export_csv_bundle(self.schema, self.log, self.target)
        self.assertEqual(self.target.read_bytes(), b"existing")
        self.write([self.filled(1), {"event": "confirmed", "index": 1}, self.filled(2), {"event": "confirmed", "index": 2}])
        with self.assertRaises(TeachingToolsError):
            export_csv_bundle(self.schema, self.log, self.target, max_rows=1)
        self.assertEqual(self.target.read_bytes(), b"existing")
        self.assertEqual(list(self.target.parent.glob(".kdautoform-csv-*")), [])

    def test_dictionary_formula_labels_are_escaped(self):
        self.schema["fields"][0]["question"] = "=unsafe_label"
        self.write([self.filled(1), {"event": "confirmed", "index": 1}])
        export_csv_bundle(self.schema, self.log, self.target)
        with zipfile.ZipFile(self.target) as archive:
            rows = list(csv.DictReader(io.StringIO(archive.read("codebook.csv").decode("utf-8-sig"))))
        self.assertEqual(next(item["label"] for item in rows if item["variable"] == "q001"), "'=unsafe_label")


if __name__ == "__main__":
    unittest.main()

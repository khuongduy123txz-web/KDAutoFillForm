import copy
from collections import Counter
from datetime import date
import json
import os
import unittest

import google_form_tool as tool


GOOGLE_URL = "https://docs.google.com/forms/d/e/qa-google-regression/viewform"


def entry(identifier, options=None, *, row=None, rules=None):
    return [identifier, [[label, None, -2] for label in options] if options else None,
            True, [row] if row else None, rules or []]


def item(identifier, title, kind, entries):
    return [identifier, title, "", kind, entries]


def public_data(items):
    form = [None] * 9
    form[1] = items
    form[2] = ["Đã ghi nhận phản hồi kiểm thử."]
    form[8] = "Google regression fixture"
    return [None, form]


def comprehensive_data():
    birth = entry(8)
    birth.extend([None, None, [True, True]])
    clock = entry(9)
    clock.extend([None, [True]])
    grid = item(7, "Chọn cột khác nhau", 7, [
        entry(70, ["A", "B", "C"], row="Dòng một"),
        entry(71, ["A", "B", "C"], row="Dòng hai"),
    ])
    grid.extend([None, None, None, [[8, 205]]])
    multi_row = entry(1000, ["A", "B", "C"], row="Dòng chọn nhiều")
    multi_row.extend([None] * 6 + [[True]])
    return public_data([
        item(1, "Họ và tên", 0, [entry(1)]),
        item(2, "Góp ý", 1, [entry(2)]),
        item(3, "Giới tính", 2, [entry(3, ["Nam", "Nữ"])]),
        item(4, "Lựa chọn danh sách", 3, [entry(4, ["A", "B"])]),
        item(5, "Chọn đúng hai", 4, [entry(5, ["A", "B", "C"], rules=[[7, 204, ["2"]]])]),
        item(6, "Mức đồng ý", 5, [entry(6, ["1", "2", "3", "4", "5"])]),
        [100, "Trang thứ hai", "", 8, None, -2],
        grid,
        item(8, "Ngày sinh", 9, [birth]),
        item(9, "Thời lượng", 10, [clock]),
        item(10, "Bảng chọn nhiều", 7, [multi_row]),
        item(11, "Đánh giá sao", 18, [entry(11, ["1", "2", "3", "4", "5"])]),
    ])


class GoogleRegressionTests(unittest.TestCase):
    def test_original_google_urls_still_normalize_and_external_hosts_are_rejected(self):
        for suffix in ("viewform", "formResponse", "edit"):
            self.assertEqual(tool.form_link(GOOGLE_URL.replace("viewform", suffix) + "?usp=sharing"), GOOGLE_URL)
        self.assertEqual(tool.form_link("https://forms.gle/qa-code"), "https://forms.gle/qa-code")
        self.assertEqual(tool.form_link("https://forms.google.com/qa-code"), "https://forms.google.com/qa-code")
        self.assertEqual(tool.form_link("https://docs.google.com/forms/u/1/d/qa/edit"),
                         "https://docs.google.com/forms/u/1/d/qa/viewform")
        for url in ("http://docs.google.com/forms/d/e/qa/viewform", "https://docs.google.com.evil.example/forms/d/e/qa/viewform",
                    "https://docs.google.com@evil.example/forms/d/e/qa/viewform", "https://127.0.0.1/private",
                    "https://docs.google.com:8443/forms/d/e/qa/viewform", "https://docs.google.com/document/d/qa/edit"):
            with self.subTest(url=url), self.assertRaises(tool.FormError):
                tool.form_link(url)

    def test_google_metadata_keeps_all_supported_kinds_pages_and_grid_rows(self):
        schema = tool.parse_schema(comprehensive_data(), GOOGLE_URL)
        self.assertEqual(schema["question_count"], 11)
        self.assertEqual(len(schema["fields"]), 12)
        self.assertEqual(len(schema["pages"]), 2)
        self.assertFalse(schema["branching"])
        by_id = {field["id"]: field for field in schema["fields"]}
        self.assertEqual([field["kind"] for field in schema["fields"]],
                         ["text", "paragraph", "single", "dropdown", "multiple", "scale", "grid", "grid", "date", "time", "grid_multiple", "scale"])
        self.assertEqual((by_id["entry.5"]["minimum"], by_id["entry.5"]["maximum"]), (2, 2))
        self.assertEqual(by_id["entry.70"]["row"], "Dòng một")
        self.assertTrue(by_id["entry.70"]["exclusive_columns"])
        self.assertEqual(by_id["entry.70"]["page"], 1)
        self.assertTrue(by_id["entry.8"]["has_year"])
        self.assertTrue(by_id["entry.8"]["has_time"])
        self.assertTrue(by_id["entry.9"]["duration"])

    def test_google_branching_and_upload_failures_remain_explicit(self):
        data = public_data([item(1, "Đi tiếp?", 2, [entry(1, ["Có", "Không"])]),
                            [2, "Trang hai", "", 8, None, -2], item(3, "Góp ý", 0, [entry(3)])])
        data[1][1][0][4][0][1][0][2] = 2
        schema = tool.parse_schema(data, GOOGLE_URL)
        self.assertTrue(schema["branching"])
        self.assertEqual(schema["fields"][0]["actions"], {"Có": 2})
        for question in (item(1, "Tải tệp", 13, [entry(1)]), item(1, "Unknown", 99, [entry(1)]),
                         item(1, "Missing answer code", 0, [])):
            with self.subTest(question=question), self.assertRaises(tool.FormError):
                tool.parse_schema(public_data([question]), GOOGLE_URL)

    def test_natural_profiles_remain_consistent_and_grid_columns_unique(self):
        schema = tool.parse_schema(comprehensive_data(), GOOGLE_URL)
        sampler = tool.Sampler(schema, 1234, date(2026, 11, 3))
        for index in range(1, 26):
            answers = sampler.sample(index)
            tool.validate_answers(schema, answers)
            self.assertEqual(answers["entry.1"], sampler.last_profile["full_name"])
            self.assertEqual(answers["entry.8"], sampler.last_profile["birth_date"])
            self.assertEqual(answers["entry.3"], sampler.last_profile["gender"])
            self.assertEqual(len(answers["entry.5"]), 2)
            self.assertNotEqual(answers["entry.70"], answers["entry.71"])
        repeated = tool.Sampler(schema, 1234, date(2026, 11, 3))
        self.assertEqual(sampler.sample(7), repeated.sample(7))

    def test_finite_web_ratios_keep_exact_counts_for_radio_and_checkbox(self):
        schema = tool.parse_schema(public_data([
            item(1, "Lựa chọn", 2, [entry(1, ["A", "B", "C"])]),
            item(2, "Chọn hai", 4, [entry(2, ["A", "B", "C"], rules=[[7, 204, ["2"]]])]),
        ]), GOOGLE_URL)
        config = {"entry.1": {"rates": {"A": 20, "B": 30, "C": 50}},
                  "entry.2": {"rates": {"A": 100, "B": 50, "C": 50}}}
        sampler = tool.WebRatioSampler(schema, config, 20, 51)
        radio, checkbox = Counter(), Counter()
        self.assertTrue(sampler.exact)
        for index in range(1, 21):
            answers = sampler.sample(index)
            tool.validate_answers(schema, answers)
            radio[answers["entry.1"]] += 1
            checkbox.update(answers["entry.2"])
        self.assertEqual(radio, {"A": 4, "B": 6, "C": 10})
        self.assertEqual(checkbox, {"A": 20, "B": 10, "C": 10})

    def test_schema_change_fingerprint_still_detects_rules_and_new_questions(self):
        schema = tool.parse_schema(comprehensive_data(), GOOGLE_URL)
        changed = copy.deepcopy(schema)
        changed["fields"][0]["required"] = False
        self.assertNotEqual(tool.fingerprint(schema), tool.fingerprint(changed))
        changed = copy.deepcopy(schema)
        changed["fields"] = changed["fields"][:-1]
        self.assertNotEqual(tool.fingerprint(schema), tool.fingerprint(changed))


@unittest.skipUnless(os.environ.get("KD_BROWSER_TESTS") == "1", "Set KD_BROWSER_TESTS=1 with Chromium installed")
class GoogleBrowserRegressionTests(unittest.TestCase):
    def test_two_page_google_form_prepare_never_submits_and_submit_waits_for_confirmation(self):
        data = public_data([item(1, "Họ và tên", 0, [entry(1)]),
                            [100, "Trang hai", "", 8, None, -2],
                            item(2, "Bạn đồng ý?", 2, [entry(2, ["Có", "Không"])])])
        html = """<!doctype html><html><body><form>
        <input type='hidden' name='pageHistory' value='0'>
        <div id='first' role='listitem'><h3>Họ và tên</h3><input name='entry.1' aria-label='Họ và tên'></div>
        <div id='second' role='listitem' hidden><h3>Bạn đồng ý?</h3><input type='hidden' name='entry.2'>
        <div role='radio' tabindex='0' data-value='Có' aria-label='Có' aria-checked='false' onclick="this.setAttribute('aria-checked','true')">Có</div>
        <div role='radio' tabindex='0' data-value='Không' aria-label='Không' aria-checked='false' onclick="this.setAttribute('aria-checked','true')">Không</div></div>
        <button type='button' id='next' onclick="document.querySelector('[name=pageHistory]').value='0,1';first.hidden=true;second.hidden=false;this.hidden=true;send.hidden=false">Tiếp</button>
        <button type='button' id='send' hidden onclick="fetch(location.href.replace('/viewform','/formResponse'),{method:'POST'}).then(()=>{document.body.innerHTML='<div class=vHW8K>Đã ghi nhận phản hồi kiểm thử.</div>'})">Gửi</button>
        </form><script>window.FB_PUBLIC_LOAD_DATA_=__DATA__;</script></body></html>""".replace("__DATA__", json.dumps(data, ensure_ascii=False))
        submissions = []
        class RoutedGoogleBrowser(tool.FormBrowser):
            def new_session(self):
                super().new_session()
                def route(request_route):
                    request = request_route.request
                    if request.method == "POST":
                        submissions.append(request.url)
                        request_route.fulfill(status=200, content_type="text/plain", body="ok")
                    else:
                        request_route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)
                self.session.route("**/*", route)
        with RoutedGoogleBrowser(headless=True) as browser:
            schema = browser.load(GOOGLE_URL)
            report = browser.prepare(schema, {"entry.1": "Nguyễn Khương Duy", "entry.2": "Có"})
            self.assertEqual(report, {"pages": 2, "page_indices": [0, 1], "questions": 2, "fields": 2})
            self.assertEqual(submissions, [])
            self.assertTrue(browser.submit(schema))
            self.assertEqual(submissions, [GOOGLE_URL.replace("/viewform", "/formResponse")])


if __name__ == "__main__":
    unittest.main()

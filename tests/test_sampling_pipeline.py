"""Single-pass response sampling, exact ratios and deterministic profiles."""
from collections import Counter
from datetime import date
import unittest
from unittest.mock import patch

import google_form_tool as tool
from test_google_regression import comprehensive_data, entry, item, public_data, GOOGLE_URL


class SamplingPipelineTests(unittest.TestCase):
    def schema(self):
        return tool.parse_schema(comprehensive_data(), GOOGLE_URL)

    def test_web_generates_each_auto_text_once_and_never_generates_fixed_text(self):
        schema = self.schema()
        config = tool.default_web_config(schema, 41)
        config["entry.2"] = {"mode": "fixed", "value": "Fixed feedback"}
        with patch.object(tool.Sampler, "sample", side_effect=AssertionError("No discarded base response")), \
             patch.object(tool, "preference", side_effect=AssertionError("No discarded base choice weights")), \
             patch.object(tool, "sample_text", wraps=tool.sample_text) as text_calls, \
             patch.object(tool, "make_profile", wraps=tool.make_profile) as profile_calls:
            sampler = tool.WebRatioSampler(schema, config, 20, 51, "2026-11-04")
            answers = sampler.sample(1)
        self.assertEqual([call.args[0]["id"] for call in text_calls.call_args_list], ["entry.1"])
        self.assertEqual(profile_calls.call_count, 1)
        self.assertIsNone(sampler.base._weights)
        self.assertEqual(answers["entry.2"], "Fixed feedback")
        self.assertEqual(answers["entry.1"], sampler.last_profile["full_name"])
        self.assertEqual(answers["entry.8"], sampler.last_profile["birth_date"])
        self.assertEqual(answers["entry.3"], sampler.last_profile["gender"])
        tool.validate_answers(schema, answers)

    def test_fixed_text_does_not_need_a_random_text_generator(self):
        schema = self.schema()
        config = tool.default_web_config(schema, 1)
        config["entry.1"] = {"mode": "fixed", "value": "Fixed name"}
        config["entry.2"] = {"mode": "fixed", "value": "Fixed feedback"}
        sampler = tool.WebRatioSampler(schema, config, 10, 10, "2026-11-04")
        with patch.object(tool, "sample_text", side_effect=AssertionError("Fixed values must skip generation")):
            answers = sampler.sample(1)
        self.assertEqual((answers["entry.1"], answers["entry.2"]), ("Fixed name", "Fixed feedback"))

    def test_exact_radio_checkbox_and_scale_rates_survive_single_pass(self):
        schema = tool.parse_schema(public_data([
            item(1, "Choice", 2, [entry(1, ["A", "B", "C"])]),
            item(2, "Pick two", 4, [entry(2, ["A", "B", "C"], rules=[[7, 204, ["2"]]])]),
            item(3, "Scale", 5, [entry(3, ["1", "2", "3", "4", "5"])]),
        ]), GOOGLE_URL)
        config = {"entry.1": {"rates": {"A": 20, "B": 30, "C": 50}},
                  "entry.2": {"rates": {"A": 100, "B": 50, "C": 50}},
                  "entry.3": {"rates": {"1": 10, "2": 10, "3": 20, "4": 30, "5": 30}}}
        sampler = tool.WebRatioSampler(schema, config, 20, 8, "2026-11-04")
        radio, checkbox, scale = Counter(), Counter(), Counter()
        for index in range(1, 21):
            answers = sampler.sample(index)
            tool.validate_answers(schema, answers)
            radio[answers["entry.1"]] += 1
            checkbox.update(answers["entry.2"])
            scale[answers["entry.3"]] += 1
        self.assertEqual(radio, {"A": 4, "B": 6, "C": 10})
        self.assertEqual(checkbox, {"A": 20, "B": 10, "C": 10})
        self.assertEqual(scale, {"1": 2, "2": 2, "3": 4, "4": 6, "5": 6})

    def test_seed_profiles_dates_and_exclusive_grid_are_repeatable(self):
        schema = self.schema()
        config = tool.default_web_config(schema, 42)
        samplers = [tool.WebRatioSampler(schema, config, 0, 17, "2026-11-04") for _ in range(2)]
        for index in range(1, 31):
            first, second = [sampler.sample(index) for sampler in samplers]
            self.assertEqual(first, second)
            profile = samplers[0].last_profile
            self.assertEqual(profile, samplers[1].last_profile)
            self.assertEqual(first["entry.1"], profile["full_name"])
            self.assertEqual(first["entry.3"], profile["gender"])
            self.assertEqual(first["entry.8"], profile["birth_date"])
            self.assertNotEqual(first["entry.70"], first["entry.71"])
            self.assertIn(profile["given_name"], profile["full_name"])
            self.assertTrue(profile["email"].endswith(str(profile["birth_date"]["year"]) + "@example.com"))
            birth = date(**{key: profile["birth_date"][key] for key in ("year", "month", "day")})
            reference = date(2026, 11, 4)
            self.assertEqual(profile["age"], reference.year - birth.year - ((reference.month, reference.day) < (birth.month, birth.day)))
            tool.validate_answers(schema, first)

    def test_last_recognized_gender_choice_defines_the_single_profile(self):
        birth = entry(5)
        birth.extend([None, None, [False, True]])
        schema = tool.parse_schema(public_data([
            item(1, "Gender", 2, [entry(1, ["Male", "Female"])]),
            item(2, "Gender again", 2, [entry(2, ["Male", "Female"])]),
            item(3, "Full name", 0, [entry(3)]),
            item(4, "Email", 0, [entry(4)]),
            item(5, "Birth date", 9, [birth]),
        ]), GOOGLE_URL)
        config = tool.default_web_config(schema, 1)
        config["entry.1"]["rates"] = {"Male": 100, "Female": 0}
        config["entry.2"]["rates"] = {"Male": 0, "Female": 100}
        sampler = tool.WebRatioSampler(schema, config, 10, 1, "2026-11-04")
        with patch.object(tool, "make_profile", wraps=tool.make_profile) as profiles:
            answers = sampler.sample(1)
        self.assertEqual(profiles.call_count, 1)
        self.assertEqual((answers["entry.1"], answers["entry.2"]), ("Male", "Female"))
        self.assertEqual(sampler.last_profile["gender"], "Nữ")
        self.assertEqual(answers["entry.3"], sampler.last_profile["full_name"])
        self.assertEqual(answers["entry.4"], sampler.last_profile["email"])
        self.assertEqual(answers["entry.5"], sampler.last_profile["birth_date"])

    def test_prepared_profile_keeps_age_rules_and_selected_faculty(self):
        birth = entry(3)
        birth.extend([None, None, [False, True]])
        schema = tool.parse_schema(public_data([
            item(1, "Age", 0, [entry(1, rules=[[1, 7, ["25", "30"]]])]),
            item(2, "Major", 2, [entry(2, ["Engineering", "Economics"])]),
            item(3, "Birth date", 9, [birth]),
        ]), GOOGLE_URL)
        sampler = tool.WebRatioSampler(schema, tool.default_web_config(schema, 9), 0, 9, "2026-11-04")
        with patch.object(tool, "profile_template", side_effect=AssertionError("Template must already be prepared")):
            for index in range(1, 21):
                answers = sampler.sample(index)
                tool.validate_answers(schema, answers)
                self.assertTrue(25 <= int(answers["entry.1"]) <= 30)
                self.assertEqual(int(answers["entry.1"]), sampler.last_profile["age"])
                self.assertEqual(answers["entry.2"], sampler.last_profile["faculty"])
                self.assertEqual(answers["entry.3"], sampler.last_profile["birth_date"])

    def test_cli_sampler_keeps_its_existing_seed_stream(self):
        sampler = tool.Sampler(self.schema(), 1234, date(2026, 11, 3))
        # Snapshot from the preceding release, including random draw ordering.
        self.assertEqual(sampler.sample(1), {
            "entry.1": "Phạm Minh Khang",
            "entry.2": "Tôi đề xuất cải thiện những khâu còn mất thời gian và hướng dẫn bằng ví dụ cụ thể để mọi người dễ áp dụng.",
            "entry.3": "Nam", "entry.4": "B", "entry.5": ["B", "A"], "entry.6": "5",
            "entry.70": "B", "entry.71": "C",
            "entry.8": {"year": 2006, "month": 4, "day": 24, "hour": 14, "minute": 10},
            "entry.9": {"hour": 3, "minute": 23, "second": 51},
            "entry.1000": ["C"], "entry.11": "4",
        })


if __name__ == "__main__":
    unittest.main()

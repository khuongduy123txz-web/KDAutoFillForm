import copy
import json
import os
import unittest
from urllib.parse import urlsplit

import google_form_tool as tool
from microsoft_forms import (
    create_microsoft_browser, find_form_payload, is_microsoft_url,
    normalize_microsoft_url, parse_microsoft_schema,
)


URL = "https://forms.cloud.microsoft/Pages/ResponsePage.aspx?id=fixture"
API = "https://forms.cloud.microsoft/formapi/api/tenant/users/owner/light/runtimeForms('fixture')"


def question(identifier, title, kind, order, info=None, **extra):
    return {"id": identifier, "title": title, "type": kind, "order": order,
            "required": True, "questionInfo": json.dumps(info or {}), **extra}


def fixture():
    return {"id": "fixture", "title": "Public test form", "settings": "{}",
            "descriptiveQuestions": [
                question("section1", "About you", "Question.ColumnGroup", 0),
                question("section2", "Your opinion", "Question.ColumnGroup", 100),
            ], "questions": [
                question("name", "Full name", "Question.TextField", 1),
                question("details", "Feedback", "Question.TextField", 2, {"Multiline": True}),
                question("email", "Email", "Question.TextField", 3, {"Validation": {"rule": 11}}),
                question("single", "Continue?", "Question.Choice", 4,
                         {"ChoiceType": 1, "Choices": [{"Description": "Yes"}, {"Description": "No"}]}),
                question("multi", "Tools", "Question.Choice", 5,
                         {"ChoiceType": 2, "Choices": [{"Description": "A"}, {"Description": "B"}], "ChoiceRestrictionType": "Exactly", "ChoiceRestrictionValue": 2}),
                question("dropdown", "City", "Question.Choice", 6,
                         {"ChoiceType": 1, "OptionDisplayStyle": "DropDown", "Choices": [{"Description": "Hanoi"}, {"Description": "Hue"}]}),
                question("matrix", "Service", "Question.MatrixChoiceGroup", 101,
                         {"Choices": [{"Description": "Disagree"}, {"Description": "Agree"}]}),
                question("row1", "Useful", "Question.MatrixChoice", 102, groupId="matrix"),
                question("row2", "Fast", "Question.MatrixChoice", 103, groupId="matrix"),
                question("rating", "Rating", "Question.Rating", 104, {"Length": 5, "MinRating": 1}),
                question("nps", "Recommend", "Question.NPS", 105),
                question("date", "Date of birth", "Question.DateTime", 106),
            ]}


class MicrosoftSchemaTests(unittest.TestCase):
    def test_url_accepts_only_response_links_on_exact_microsoft_hosts(self):
        for url in (URL, "https://forms.office.com/r/abcdef", "https://forms.office365.com/e/abcdef"):
            self.assertTrue(is_microsoft_url(url))
            self.assertTrue(normalize_microsoft_url(url).startswith("https://"))
        for url in ("https://forms.office.com.evil.test/r/code", "https://user:pass@forms.office.com/r/code",
                    "https://forms.office.com:443/r/code", "http://forms.office.com/r/code",
                    "https://forms.office.com/Pages/DesignPageV2.aspx?id=foo", "https://forms.office.com/Pages/ResponsePage.aspx"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                normalize_microsoft_url(url)
        self.assertFalse(is_microsoft_url("not a URL"))

    def test_response_query_keeps_form_id_and_removes_fragment(self):
        result = normalize_microsoft_url(URL + "&lang=vi#secret")
        self.assertIn("id=fixture", result)
        self.assertIn("lang=vi", result)
        self.assertNotIn("#", result)

    def test_metadata_covers_every_section_and_likert_row(self):
        schema = parse_microsoft_schema({"Data": {"Form": fixture()}}, URL)
        self.assertEqual(schema["provider"], "microsoft")
        self.assertEqual(len(schema["pages"]), 2)
        self.assertEqual(schema["question_count"], 10)
        self.assertEqual(len(schema["fields"]), 11)
        kinds = {field["kind"] for field in schema["fields"]}
        self.assertTrue({"text", "paragraph", "single", "multiple", "dropdown", "grid", "scale", "date"} <= kinds)
        multi = next(f for f in schema["fields"] if f["id"] == "ms.multi")
        self.assertEqual((multi["minimum"], multi["maximum"]), (2, 2))
        rows = [f for f in schema["fields"] if f["kind"] == "grid"]
        self.assertEqual([f["row"] for f in rows], ["Useful", "Fast"])
        self.assertTrue(all(f["item_id"] == "matrix" for f in rows))
        nps = next(f for f in schema["fields"] if f["id"] == "ms.nps")
        self.assertEqual(nps["options"], [str(i) for i in range(11)])

    def test_pascalcase_payload_and_json_wrappers(self):
        data = fixture()
        data = {key[0].upper() + key[1:]: value for key, value in data.items()}
        data["Questions"] = [{key[0].upper() + key[1:]: value for key, value in q.items()} for q in data["Questions"]]
        self.assertEqual(len(parse_microsoft_schema({"form": json.dumps(data)}, URL)["fields"]), 11)
        self.assertIsNone(find_form_payload({"responses": [{"id": "wrong", "questions": []}]}))

    def test_branch_targets_enable_dynamic_visibility_without_omitting_schema(self):
        data = fixture()
        info = json.loads(data["questions"][3]["questionInfo"])
        info["Choices"][1]["BranchInfo"] = {"TargetQuestionId": "date"}
        data["questions"][3]["questionInfo"] = json.dumps(info)
        schema = parse_microsoft_schema(data, URL)
        self.assertTrue(schema["branching"])
        self.assertEqual(schema["fields"][3]["actions"]["No"], "date")
        self.assertEqual(len(schema["fields"]), 11)

    def test_text_validation_maps_to_existing_sampler_rules(self):
        data = fixture()
        data["questions"][0]["questionInfo"] = json.dumps({"NumberValidation": {"NumberValidationRule": 7, "NumberMinBoundary": 18, "NumberMaxBoundary": 25}})
        schema = parse_microsoft_schema(data, URL)
        answers = tool.Sampler(schema, 42).sample(1)
        self.assertTrue(18 <= float(answers["ms.name"]) <= 25)
        self.assertIn("@", answers["ms.email"])
        tool.validate_answers(schema, answers)

    def test_unknown_questions_and_restrictions_fail_instead_of_being_skipped(self):
        for kind in ("Question.Ranking", "Question.FileUpload", "Question.Unknown"):
            data = fixture()
            data["questions"][0]["type"] = kind
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                parse_microsoft_schema(data, URL)
        data = fixture()
        data["questions"][0]["questionInfo"] = json.dumps({"Validation": {"rule": 999}})
        with self.assertRaises(ValueError):
            parse_microsoft_schema(data, URL)
        data = fixture()
        data["settings"] = json.dumps({"RequiresUniqueResponse": True})
        with self.assertRaises(ValueError):
            parse_microsoft_schema(data, URL)

    def test_duplicate_ids_labels_and_orphaned_rows_fail(self):
        for change in ("id", "label", "row"):
            data = fixture()
            if change == "id":
                data["questions"][1]["id"] = "name"
            elif change == "label":
                data["questions"][3]["questionInfo"] = json.dumps({"Choices": [{"Description": "Yes"}, {"Description": "Yes"}]})
            else:
                data["questions"][7]["groupId"] = "missing"
            with self.subTest(change=change), self.assertRaises(ValueError):
                parse_microsoft_schema(data, URL)


FIXTURE_HTML = """<!doctype html><meta charset=utf-8><div id=content></div><script>
window.OfficeFormServerInfo={prefetchFormUrl:API,prefetchFormData:FORM};
const raw=FORM;let step=0;window.submits=0;
const card=(id,title,body)=>'<div data-automation-id="questionItem"><div id="QuestionId_'+id+'"><span data-automation-id="questionTitle">'+title+'</span></div>'+body+'</div>';
const radio=(name,labels)=>'<div role="radiogroup" aria-label="'+name+'">'+labels.map(x=>'<label><input type=radio name="'+name+'" value="'+x+'" aria-label="'+x+'">'+x+'</label>').join('')+'</div>';
function render(){content.innerHTML=step===0?
card('name','Full name','<input aria-label="Full name">')+
card('details','Feedback','<textarea aria-label="Feedback"></textarea>')+
card('email','Email','<input aria-label="Email">')+
card('single','Continue?',radio('single',['Yes','No']))+
card('multi','Tools',['A','B'].map(x=>'<label><input type=checkbox aria-label="'+x+'">'+x+'</label>').join(''))+
card('dropdown','City','<select><option>Hanoi</option><option>Hue</option></select>')+
'<button data-automation-id="nextButton" onclick="step=1;render()">Next</button>':
card('matrix','Service',radio('Useful',['Disagree','Agree'])+radio('Fast',['Disagree','Agree']))+
card('rating','Rating',radio('Rating',['1','2','3','4','5']))+
card('nps','Recommend',radio('Recommend',Array.from({length:11},(_,i)=>String(i))))+
card('date','Date of birth','<input type=date>')+
'<button data-automation-id="submitButton" onclick="send()">Submit</button>';}
async function send(){window.submits++;const r=await fetch(RESP,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({answers:'test'})});await r.json();content.innerHTML='<div data-automation-id="thankYouMessage">Your response was submitted.</div>';}
render();fetch(API);
</script>"""


@unittest.skipUnless(os.environ.get("KD_BROWSER_TESTS") == "1", "Set KD_BROWSER_TESTS=1 with installed Chromium")
class MicrosoftBrowserTests(unittest.TestCase):
    def setUp(self):
        self.form = fixture()
        self.posts = []
        self.response_result = {"id": 1}
        self.intro = False
        Base = create_microsoft_browser(tool.FormBrowser, tool.FormError, tool.normalized, tool.plain, tool.fingerprint, tool.validate_answers)
        test = self
        class FixtureBrowser(Base):
            def new_session(self):
                super().new_session()
                self.page.route("**/*", test.route)
        self.browser = FixtureBrowser(headless=True).__enter__()
        self.addCleanup(self.browser.__exit__, None, None, None)

    def route(self, route):
        request = route.request
        if request.method == "POST":
            self.posts.append(request.url)
            route.fulfill(status=200, content_type="application/json", body=json.dumps(self.response_result))
        elif "/formapi/api/" in request.url:
            route.fulfill(status=200, content_type="application/json", body=json.dumps(self.form))
        else:
            response_url = "https://forms.cloud.microsoft/formapi/api/tenant/users/owner/forms('fixture')/responses"
            template = FIXTURE_HTML
            if self.intro:
                template = template.replace("render();fetch(API);", "content.innerHTML='<button onclick=send()>Start now</button>';fetch(API);")
            html = template.replace("API", json.dumps(API)).replace("FORM", json.dumps(self.form)).replace("RESP", json.dumps(response_url))
            route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)

    def prepare(self):
        schema = self.browser.load(URL)
        sampler = tool.Sampler(schema, 42)
        answers = sampler.sample(1)
        self.browser.profile = sampler.last_profile
        return schema, self.browser.prepare(schema, answers)

    def test_prepare_fills_all_control_types_and_stops_before_submit(self):
        schema, report = self.prepare()
        self.assertEqual((report["pages"], report["questions"], report["fields"]), (2, 10, 11))
        self.assertEqual(len(report["field_ids"]), 11)
        self.assertEqual(self.posts, [])
        self.assertEqual(self.browser.page.evaluate("window.submits"), 0)
        self.assertTrue(self.browser.page.locator('input[type="date"]').input_value())

    def test_scan_does_not_press_start_or_trigger_a_timed_response(self):
        self.intro = True
        schema = self.browser.load(URL)
        self.assertEqual(schema["question_count"], 10)
        self.assertEqual(self.posts, [])
        self.assertEqual(self.browser.page.evaluate("window.submits"), 0)

    def test_submit_requires_network_response_id_and_confirmation(self):
        schema, _ = self.prepare()
        self.assertTrue(self.browser.submit(schema))
        self.assertEqual(len(self.posts), 1)

    def test_http200_error_payload_is_not_confirmed(self):
        schema, _ = self.prepare()
        self.response_result = {"error": {"message": "Not saved"}}
        with self.assertRaises(tool.FormError):
            self.browser.submit(schema)
        self.assertEqual(len(self.posts), 1)

    def test_schema_changes_stop_before_filling_or_submit(self):
        schema = self.browser.load(URL)
        answers = tool.Sampler(schema, 42).sample(1)
        self.form["questions"][0]["title"] = "Changed name"
        with self.assertRaises(tool.FormError):
            self.browser.prepare(schema, answers)
        self.assertEqual(self.posts, [])


if __name__ == "__main__":
    unittest.main()

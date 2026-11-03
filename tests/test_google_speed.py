"""Real browser regression and timing evidence; every submission is locally routed."""
import copy
import json
import os
import time
import unittest
from unittest.mock import Mock

import google_form_tool as tool
from google_fast import FastFillError, fill_google_page
from test_google_regression import public_data, item, entry

URL = "https://docs.google.com/forms/d/e/qa-google-speed/viewform"


def four_page_data():
    items = []
    for page in range(4):
        if page:
            items.append([1000 + page, "Page " + str(page + 1), "", 8, None, -2])
        for offset in range(10):
            identifier = page * 10 + offset + 1
            items.append(item(identifier, "Question " + str(identifier),
                              0 if offset < 5 else 2,
                              [entry(identifier, None if offset < 5 else ["Yes", "No"])]))
    return public_data(items)


def four_page_html(data):
    schema = tool.parse_schema(data, URL)
    return """<!doctype html><html><head><style>
    [role=listitem]{padding:4px}input{min-height:20px}
    </style></head><body><form onsubmit="return false">
    <input type=hidden name=pageHistory value=0><div id=questions></div>
    <button type=button id=next onclick="move()">Next</button>
    <button type=button id=send hidden onclick="finish()">Submit</button>
    </form><script>
    window.FB_PUBLIC_LOAD_DATA_=__DATA__;
    const fields=__FIELDS__;let index=0;window.saved={};window.transitions=0;
    function record(){for(const [key,value] of new FormData(document.querySelector('form')))if(key.startsWith('entry.'))saved[key]=value}
    function render(){
      questions.replaceChildren();
      for(const f of fields.filter(f=>f.page===index)){
        const card=document.createElement('div');card.setAttribute('role','listitem');
        const title=document.createElement('h3');title.textContent=f.question;card.append(title);
        for(const option of f.options.length?f.options:[null]){
          const input=document.createElement('input');input.name=f.id;input.required=true;
          input.type=option===null?'text':'radio';input.setAttribute('aria-label',option===null?f.question:option);
          if(option!==null){input.value=option;input.setAttribute('data-value',option)}
          card.append(input);
        }
        questions.append(card);
      }
      next.hidden=index===3;send.hidden=index!==3;
    }
    function move(){if(!document.querySelector('form').checkValidity())return;record();
      index++;transitions++;document.querySelector('[name=pageHistory]').value+=','+index;render()}
    async function finish(){if(!document.querySelector('form').checkValidity())return;record();
      await fetch(location.href.replace('/viewform','/formResponse'),{method:'POST',body:JSON.stringify(saved)});
      document.body.innerHTML='<div class=vHW8K>Recorded test response.</div>'}
    render();
    </script></body></html>""".replace("__DATA__", json.dumps(data)).replace("__FIELDS__", json.dumps(schema["fields"]))


class BatchTransportTests(unittest.TestCase):
    def test_duplicate_ids_are_rejected_before_browser_actions(self):
        page = Mock()
        with self.assertRaises(FastFillError):
            fill_google_page(page, [{"id": "entry.1"}] * 2, {})
        page.evaluate.assert_not_called()

    def test_malformed_result_does_not_claim_fields_were_filled(self):
        page = Mock()
        page.evaluate.return_value = {"handled": ["entry.other"]}
        with self.assertRaises(FastFillError):
            fill_google_page(page, [{"id": "entry.1"}], {"entry.1": "A"})


@unittest.skipUnless(os.environ.get("KD_BROWSER_TESTS") == "1", "Set KD_BROWSER_TESTS=1")
class GoogleSpeedBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bot = tool.FormBrowser(headless=True).__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.bot.__exit__(None, None, None)

    def page(self, html):
        self.bot.new_session()
        self.bot.page.set_content("<form>" + html + "</form>")
        return self.bot.page

    def field(self, identifier, kind="text", options=None, **extra):
        return {"id": "entry." + str(identifier), "question": "Question " + str(identifier),
                "kind": kind, "options": options or [], **extra}

    def card(self, identifier, body):
        return '<div role="listitem"><h3>Question ' + str(identifier) + '</h3>' + body + '</div>'

    def test_native_text_checkbox_grid_and_temporal_events_reach_real_controls(self):
        fields = [self.field(1), self.field(2, "paragraph"),
                  self.field(3, "multiple", ["A", "B", "C"]),
                  self.field(4, "grid", ["A", "B"], row="Row one"),
                  self.field(5, "date"), self.field(6, "time")]
        html = self.card(1, '<input name="entry.1" oninput="window.textEvent=this.value">')
        html += self.card(2, '<textarea name="entry.2"></textarea>')
        html += self.card(3, ''.join('<input type=checkbox name="entry.3" aria-label="'+v+'" value="'+v+'"'+(' checked' if v=="B" else '')+'>' for v in ["A","B","C"]))
        html += self.card(4, '<div role=radiogroup aria-label="Row one">'+''.join('<input type=radio name="entry.4" aria-label="'+v+'" value="'+v+'">' for v in ["A","B"])+'</div>')
        html += self.card(5, '<input type=date name="entry.5">')
        html += self.card(6, '<input type=time name="entry.6">')
        page = self.page(html)
        answers = {"entry.1": "Synthetic Student", "entry.2": "A useful comment",
                   "entry.3": ["A","C"], "entry.4": "B",
                   "entry.5": {"year":2002,"month":7,"day":19},
                   "entry.6": {"hour":14,"minute":23}}
        result = fill_google_page(page, fields, answers)
        self.assertEqual(result["handled"], list(answers), result)
        self.assertEqual(page.evaluate("window.textEvent"), answers["entry.1"])
        self.assertEqual(page.evaluate("Array.from(document.querySelectorAll('[name=\"entry.3\"]:checked')).map(e=>e.value)"), ["A","C"])
        self.assertEqual(page.locator('[name="entry.5"]').input_value(), "2002-07-19")

    def test_async_google_like_controller_and_cumulative_rating_are_verified(self):
        fields = [self.field(1, "single", ["Yes","No"]), self.field(2,"scale",["1","2","3"])]
        html = self.card(1, '<input type=hidden name="entry.1_sentinel"><div role=radio aria-label=Yes aria-checked=false onclick="setTimeout(()=>{this.setAttribute(\'aria-checked\',\'true\');document.querySelector(\'[name=&quot;entry.1&quot;]\').value=\'Yes\'},30)">Yes</div><input type=hidden name="entry.1">')
        html += self.card(2, '<input type=hidden name="entry.2"><div role=radio aria-label=1 aria-checked=false>1</div><div role=radio aria-label=2 aria-checked=false onclick="this.parentElement.querySelectorAll(\'[role=radio]\').forEach((e,i)=>e.setAttribute(\'aria-checked\',i&lt;2?\'true\':\'false\'));this.parentElement.querySelector(\'input\').value=\'2\'">2</div><div role=radio aria-label=3 aria-checked=false>3</div>')
        page = self.page(html)
        result = fill_google_page(page, fields, {"entry.1":"Yes","entry.2":"2"})
        self.assertEqual(result["handled"], ["entry.1","entry.2"], result)
        self.assertEqual(page.locator('[name="entry.1"]').input_value(), "Yes")

    def test_dropdown_readonly_and_unverified_custom_state_use_fallback(self):
        fields = [self.field(1,"dropdown",["A"]),self.field(2),
                  self.field(3,"single",["A"])]
        html = self.card(1, '<select name="entry.1"><option>A</option></select>')
        html += self.card(2, '<input readonly name="entry.2">')
        html += self.card(3, '<input type=hidden name="entry.3"><div role=radio aria-label=A aria-checked=false onclick="this.setAttribute(\'aria-checked\',\'true\')">A</div>')
        page = self.page(html)
        result = fill_google_page(page, fields, {"entry.1":"A","entry.2":"X","entry.3":"A"})
        self.assertEqual(result["fallback"], ["entry.1","entry.2","entry.3"])
        self.assertEqual(result["handled"], [])
        self.assertEqual(page.locator('[name="entry.3"]').input_value(), "")

    def test_disabled_hydration_waits_once_without_forcing_disabled_state(self):
        page = self.page(self.card(1, '<input disabled name="entry.1"><script>setTimeout(()=>document.querySelector("[name=\\"entry.1\\"]").disabled=false,50)</script>'))
        result = fill_google_page(page, [self.field(1)], {"entry.1":"Ready"})
        self.assertEqual(result["handled"], ["entry.1"], result)

    def test_duplicate_question_titles_never_choose_arbitrary_card(self):
        page = self.page(self.card(1, '<input>') + self.card(1, '<input>'))
        result = fill_google_page(page, [self.field(1)], {"entry.1":"X"})
        self.assertEqual(result["fallback"], ["entry.1"])
        self.assertEqual(page.locator('input').evaluate_all("els=>els.map(e=>e.value)"), ["",""])

    def test_four_pages_fast_and_compatible_have_identical_answers_and_confirmation(self):
        data = four_page_data()
        schema = tool.parse_schema(data, URL)
        answers = {f["id"]: "Synthetic Student" if not f["options"] else "Yes" for f in schema["fields"]}
        html = four_page_html(data)
        evidence = {}
        for engine in ("compatible", "fast"):
            submissions = []
            class RoutedBrowser(tool.FormBrowser):
                def new_session(inner):
                    super().new_session()
                    def route(r):
                        if r.request.method == "POST":
                            submissions.append(json.loads(r.request.post_data))
                            r.fulfill(status=200,content_type="text/plain",body="ok")
                        else:
                            r.fulfill(status=200,content_type="text/html; charset=utf-8",body=html)
                    inner.session.route("**/*",route)
            # Share the browser process but retain a fresh respondent context.
            browser = RoutedBrowser(headless=True)
            browser.browser = self.bot.browser
            browser.engine = engine
            try:
                started = time.perf_counter()
                report = browser.prepare(schema, answers)
                self.assertEqual(report, {"pages":4,"page_indices":[0,1,2,3],"questions":40,"fields":40})
                self.assertEqual(submissions, [])
                self.assertEqual(browser.page.evaluate("window.transitions"), 3)
                self.assertTrue(browser.submit(schema))
                self.assertEqual(submissions, [answers])
                evidence[engine] = {"seconds":round(time.perf_counter()-started,3),"metrics":browser.metrics}
                self.assertEqual(browser.metrics["batch_fields"],40 if engine=="fast" else 0)
                self.assertEqual(browser.metrics["fallback_fields"],0 if engine=="fast" else 40)
            finally:
                if browser.session:
                    browser.session.close()
        print("FOUR_PAGE_ROUTED_BENCHMARK "+json.dumps(evidence),flush=True)

    def test_fast_navigation_respects_native_validation_and_ambiguous_buttons(self):
        page = self.page('<input required name=test><button type=button onclick="window.moved=true">Next</button>')
        with self.assertRaises(tool.FormError):
            self.bot.click_navigation("next")
        self.assertIsNone(page.evaluate("window.moved"))
        page.locator('input').fill("Valid")
        page.locator('form').evaluate("e=>e.insertAdjacentHTML('beforeend','<button type=button>Next</button>')")
        with self.assertRaises(tool.FormError):
            self.bot.click_navigation("next")
        self.assertIsNone(page.evaluate("window.moved"))

    def test_branch_skips_only_declared_pages_and_nonbranch_missing_page_stops(self):
        data = four_page_data()
        # Route the first answer directly to the third section.
        data[1][1][5][4][0][1][0][2] = 1002
        schema = tool.parse_schema(data, URL)
        self.assertTrue(schema["branching"])
        html = four_page_html(data).replace("index++;transitions++", "index=index===0?2:index+1;transitions++")
        answers = {f["id"]:"Text" if not f["options"] else "Yes" for f in schema["fields"]}
        browser = tool.FormBrowser(headless=True)
        browser.browser = self.bot.browser
        original = browser.new_session
        def routed():
            original()
            browser.session.route("**/*",lambda route:route.fulfill(status=200,content_type="text/html",body=html))
        browser.new_session = routed
        try:
            report = browser.prepare(schema, answers)
            self.assertEqual(report["page_indices"], [0,2,3])
            self.assertEqual(report["fields"], 30)
            self.assertEqual(browser.metrics["batch_fields"], 30)
            schema["branching"] = False
            with self.assertRaises(tool.FormError):
                browser.prepare(schema, answers)
        finally:
            if browser.session:
                browser.session.close()

    def test_prepare_checks_fingerprint_before_batch_actions(self):
        data = four_page_data()
        schema = tool.parse_schema(data, URL)
        changed = copy.deepcopy(schema)
        changed["fields"][0]["required"] = False
        browser = tool.FormBrowser(headless=True)
        browser.browser = self.bot.browser
        original = browser.new_session
        def routed():
            original()
            browser.session.route("**/*",lambda route:route.fulfill(status=200,content_type="text/html",body=four_page_html(data)))
        browser.new_session = routed
        try:
            with self.assertRaises(tool.FormError):
                browser.prepare(changed,{f["id"]:"Value" if not f["options"] else "Yes" for f in schema["fields"]})
            self.assertEqual(browser.page.locator('[name="entry.1"]').input_value(), "")
        finally:
            if browser.session:
                browser.session.close()


if __name__ == "__main__":
    unittest.main()

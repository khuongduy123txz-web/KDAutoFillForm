"""Dropdown batch regressions and a five-page mixed-control routed benchmark."""
import json
import os
import time
import unittest

import google_form_tool as tool
from google_fast import fill_google_page
from test_google_regression import public_data, item, entry


URL = "https://docs.google.com/forms/d/e/qa-five-mixed/viewform"


@unittest.skipUnless(os.environ.get("KD_BROWSER_TESTS") == "1", "Set KD_BROWSER_TESTS=1")
class DropdownBatchBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bot = tool.FormBrowser(headless=True).__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.bot.__exit__(None, None, None)

    def field(self, identifier, kind="dropdown", options=None):
        return {"id": "entry." + str(identifier), "question": "Question " + str(identifier),
                "kind": kind, "options": options or ["A", "B"]}

    def page(self, body):
        self.bot.new_session()
        self.bot.page.set_content('<form><div role=listitem><h3>Question 1</h3>' + body + '</div></form>')
        return self.bot.page

    def test_native_select_dispatches_controller_events_without_touching_hidden_values(self):
        page = self.page('<select required name="entry.1" oninput="window.inputEvent=this.value" '
                         'onchange="window.changeEvent=this.value"><option value="">Choose</option>'
                         '<option>A</option><option>B</option></select>'
                         '<input type=hidden name=unrelated value=untouched>')
        result = fill_google_page(page, [self.field(1)], {"entry.1": "B"})
        self.assertEqual(result["handled"], ["entry.1"], result)
        self.assertEqual(page.evaluate("[window.inputEvent,window.changeEvent]"), ["B", "B"])
        self.assertEqual(page.locator('[name=unrelated]').input_value(), "untouched")

    def test_native_disabled_and_ambiguous_options_are_not_selected(self):
        for options in ('<option disabled>B</option>', '<option>B</option><option>B</option>'):
            with self.subTest(options=options):
                page = self.page('<select name="entry.1"><option selected>A</option>' + options + '</select>')
                result = fill_google_page(page, [self.field(1)], {"entry.1": "B"})
                self.assertEqual(result["fallback"], ["entry.1"], result)
                self.assertEqual(page.locator('select').input_value(), "A")

    def custom(self, *, delayed=True, commit=True, duplicate=False):
        # Only the widget's actual click controller may update the entry input.
        body = '<input type=hidden name="entry.1" value=""><div role=listbox aria-expanded=false '
        body += 'onclick="if(event.target===this){this.setAttribute(\'aria-expanded\',this.getAttribute(\'aria-expanded\')===\'true\'?\'false\':\'true\');setTimeout(()=>this.querySelector(\'.menu\').hidden=this.getAttribute(\'aria-expanded\')!==\'true\',' + ('30' if delayed else '0') + ')}" '
        body += 'onkeydown="if(event.key===\'Escape\'){this.setAttribute(\'aria-expanded\',\'false\');this.querySelector(\'.menu\').hidden=true}">Choose<div class=menu hidden>'
        for value in (["A", "B", "B"] if duplicate else ["A", "B"]):
            action = "window.optionClicks=(window.optionClicks||0)+1;const box=this.closest('[role=listbox]');const selected=this;"
            if commit:
                action += "setTimeout(()=>{box.querySelectorAll('[role=option]').forEach(e=>e.setAttribute('aria-selected','false'));selected.setAttribute('aria-selected','true');box.parentElement.querySelector('input').value=selected.dataset.value;box.setAttribute('aria-expanded','false');box.querySelector('.menu').hidden=true},20)"
            body += '<div role=option data-value="' + value + '" aria-selected=false onclick="' + action + '">' + value + '</div>'
        return body + '</div></div>'

    def test_custom_dropdown_waits_for_popup_and_real_async_controller_commit(self):
        page = self.page(self.custom())
        result = fill_google_page(page, [self.field(1)], {"entry.1": "B"})
        self.assertEqual(result["handled"], ["entry.1"], result)
        self.assertEqual(page.locator('[name="entry.1"]').input_value(), "B")
        self.assertEqual(page.evaluate("window.optionClicks"), 1)
        self.assertEqual(page.locator('[role=listbox]').get_attribute('aria-expanded'), 'false')

    def test_ambiguous_popup_is_dismissed_without_clicking_any_option(self):
        page = self.page(self.custom(duplicate=True))
        result = fill_google_page(page, [self.field(1)], {"entry.1": "B"})
        self.assertEqual(result["fallback"], ["entry.1"], result)
        self.assertIsNone(page.evaluate("window.optionClicks"))
        self.assertEqual(page.locator('[role=listbox]').get_attribute('aria-expanded'), 'false')
        self.assertEqual(page.locator('[name="entry.1"]').input_value(), "")

    def test_custom_unverified_state_is_never_reported_filled_and_menu_is_dismissed(self):
        page = self.page(self.custom(commit=False))
        result = fill_google_page(page, [self.field(1)], {"entry.1": "B"})
        self.assertEqual(result["fallback"], ["entry.1"], result)
        self.assertEqual(page.locator('[name="entry.1"]').input_value(), "")
        self.assertEqual(page.locator('[role=listbox]').get_attribute('aria-expanded'), 'false')

    def test_controller_can_replace_clicked_item_with_verified_selected_display(self):
        page = self.page(self.custom())
        page.locator('[role=option][data-value=B]').evaluate('''option=>option.onclick=function(){
            const box=this.closest('[role=listbox]');
            const display=this.cloneNode(true);display.setAttribute('aria-selected','true');
            this.remove();box.parentElement.querySelector('input').value='B';
            box.append(display);box.querySelector('.menu').hidden=true;
            box.setAttribute('aria-expanded','false');
        }''')
        result = fill_google_page(page, [self.field(1)], {"entry.1": "B"})
        self.assertEqual(result['handled'], ['entry.1'], result)
        self.assertEqual(page.locator('[name="entry.1"]').input_value(), 'B')

    def test_five_pages_mixed_controls_preserve_all_answers_and_single_submission(self):
        items = []
        for page in range(5):
            if page:
                items.append([1000 + page, "Page " + str(page + 1), "", 8, None, -2])
            for offset, kind in enumerate((0, 3, 3, 4)):
                identifier = page * 4 + offset + 1
                items.append(item(identifier, "Question " + str(identifier), kind,
                                  [entry(identifier, None if kind == 0 else ["A", "B"])]))
        data = public_data(items)
        schema = tool.parse_schema(data, URL)
        answers = {f["id"]: ["A", "B"] if f["kind"] == "multiple" else "Text" if f["kind"] == "text" else "B"
                   for f in schema["fields"]}
        html = r'''<!doctype html><html><body><form onsubmit="return false">
        <input type=hidden name=pageHistory value=0><div id=questions></div>
        <button type=button id=next onclick="move()">Next</button>
        <button type=button id=send hidden onclick="finish()">Submit</button></form>
        <script>window.FB_PUBLIC_LOAD_DATA_=__DATA__;const fields=__FIELDS__;
        let index=0;window.saved={};window.transitions=0;
        function record(){const data=new FormData(document.querySelector('form'));
          for(const f of fields.filter(f=>f.page===index))saved[f.id]=f.kind==='multiple'?data.getAll(f.id):data.get(f.id)}
        function render(){questions.replaceChildren();
          for(const f of fields.filter(f=>f.page===index)){
            const card=document.createElement('div');card.setAttribute('role','listitem');
            const title=document.createElement('h3');title.textContent=f.question;card.append(title);
            if(f.kind==='text'){const input=document.createElement('input');input.name=f.id;input.required=true;card.append(input)}
            else if(f.kind==='multiple'){for(const value of f.options){const input=document.createElement('input');
              input.type='checkbox';input.name=f.id;input.value=value;input.setAttribute('aria-label',value);card.append(input)}}
            else if(Number(f.id.split('.')[1])%4===2){const select=document.createElement('select');select.name=f.id;select.required=true;
              for(const value of ['',...f.options]){const option=document.createElement('option');option.value=value;option.textContent=value||'Choose';select.append(option)}card.append(select)}
            else{const input=document.createElement('input');input.type='hidden';input.name=f.id;card.append(input);
              const box=document.createElement('div');box.setAttribute('role','listbox');box.setAttribute('aria-expanded','false');box.textContent='Choose';
              const menu=document.createElement('div');menu.hidden=true;
              box.onclick=event=>{if(event.target===box){menu.hidden=!menu.hidden;box.setAttribute('aria-expanded',String(!menu.hidden))}};
              box.onkeydown=event=>{if(event.key==='Escape'){menu.hidden=true;box.setAttribute('aria-expanded','false')}};
              for(const value of f.options){const option=document.createElement('div');option.textContent=value;option.dataset.value=value;
                option.setAttribute('role','option');option.setAttribute('aria-selected','false');option.onclick=()=>{
                  menu.querySelectorAll('[role=option]').forEach(e=>e.setAttribute('aria-selected','false'));
                  option.setAttribute('aria-selected','true');input.value=value;menu.hidden=true;box.setAttribute('aria-expanded','false')};menu.append(option)}
              box.append(menu);card.append(box)}questions.append(card)}next.hidden=index===4;send.hidden=index!==4}
        function move(){if(!document.querySelector('form').checkValidity())return;record();index++;transitions++;
          document.querySelector('[name=pageHistory]').value+=','+index;render()}
        async function finish(){if(!document.querySelector('form').checkValidity())return;record();
          await fetch(location.href.replace('/viewform','/formResponse'),{method:'POST',body:JSON.stringify(saved)});
          document.body.innerHTML='<div class=vHW8K>Recorded test response.</div>'}render();</script></body></html>'''
        html = html.replace('__DATA__', json.dumps(data)).replace('__FIELDS__', json.dumps(schema["fields"]))
        submissions = []
        browser = tool.FormBrowser(headless=True)
        browser.browser = self.bot.browser
        original = browser.new_session
        def routed():
            original()
            def route(request):
                if request.request.method == 'POST':
                    submissions.append(json.loads(request.request.post_data))
                    request.fulfill(status=200, content_type='text/plain', body='ok')
                else:
                    request.fulfill(status=200, content_type='text/html; charset=utf-8', body=html)
            browser.session.route('**/*', route)
        browser.new_session = routed
        try:
            started = time.perf_counter()
            report = browser.prepare(schema, answers)
            self.assertEqual(report, {"pages": 5, "page_indices": [0, 1, 2, 3, 4], "questions": 20, "fields": 20})
            self.assertEqual(submissions, [])
            self.assertEqual(browser.page.evaluate('window.transitions'), 4)
            self.assertTrue(browser.submit(schema))
            self.assertEqual(submissions, [answers])
            self.assertEqual(browser.metrics['batch_fields'], 20)
            self.assertEqual(browser.metrics['fallback_fields'], 0)
            print('FIVE_PAGE_MIXED_ROUTED_BENCHMARK ' + json.dumps({
                'seconds': round(time.perf_counter() - started, 3), 'metrics': browser.metrics}), flush=True)
        finally:
            if browser.session:
                browser.session.close()


if __name__ == '__main__':
    unittest.main()

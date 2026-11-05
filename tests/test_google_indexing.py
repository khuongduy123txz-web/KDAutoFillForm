"""DOM indexing correctness and browser round-trip regression checks."""
import os
import unittest
from unittest.mock import patch

import google_form_tool as tool
from google_fast import fill_google_page


@unittest.skipUnless(os.environ.get("KD_BROWSER_TESTS") == "1", "Set KD_BROWSER_TESTS=1")
class GoogleIndexingBrowserTests(unittest.TestCase):
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

    @staticmethod
    def field(identifier, question=None, kind="text", options=None):
        return {"id": "entry." + str(identifier), "question": question or "Question " + str(identifier),
                "kind": kind, "options": options or []}

    @staticmethod
    def card(question, html, heading=True):
        title = "<h3>" + question + "</h3>" if heading else question
        return "<div role=listitem>" + title + html + "</div>"

    @staticmethod
    def observe_card_text(page):
        page.evaluate("""() => {
            window.cardTextReads=0;
            const original=Object.getOwnPropertyDescriptor(HTMLElement.prototype,'innerText');
            Object.defineProperty(HTMLElement.prototype,'innerText',{
                configurable:true,
                get(){if(this.getAttribute('role')==='listitem')window.cardTextReads++;
                      return original.get.call(this)},
                set(value){original.set.call(this,value)}
            });
        }""")

    def test_many_named_questions_use_one_browser_call_without_reading_card_layout_text(self):
        fields = [self.field(index) for index in range(1, 101)]
        answers = {field["id"]: "Value " + str(index) for index, field in enumerate(fields)}
        html = "".join(self.card(field["question"], '<input name="' + field["id"] + '">') for field in fields)
        page = self.page(html + '<input type=hidden name=pageHistory value=0><button type=button>Next</button>')
        self.observe_card_text(page)
        with patch.object(page, "evaluate", wraps=page.evaluate) as evaluate, \
                patch.object(page, "wait_for_function", wraps=page.wait_for_function) as wait:
            result = fill_google_page(page, fields, answers)
        self.assertEqual(evaluate.call_count, 1)
        wait.assert_not_called()
        self.assertEqual(result["handled"], list(answers))
        self.assertEqual(result["fallback"], [])
        self.assertEqual(page.evaluate("window.cardTextReads"), 0)
        self.assertEqual(result["navigation"], {"history": "0", "next": True, "submit": False})
        actual = page.locator("input:not([type=hidden])").evaluate_all("els=>Object.fromEntries(els.map(el=>[el.name,el.value]))")
        self.assertEqual(actual, answers)

    def test_unnamed_title_prefixes_and_heading_priority_remain_ambiguous(self):
        page = self.page(self.card("Question", "<input>") +
                         self.card("Question long", "<input>") +
                         self.card("Question", "<input>", heading=False))
        self.observe_card_text(page)
        fields = [self.field(1, "Question"), self.field(2, "Question long")]
        result = fill_google_page(page, fields, {"entry.1": "Ambiguous", "entry.2": "Specific"})
        self.assertEqual(result["handled"], ["entry.2"], result)
        self.assertEqual(result["fallback"], ["entry.1"], result)
        self.assertEqual(page.locator("input").evaluate_all("els=>els.map(el=>el.value)"), ["", "Specific", ""])
        self.assertEqual(page.evaluate("window.cardTextReads"), 3)

    def test_duplicate_titles_resolve_only_their_verified_parameter_identifiers(self):
        def control(identifier):
            return ('<span data-params="[' + str(identifier) + ']"></span>'
                    '<div role=radio aria-label=Yes aria-checked=false onclick="'
                    "this.setAttribute('aria-checked','true');"
                    "const input=document.createElement('input');input.type='hidden';"
                    "input.name='entry." + str(identifier) + "';input.value='Yes';"
                    'this.parentElement.append(input)">Yes</div>')
        page = self.page(self.card("Repeated", control(1111)) + self.card("Repeated", control(2222)))
        fields = [self.field(1111, "Repeated", "single", ["Yes"]), self.field(2222, "Repeated", "single", ["Yes"])]
        answers = {"entry.1111": "Yes", "entry.2222": "Yes"}
        result = fill_google_page(page, fields, answers)
        self.assertEqual(result["handled"], list(answers), result)
        self.assertEqual(page.locator("input").evaluate_all("els=>Object.fromEntries(els.map(el=>[el.name,el.value]))"), answers)

    def test_duplicate_parameter_and_title_never_choose_a_card_by_dom_order(self):
        body = '<span data-params="[1111]"></span><input>'
        page = self.page(self.card("Repeated", body) + self.card("Repeated", body))
        result = fill_google_page(page, [self.field(1111, "Repeated")], {"entry.1111": "Wrong"})
        self.assertEqual(result["fallback"], ["entry.1111"], result)
        self.assertEqual(page.locator("input").evaluate_all("els=>els.map(el=>el.value)"), ["", ""])

    def test_numeric_parameter_collision_cannot_override_a_different_question(self):
        page = self.page(self.card("Wanted", '<input>') +
                         self.card("Different", '<span data-params="[1111]"></span><input>'))
        result = fill_google_page(page, [self.field(1111, "Wanted")], {"entry.1111": "Correct"})
        self.assertEqual(result["handled"], ["entry.1111"], result)
        self.assertEqual(page.locator("input").evaluate_all("els=>els.map(el=>el.value)"), ["Correct", ""])

    def test_nested_native_choice_is_pruned_without_pairwise_contains_checks(self):
        page = self.page(self.card("Wrapped",
            '<div role=checkbox aria-label=A aria-checked=false onclick="window.outerClicks=(window.outerClicks||0)+1;'
            "this.setAttribute('aria-checked','true');this.querySelector('input').checked=true"
            '"><input type=checkbox name=entry.1 value=A onclick="window.innerClicks=(window.innerClicks||0)+1"></div>'))
        page.evaluate("""() => {
            window.containsCalls=0;
            const original=Node.prototype.contains;
            Node.prototype.contains=function(other){window.containsCalls++;return original.call(this,other)};
        }""")
        result = fill_google_page(page, [self.field(1, "Wrapped", "multiple", ["A"])], {"entry.1": ["A"]})
        self.assertEqual(result["handled"], ["entry.1"], result)
        self.assertEqual(page.evaluate("[window.outerClicks,window.innerClicks||0,window.containsCalls]"), [1, 0, 0])
        self.assertTrue(page.locator("input").is_checked())

    def test_large_choice_list_never_compares_every_control_pair(self):
        values = [str(index) for index in range(300)]
        controls = '<input type=hidden name=entry.1_sentinel>' + "".join(
            '<input type=checkbox name=entry.1 value="' + value + '" aria-label="' + value + '">'
            for value in values)
        page = self.page(self.card("Many choices", controls))
        page.evaluate("""() => {
            window.containsCalls=0;
            const original=Node.prototype.contains;
            Node.prototype.contains=function(other){window.containsCalls++;return original.call(this,other)};
        }""")
        result = fill_google_page(page, [self.field(1, "Many choices", "multiple", values)], {"entry.1": values})
        self.assertEqual(result["handled"], ["entry.1"], result)
        self.assertEqual(page.locator("input:checked").count(), len(values))
        self.assertEqual(page.evaluate("window.containsCalls"), 0)

    def test_multiset_verification_rejects_duplicate_serialized_values(self):
        controls = "".join('<div role=checkbox aria-label="' + value + '" aria-checked=false onclick="'
            "this.setAttribute('aria-checked','true');const input=document.createElement('input');"
            "input.type='hidden';input.name='entry.1';input.value='A';this.parentElement.append(input)"
            '">' + value + '</div>' for value in ["A", "B"])
        page = self.page(self.card("Multiple", controls))
        result = fill_google_page(page, [self.field(1, "Multiple", "multiple", ["A", "B"])],
                                  {"entry.1": ["A", "B"]})
        self.assertEqual(result["fallback"], ["entry.1"], result)
        self.assertEqual(page.locator("input").evaluate_all("els=>els.map(el=>el.value)"), ["A", "A"])

    def test_email_and_navigation_are_verified_in_the_same_evaluation_without_clicking_next(self):
        page = self.page(self.card("Name", "<input name=entry.1>") +
            '<input type=email required name=emailAddress>'
            '<input type=hidden name=pageHistory value="0,1"><button disabled type=button onclick="window.moved=true">Next</button>')
        page.locator("[name=emailAddress]").evaluate("""el => el.oninput=function(){
            window.emailEvent=this.value;document.querySelector('button').disabled=!this.validity.valid;
        }""")
        result = fill_google_page(page, [self.field(1, "Name")], {"entry.1": "Student"}, email="synthetic@example.com")
        self.assertTrue(result["email_handled"])
        self.assertEqual(result["navigation"], {"history": "0,1", "next": True, "submit": False})
        self.assertEqual(page.locator("[name=emailAddress]").input_value(), "synthetic@example.com")
        self.assertEqual(page.evaluate("window.emailEvent"), "synthetic@example.com")
        self.assertIsNone(page.evaluate("window.moved"))

    def test_readonly_email_uses_fallback_flag_without_modifying_control(self):
        page = self.page(self.card("Name", "<input name=entry.1>") + '<input type=email readonly name=emailAddress>')
        result = fill_google_page(page, [self.field(1, "Name")], {"entry.1": "Student"}, email="synthetic@example.com")
        self.assertFalse(result["email_handled"])
        self.assertEqual(page.locator("[name=emailAddress]").input_value(), "")


if __name__ == "__main__":
    unittest.main()

import copy
import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import google_form_tool as tool
import html_forms as html


URL = "https://survey.example/survey"


class HtmlSecurityTests(unittest.TestCase):
    def test_private_addresses_credentials_and_non_https_are_rejected(self):
        for url in ("http://example.com/survey", "https://localhost/x", "https://127.0.0.1/x",
                    "https://169.254.169.254/latest", "https://[::1]/x", "https://10.0.0.1/x",
                    "https://user:pass@example.com/x", "https://example.com:8080/x"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                html.normalize_html_url(url)
        with patch.object(html.socket, "getaddrinfo", return_value=[(None, None, None, None, ("192.168.1.2", 443))]):
            with self.assertRaises(ValueError):
                html.normalize_html_url(URL)

    def test_generic_form_requires_explicit_opt_in(self):
        with self.assertRaises(tool.FormError):
            tool.form_link(URL)
        with patch.object(html, "_public_addresses", return_value=["93.184.216.34"]):
            self.assertEqual(tool.form_link(URL + "#fragment", allow_generic=True), URL)
            self.assertEqual(tool.form_browser(URL).__name__, "HtmlFormsBrowser")

    def test_transport_pins_public_address_preserves_cookie_and_refuses_redirect(self):
        request = SimpleNamespace(url=URL, method="GET", post_data_buffer=None,
                                  all_headers=lambda: {"cookie": "csrf=fixture", "host": "evil.example"})
        connection = Mock()
        connection.getresponse.return_value = SimpleNamespace(status=302, getheaders=lambda: [("Location", "https://127.0.0.1/")])
        with patch.object(html, "_public_addresses", return_value=["93.184.216.34"]), \
             patch.object(html, "_PinnedHTTPSConnection", return_value=connection) as transport:
            with self.assertRaisesRegex(ValueError, "URL"):
                html._fetch_public(request)
        transport.assert_called_once_with("survey.example", "93.184.216.34")
        self.assertEqual(connection.request.call_args.kwargs["headers"]["cookie"], "csrf=fixture")
        self.assertNotIn("host", connection.request.call_args.kwargs["headers"])
        connection.close.assert_called_once()


FORM = """<!doctype html><meta charset=utf-8><title>Survey fixture</title><h1>Survey fixture</h1>
<form action='/save' method='post'><input type=hidden name=csrf value=fixture>
<label>Full name<input name=full_name required></label>
<label>Age<input name=age type=number required></label>
<label>Email<input name=email type=email required></label>
<label>Feedback<textarea name=feedback></textarea></label>
<fieldset><legend>Do you agree?</legend><label><input type=radio name=agree value=yes required>Yes</label><label><input type=radio name=agree value=no>No</label></fieldset>
<fieldset><legend>Tools</legend><label><input type=checkbox name=tools value=a>Excel</label><label><input type=checkbox name=tools value=b>SPSS</label></fieldset>
<label>City<select name=city><option value=''>Choose</option><option value=hanoi>Hanoi</option><option value=hue>Hue</option></select></label>
<label>Birth date<input type=date name=birth_date></label><label>Time<input type=time name=meeting_time></label>
<button type=submit>Submit</button></form>"""


@unittest.skipUnless(os.environ.get("KD_BROWSER_TESTS") == "1", "Set KD_BROWSER_TESTS=1 with Chromium installed")
class HtmlBrowserTests(unittest.TestCase):
    def setUp(self):
        self.posts = []
        self.content = FORM
        self.status = 200
        self.patch_dns = patch.object(html, "_public_addresses", return_value=["93.184.216.34"])
        self.patch_dns.start()
        self.addCleanup(self.patch_dns.stop)
        self.patch_transport = patch.object(html, "_fetch_public", side_effect=self.fetch)
        self.patch_transport.start()
        self.addCleanup(self.patch_transport.stop)
        self.browser = tool.form_browser(URL)(headless=True).__enter__()
        self.addCleanup(self.browser.__exit__, None, None, None)

    def fetch(self, request):
        if request.method == "POST":
            self.posts.append((request.url, request.post_data))
            return self.status, {"content-type": "text/html; charset=utf-8"}, b'<p data-submission-success="true">Your response was recorded.</p>'
        return 200, {"content-type": "text/html; charset=utf-8"}, self.content.encode()

    def prepare(self):
        schema = self.browser.load(URL)
        sampler = tool.Sampler(schema, 42)
        answers = sampler.sample(1)
        self.browser.profile = sampler.last_profile
        report = self.browser.prepare(schema, answers)
        return schema, report

    def test_native_controls_prepare_all_questions_without_submission(self):
        schema, report = self.prepare()
        self.assertEqual(schema["provider"], "html")
        self.assertEqual(report["fields"], 9)
        self.assertEqual(report["pages"], 1)
        self.assertEqual(self.posts, [])
        self.assertTrue(self.browser.page.locator('[name="full_name"]').input_value())
        self.assertTrue(self.browser.page.locator('[name="age"]').input_value())
        self.assertEqual(self.browser.page.locator("form").evaluate("f=>f.checkValidity()"), True)

    def test_submit_posts_once_and_waits_for_explicit_confirmation(self):
        schema, _ = self.prepare()
        self.assertTrue(self.browser.submit(schema))
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.posts[0][0], "https://survey.example/save")
        self.assertIn("csrf=fixture", self.posts[0][1])

    def test_failed_post_is_not_confirmed(self):
        schema, _ = self.prepare()
        self.status = 400
        with self.assertRaises(tool.FormError):
            self.browser.submit(schema)
        self.assertEqual(len(self.posts), 1)

    def test_unknown_widgets_uploads_multipage_and_hidden_questions_stop(self):
        for addition in ("<input name=file type=file>", "<input name=password type=password>",
                         "<button type=button>Next</button>", "<input name=hidden_question style='display:none'>"):
            self.content = FORM.replace("</form>", addition + "</form>")
            with self.subTest(addition=addition), self.assertRaises(tool.FormError):
                self.browser.load(URL)
        self.assertEqual(self.posts, [])

    def test_changed_action_is_detected_before_sending(self):
        schema = self.browser.load(URL)
        answers = tool.Sampler(schema, 42).sample(1)
        self.content = FORM.replace("/save", "/different-endpoint")
        with self.assertRaises(tool.FormError):
            self.browser.prepare(schema, answers)
        self.assertEqual(self.posts, [])


if __name__ == "__main__":
    unittest.main()

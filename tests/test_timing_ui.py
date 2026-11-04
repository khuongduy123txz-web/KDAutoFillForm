"""Browser UI checks against a local server and fake submissions only."""
import hashlib
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import google_form_tool as tool
import test_workflow as workflow


@unittest.skipUnless(os.environ.get("KD_BROWSER_TESTS") == "1", "Set KD_BROWSER_TESTS=1")
class TimingInterfaceTests(unittest.TestCase):
    def setUp(self):
        workflow.WorkflowTests.setUp(self)
        self.gate = threading.Event()
        self.entered = threading.Event()
        original = tool.FormBrowser
        test = self

        class Browser(original):
            def _prepare(self, schema, answers):
                test.entered.set()
                if not test.gate.wait(5):
                    raise AssertionError("Fake browser preparation gate timed out")
                time.sleep(0.025)
                return super()._prepare(schema, answers)

            def submit(self, schema):
                time.sleep(0.02)
                return super().submit(schema)

            def __exit__(self, *args):
                time.sleep(0.025)
                return super().__exit__(*args)

        mock = patch.object(tool, "FormBrowser", Browser)
        mock.start()
        self.addCleanup(mock.stop)
        local = {"KD_CLOUD": "0", "RENDER": "", "KD_PUBLIC_URL": "", "RENDER_EXTERNAL_URL": "",
                 "KD_ALLOWED_ORIGINS": "", "KD_HOST": "", "PORT": "0"}
        with patch.dict(os.environ, local), patch.object(tool, "WebApplication", return_value=self.app):
            self.server = tool.create_web_server(port=0, host="127.0.0.1", key_store=self.store)
        self.serving = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.02), daemon=True)
        self.serving.start()

        def close_server():
            self.gate.set()
            self.server.shutdown()
            self.server.server_close()
            self.serving.join(timeout=1)
        self.addCleanup(close_server)

        from playwright.sync_api import sync_playwright
        self.runtime = sync_playwright().start()
        self.addCleanup(self.runtime.stop)
        self.browser = self.runtime.chromium.launch(headless=True)
        self.addCleanup(self.browser.close)
        self.context = self.browser.new_context(viewport={"width": 1365, "height": 900})
        self.base = "http://127.0.0.1:" + str(self.server.server_port)
        raw_guest = tool.secrets.token_urlsafe(32)
        self.guest = hashlib.sha256(raw_guest.encode()).hexdigest()
        self.store.ensure_guest(self.guest)
        self.app.scans["scan"]["owner_id"] = self.guest
        self.context.add_cookies([{"name": "kd_guest", "value": raw_guest, "url": self.base}])
        self.context.add_init_script("""if (!localStorage.getItem('formpilot.scan')) {
            localStorage.setItem('formpilot.scan', 'scan');
            localStorage.setItem('formpilot.url', 'https://docs.google.com/forms/d/e/workflow/viewform');
            localStorage.setItem('formpilot.count', '2');
        }""")
        # There is no network access to any real form in this test.
        self.context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(self.base + "/")
                           else route.abort())
        self.page = self.context.new_page()
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.goto(self.base)
        self.page.wait_for_function("!document.querySelector('#startButton').disabled")
        if self.page.locator("#autoRun").is_checked():
            self.page.locator("label[for=autoRun]").click()
        self.addCleanup(self.gate.set)

    def test_presets_persistence_payload_actual_measurements_and_mobile_layout(self):
        page = self.page
        for preset, expected in (("maximum", "0"), ("fast", "2"), ("fixed60", "60")):
            page.locator("#timingPreset").select_option(preset)
            self.assertEqual(page.locator("#intervalMin").input_value(), expected)
            self.assertEqual(page.locator("#intervalMax").input_value(), expected)
        page.locator("#timingBasis").select_option("completion")
        page.reload()
        page.wait_for_function("!document.querySelector('#startButton').disabled")
        self.assertEqual(page.locator("#timingBasis").input_value(), "completion")
        self.assertEqual(page.locator("#timingPreset").input_value(), "fixed60")
        self.assertEqual(page.locator("#intervalMin").input_value(), "60")
        if page.locator("#autoRun").is_checked():
            page.locator("label[for=autoRun]").click()
        page.locator("#timingPreset").select_option("custom_random")
        page.locator("#intervalMin").fill("30")
        page.locator("#intervalMax").fill("180")
        with page.expect_request(lambda request: request.method == "POST" and request.url.endswith("/api/jobs")) as sent:
            page.locator("#startButton").click()
        self.assertEqual(sent.value.post_data_json["schedule"],
                         {"mode": "random", "min_seconds": 30, "max_seconds": 180, "basis": "completion"})
        self.assertTrue(self.entered.wait(1))
        page.wait_for_function("document.querySelector('#timingPreset').disabled")
        for node in ("#timingPreset", "#timingBasis", "#intervalMin", "#intervalMax", "#quantity"):
            self.assertTrue(page.locator(node).is_disabled(), node)
        page.locator("#startButton").click()
        self.gate.set()
        page.wait_for_function("state.job && state.job.status === 'stopped'")

        page.locator("#timingPreset").select_option("maximum")
        page.locator("#timingBasis").select_option("start")
        page.locator("#quantity").fill("2")
        page.locator("#startButton").click()
        page.wait_for_function("state.job && state.job.status === 'completed'", timeout=15000)
        displayed = page.locator("#responseTiming").inner_text()
        for text in ("Xử lý:", "Hai lần bắt đầu:", "Hai xác nhận:"):
            self.assertIn(text, displayed)
        state = page.evaluate("state.job")
        self.assertEqual(state["confirmed"], 2)
        self.assertEqual(state["schedule"], {"mode": "fixed", "min_seconds": 0.0,
                                             "max_seconds": 0.0, "basis": "start"})
        self.assertGreaterEqual(state["last_response_seconds"], 0.06)
        self.assertGreater(state["last_start_interval_seconds"], 0)
        self.assertGreater(state["last_confirmation_interval_seconds"], 0)
        self.assertFalse(page.locator("#timingPreset").is_disabled())
        estimate = page.locator("#durationEstimate")
        self.assertIn("Ước tính theo tốc độ vừa đo:", estimate.inner_text())
        # Changing only the engine must discard the previous engine's ETA.
        page.locator("#engineMode").select_option("compatible")
        self.assertIn("Tổng khoảng chờ/nhịp tối thiểu:", estimate.inner_text())
        # A new scan must also discard that ETA, even after the engine matches.
        page.evaluate("state.scanId = 'another-form-scan'; updateRunOptions()")
        page.locator("#engineMode").select_option("fast")
        self.assertIn("Tổng khoảng chờ/nhịp tối thiểu:", estimate.inner_text())
        page.evaluate("state.scanId = state.job.scan_id; updateRunOptions()")
        self.assertIn("Ước tính theo tốc độ vừa đo:", estimate.inner_text())
        page.set_viewport_size({"width": 390, "height": 844})
        page.locator("#jobPanel").scroll_into_view_if_needed()
        page.wait_for_timeout(150)
        size = page.evaluate("({width:document.documentElement.clientWidth, scroll:document.documentElement.scrollWidth})")
        self.assertLessEqual(size["scroll"], size["width"], size)
        screenshot = Path(tempfile.gettempdir()) / "kdautoform_timing_ui_mobile.png"
        page.screenshot(path=str(screenshot), full_page=True)
        self.assertEqual(self.errors, [])
        print("Timing UI screenshot:", screenshot)


if __name__ == "__main__":
    unittest.main()

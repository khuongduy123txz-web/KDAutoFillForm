import hashlib
import http.client
from http.cookies import SimpleCookie
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import google_form_tool as tool


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.raw_key = tool.create_access_key()
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.settings = dict(tool.live_settings())
        self.settings["keys"] = {"qa": {"sha256": tool.access_key_hash(self.raw_key), "enabled": True}}
        self.store = tool.KeyStore(Path(self.folder.name) / "keys.sqlite3",
                                   export_dir=Path(self.folder.name) / "vault", legacy=self.settings["keys"])
        mock_settings = patch.object(tool, "live_settings", return_value=self.settings)
        mock_settings.start()
        self.addCleanup(mock_settings.stop)
        self.cookies = {}
        self.server = self.launch(cloud=True)
        status, _, html = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.token = re.search(r'const TOKEN="([^"]+)"', html).group(1)
        self.guest_id = hashlib.sha256(self.cookies["kd_guest"].encode()).hexdigest()

    def launch(self, cloud):
        env = {"KD_CLOUD": "1" if cloud else "0", "RENDER": "",
               "KD_PUBLIC_URL": "https://kdautoform.example" if cloud else "",
               "RENDER_EXTERNAL_URL": "", "KD_ALLOWED_ORIGINS": "",
               "KD_HOST": "", "PORT": "0"}
        with patch.dict(os.environ, env):
            server = tool.create_web_server(key_store=self.store)
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.02), daemon=True)
        thread.start()
        def close():
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.addCleanup(close)
        return server

    def request(self, method, path, payload=None, origin="https://kdautoform.example",
                server=None, token=None, cookie=None, update_cookies=True):
        server = server or self.server
        headers = {}
        body = None
        if payload is not None:
            body = json.dumps(payload).encode()
            headers.update({"Content-Type": "application/json", "Origin": origin,
                            "X-Local-Token": token or self.token})
        cookie = "; ".join(f"{name}={value}" for name, value in self.cookies.items()) if cookie is None else cookie
        if cookie:
            headers["Cookie"] = cookie
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        try:
            connection.request(method, path, body, headers)
            response = connection.getresponse()
            status, header_items, content = response.status, response.getheaders(), response.read().decode()
        finally:
            connection.close()
        result_headers = dict(header_items)
        if update_cookies:
            for name, value in header_items:
                if name.lower() == "set-cookie":
                    parsed = SimpleCookie()
                    parsed.load(value)
                    for cookie_name, morsel in parsed.items():
                        if morsel["max-age"] == "0":
                            self.cookies.pop(cookie_name, None)
                        else:
                            self.cookies[cookie_name] = morsel.value
        data = json.loads(content) if result_headers.get("Content-Type", "").startswith("application/json") else content
        return status, result_headers, data

    def login(self):
        status, headers, data = self.request("POST", "/api/auth/login", {"key": self.raw_key})
        self.assertEqual(status, 200, data)
        return headers

    def seed_scan(self, count=1, mode="send"):
        schema = {"title": "Test", "url": "https://docs.google.com/forms/d/e/test/viewform",
                  "branching": False, "pages": [{"index": 0}], "question_count": 1,
                  "fields": [{"id": "entry.1", "question": "Name", "kind": "text", "options": [],
                              "page": 0, "rules": [], "exclusive_columns": False}]}
        config = tool.default_web_config(schema, 42)
        self.server.application.scans["one-scan"] = {
            "id": "one-scan", "status": "ready", "schema": schema, "config": config,
            "owner_id": self.guest_id}
        return {"scan_id": "one-scan", "count": count, "mode": mode, "config": config, "delay": 0}

    def wait_job(self, identifier):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            job = self.server.application.job_state(identifier)
            if job["status"] not in ("queued", "running", "stopping"):
                return job
            time.sleep(0.01)
        self.fail("Mock worker did not finish")

    def run_stubbed(self, payload):
        def complete(job):
            job.update(status="completed")
        with patch.object(self.server.application, "_run_job", side_effect=complete):
            status, _, data = self.request("POST", "/api/jobs", payload)
            self.assertEqual(status, 202, data)
            job = self.wait_job(data["job_id"])
        return job

    def run_fake_browser(self, payload, prepare_error=None, submit_error=None):
        calls = []
        class Browser:
            def __init__(self, headless):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def _prepare(self, schema, answers):
                if prepare_error:
                    raise tool.FormError(prepare_error)
                return {"page_indices": [0], "questions": 1, "pages": 1}
            def submit(self, schema):
                calls.append("submit")
                if submit_error:
                    raise tool.FormError(submit_error)
        sampler = SimpleNamespace(last_profile={}, sample=lambda index: {})
        with patch.object(tool, "FormBrowser", Browser), patch.object(tool, "WebRatioSampler", return_value=sampler), \
                patch.object(tool, "validate_answers"), patch.object(tool, "RunLog"):
            status, _, data = self.request("POST", "/api/jobs", payload)
            self.assertEqual(status, 202, data)
            job = self.wait_job(data["job_id"])
        return job, calls

    def test_home_creates_guest_and_free_state_without_key(self):
        self.assertEqual(self.request("GET", "/healthz")[0], 200)
        status, _, state = self.request("GET", "/api/state")
        self.assertEqual(status, 200, state)
        self.assertEqual(state["plan"], "free")
        self.assertEqual((state["free_limit"], state["remaining"], state["used"]), (10, 10, 0))
        self.assertFalse(state["locked"])
        self.assertFalse(self.request("GET", "/api/auth/status")[2]["authenticated"])
        self.assertEqual(self.server.server_address[0], "0.0.0.0")

    def test_refresh_does_not_reset_free_quota(self):
        before = self.cookies["kd_guest"]
        for _ in range(3):
            self.store.consume_guest(self.guest_id)
        self.assertEqual(self.request("GET", "/")[0], 200)
        self.assertEqual(self.cookies["kd_guest"], before)
        self.assertEqual(self.request("GET", "/api/state")[2]["remaining"], 7)
        server = self.launch(cloud=True)
        self.assertEqual(self.request("GET", "/api/state", server=server)[2]["remaining"], 7)

    def test_https_login_cookie_and_logout_return_to_free_plan(self):
        self.store.consume_guest(self.guest_id)
        headers = self.login()
        for attribute in ("; Secure", "; HttpOnly", "SameSite=Strict"):
            self.assertIn(attribute, headers["Set-Cookie"])
        self.assertEqual(self.request("GET", "/api/state")[2]["plan"], "unlimited")
        self.request("GET", "/")
        self.assertEqual(self.request("GET", "/api/state")[2]["plan"], "unlimited")
        status, headers, _ = self.request("POST", "/api/auth/logout", {})
        self.assertEqual(status, 200)
        self.assertIn("Max-Age=0", headers["Set-Cookie"])
        state = self.request("GET", "/api/state")[2]
        self.assertEqual((state["plan"], state["remaining"]), ("free", 9))
        self.login()

    def test_public_api_accepts_configured_origin_and_rejects_others(self):
        for origin in ("https://unrelated.example", "https://kdautoform.example.evil.example", "http://localhost:8765"):
            with self.subTest(origin=origin):
                self.assertEqual(self.request("POST", "/api/auth/login", {"key": self.raw_key}, origin=origin)[0], 403)
        self.assertEqual(self.request("POST", "/api/auth/login", {"key": "invalid"})[0], 401)
        self.login()

    def test_local_cookie_has_no_secure_attribute(self):
        server = self.launch(cloud=False)
        _, headers, html = self.request("GET", "/", server=server, cookie="", update_cookies=False)
        self.assertNotIn("; Secure", headers["Set-Cookie"])
        token = re.search(r'const TOKEN="([^"]+)"', html).group(1)
        status, headers, data = self.request("POST", "/api/auth/login", {"key": self.raw_key},
                                           origin=f"http://localhost:{server.server_port}", server=server, token=token)
        self.assertEqual(status, 200, data)
        self.assertNotIn("; Secure", headers["Set-Cookie"])

    def test_free_rejects_unbounded_and_over_remaining_counts(self):
        for _ in range(8):
            self.store.consume_guest(self.guest_id)
        payload = self.seed_scan()
        for count in (0, 3, 100):
            with self.subTest(count=count), patch.object(self.server.application, "run_job") as worker:
                status, _, _ = self.request("POST", "/api/jobs", {**payload, "count": count})
                self.assertGreaterEqual(status, 400)
                worker.assert_not_called()
        self.assertEqual(self.store.guest_usage(self.guest_id)["used"], 8)

    def test_preview_and_prepare_failure_do_not_consume_quota(self):
        preview, calls = self.run_fake_browser(self.seed_scan(mode="check"))
        self.assertEqual(preview["status"], "completed")
        self.assertEqual(calls, [])
        self.assertEqual(self.store.guest_usage(self.guest_id)["used"], 0)
        failed, calls = self.run_fake_browser(self.seed_scan(), prepare_error="prepare failed")
        self.assertEqual(failed["status"], "error")
        self.assertEqual(calls, [])
        self.assertEqual(self.store.guest_usage(self.guest_id)["used"], 0)

    def test_unconfirmed_submit_consumes_one_attempt(self):
        failed, calls = self.run_fake_browser(self.seed_scan(), submit_error="confirmation uncertain")
        self.assertEqual(failed["status"], "error")
        self.assertEqual(calls, ["submit"])
        self.assertEqual(self.store.guest_usage(self.guest_id)["used"], 1)

    def test_tenth_send_finishes_then_locks_new_work_but_keeps_results(self):
        for _ in range(9):
            self.store.consume_guest(self.guest_id)
        job, calls = self.run_fake_browser(self.seed_scan())
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["confirmed"], 1)
        self.assertEqual(calls, ["submit"])
        state = self.request("GET", "/api/state")[2]
        self.assertEqual((state["remaining"], state["used"], state["locked"]), (0, 10, True))
        status, _, denied = self.request("POST", "/api/jobs", self.seed_scan())
        self.assertEqual(status, 402, denied)
        self.assertTrue(denied["locked"])
        self.assertEqual(self.request("GET", "/api/jobs/" + job["id"])[0], 200)
        self.assertEqual(self.request("POST", "/api/jobs/" + job["id"] + "/stop", {})[0], 200)
        log_path = Path(self.folder.name) / "qa.jsonl"
        log_path.write_text('{"synthetic":true}\n', encoding="utf-8")
        self.server.application.jobs[job["id"]]["log_path"] = str(log_path)
        self.assertEqual(self.request("GET", "/api/jobs/" + job["id"] + "/download")[0], 200)
        self.assertEqual(self.request("POST", "/api/scan", {"url": "https://docs.google.com/forms/d/e/test/viewform"})[0], 402)

    def test_unlimited_key_unlocks_exhausted_guest_and_allows_multiple_batches(self):
        payload = self.seed_scan(count=100)
        for _ in range(10):
            self.store.consume_guest(self.guest_id)
        self.login()
        state = self.request("GET", "/api/state")[2]
        self.assertEqual(state["plan"], "unlimited")
        self.assertFalse(state["locked"])
        self.run_stubbed(payload)
        self.run_stubbed({**payload, "count": 0})
        self.login()
        self.assertEqual(self.store.guest_usage(self.guest_id)["used"], 10)

    def test_guest_cannot_access_another_guest_scan_or_spoof_owner(self):
        payload = self.seed_scan()
        other_token = tool.secrets.token_urlsafe(32)
        other_id = hashlib.sha256(other_token.encode()).hexdigest()
        self.store.ensure_guest(other_id)
        outsider_cookie = "kd_guest=" + other_token
        self.assertGreaterEqual(self.request("GET", "/api/scans/one-scan", cookie=outsider_cookie,
                                            update_cookies=False)[0], 400)
        malicious = {**payload, "_owner_id": self.guest_id, "_guest_id": self.guest_id}
        self.assertGreaterEqual(self.request("POST", "/api/jobs", malicious, cookie=outsider_cookie,
                                            update_cookies=False)[0], 400)

    def test_cloud_forces_headless_and_allows_scans_while_jobs_are_queued(self):
        payload = self.seed_scan(mode="check")
        with patch.object(self.server.application, "run_job", return_value=None):
            status, _, result = self.request("POST", "/api/jobs", {**payload, "headless": False})
        self.assertEqual(status, 202, result)
        job = self.request("GET", "/api/jobs/" + result["job_id"])[2]
        self.assertTrue(job["headless"])
        app = self.server.application
        with patch.object(app, "read_form_schema", return_value=app.scans["one-scan"]["schema"]):
            status, _, result = self.request("POST", "/api/scan", {"url": "https://docs.google.com/forms/d/e/test/viewform"})
            self.assertEqual(status, 202, result)
            deadline = time.monotonic() + 2
            while app.scan_state(result["scan_id"])["status"] == "scanning" and time.monotonic() < deadline:
                time.sleep(.005)
            scan = app.scan_state(result["scan_id"])
            self.assertEqual(scan["status"], "ready", scan)
            self.assertEqual(scan["owner_id"], self.guest_id)

    def test_custom_domain_and_render_default_url_work(self):
        with patch.dict(os.environ, {"KD_CLOUD": "1", "KD_PUBLIC_URL": "",
                                    "RENDER_EXTERNAL_URL": "https://kdautoform-test.onrender.com",
                                    "KD_ALLOWED_ORIGINS": "https://custom.example"}):
            server = tool.create_web_server(port=0, host="127.0.0.1", key_store=self.store)
        server.server_close()
        self.assertEqual(server.settings["public_url"], "https://kdautoform-test.onrender.com")
        self.assertIn("https://custom.example", server.settings["origins"])

    def test_invalid_cloud_configuration_reports_clear_error(self):
        base = {"KD_CLOUD": "1", "RENDER_EXTERNAL_URL": "", "KD_ALLOWED_ORIGINS": "",
                "KD_PUBLIC_URL": "https://kdautoform.example", "PORT": "invalid"}
        with patch.dict(os.environ, base), self.assertRaises(tool.FormError):
            tool.create_web_server(key_store=self.store)
        for public_url in ("", "https://kdautoform.example/api", "https://user:password@example.com"):
            with self.subTest(url=public_url), patch.dict(os.environ, {**base, "PORT": "0", "KD_PUBLIC_URL": public_url}):
                with self.assertRaises(tool.FormError):
                    tool.create_web_server(key_store=self.store)

    def test_pause_resume_and_compact_poll_preserve_guest_ownership(self):
        payload = self.seed_scan()
        app = self.server.application
        with patch.object(app, "run_job", return_value=None):
            status, _, created = self.request("POST", "/api/jobs", {**payload, "schedule": {
                "mode": "random", "min_seconds": 30, "max_seconds": 180}, "seed": 42})
        self.assertEqual(status, 202, created)
        identifier = created["job_id"]
        status, _, paused = self.request("POST", "/api/jobs/" + identifier + "/pause", {})
        self.assertEqual(status, 200, paused)
        self.assertTrue(paused["pause_requested"])
        self.assertTrue(app.jobs[identifier]["control"].paused)
        state = self.request("GET", "/api/jobs/" + identifier)[2]
        self.assertEqual((state["plan"], state["remaining"], state["seed"]), ("free", 10, 42))
        self.assertNotIn("counts", state)
        self.assertNotIn("control", state)
        other_raw = tool.secrets.token_urlsafe(32)
        self.store.ensure_guest(hashlib.sha256(other_raw.encode()).hexdigest())
        self.assertEqual(self.request("POST", "/api/jobs/" + identifier + "/resume", {},
                                      cookie="kd_guest=" + other_raw, update_cookies=False)[0], 400)
        self.assertFalse(self.request("POST", "/api/jobs/" + identifier + "/resume", {})[2]["pause_requested"])
        self.request("POST", "/api/jobs/" + identifier + "/stop", {})
        self.assertTrue(app.jobs[identifier]["control"].stopped)
        self.assertEqual(self.store.guest_usage(self.guest_id, fresh=True)["used"], 0)

    def test_bad_key_throttle_does_not_block_another_guest_on_same_proxy(self):
        for _ in range(6):
            self.assertEqual(self.request("POST", "/api/auth/login", {"key": "KD-invalid"})[0], 401)
        self.assertEqual(self.request("POST", "/api/auth/login", {"key": "KD-invalid"})[0], 429)
        raw = tool.secrets.token_urlsafe(32)
        self.store.ensure_guest(hashlib.sha256(raw.encode()).hexdigest())
        status, _, body = self.request("POST", "/api/auth/login", {"key": self.raw_key},
                                       cookie="kd_guest=" + raw, update_cookies=False)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["plan"], "unlimited")

    def test_ratio_preset_endpoint_does_not_charge_or_accept_unknown_presets(self):
        payload = self.seed_scan()
        status, _, result = self.request("POST", "/api/scans/one-scan/preset", {
            "preset": "balanced", "config": payload["config"]})
        self.assertEqual(status, 200, result)
        self.assertEqual(result["config"], payload["config"])
        self.assertEqual(self.request("POST", "/api/scans/one-scan/preset", {
            "preset": "unknown", "config": payload["config"]})[0], 400)
        self.assertEqual(self.store.guest_usage(self.guest_id, fresh=True)["used"], 0)


if __name__ == "__main__":
    unittest.main()

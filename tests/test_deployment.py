import http.client
import json
import os
from pathlib import Path
import re
import threading
import unittest
from unittest.mock import patch

import google_form_tool as tool


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.raw_key = tool.create_access_key()
        self.settings = dict(tool.live_settings())
        self.settings["keys"] = {"qa": {"sha256": tool.access_key_hash(self.raw_key), "enabled": True}}
        mock_settings = patch.object(tool, "live_settings", return_value=self.settings)
        mock_settings.start()
        self.addCleanup(mock_settings.stop)
        self.server = self.launch(cloud=True)
        status, _, html = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.token = re.search(r'const TOKEN="([^"]+)"', html).group(1)
        self.cookie = ""

    def launch(self, cloud):
        env = {
            "KD_CLOUD": "1" if cloud else "0", "RENDER": "",
            "KD_PUBLIC_URL": "https://kdautoform.example" if cloud else "",
            "RENDER_EXTERNAL_URL": "", "KD_ALLOWED_ORIGINS": "",
            "KD_HOST": "", "PORT": "0",
        }
        with patch.dict(os.environ, env):
            server = tool.create_web_server()
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.02), daemon=True)
        thread.start()

        def close():
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.addCleanup(close)
        return server

    def request(self, method, path, payload=None, origin="https://kdautoform.example", server=None, token=None):
        server = server or self.server
        headers = {}
        body = None
        if payload is not None:
            body = json.dumps(payload).encode()
            headers.update({"Content-Type": "application/json", "Origin": origin,
                            "X-Local-Token": token or self.token})
        if getattr(self, "cookie", ""):
            headers["Cookie"] = self.cookie
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        try:
            connection.request(method, path, body, headers)
            response = connection.getresponse()
            status, result_headers, content = response.status, dict(response.getheaders()), response.read().decode()
        finally:
            connection.close()
        data = json.loads(content) if result_headers.get("Content-Type", "").startswith("application/json") else content
        return status, result_headers, data

    def login(self):
        status, headers, data = self.request("POST", "/api/auth/login", {"key": self.raw_key})
        self.assertEqual(status, 200, data)
        self.cookie = headers["Set-Cookie"].split(";", 1)[0]
        return headers

    def test_health_is_public_but_app_state_requires_key(self):
        status, _, body = self.request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(self.request("GET", "/api/state")[0], 401)
        self.assertEqual(self.server.server_address[0], "0.0.0.0")

    def test_https_login_cookie_and_logout(self):
        headers = self.login()
        self.assertIn("; Secure", headers["Set-Cookie"])
        self.assertIn("; HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        status, _, state = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertTrue(state["headless_only"])
        status, headers, _ = self.request("POST", "/api/auth/logout", {})
        self.assertEqual(status, 200)
        self.assertIn("Max-Age=0", headers["Set-Cookie"])
        self.assertEqual(self.request("GET", "/api/state")[0], 401)

    def test_public_api_accepts_configured_origin_and_rejects_others(self):
        for origin in ("https://unrelated.example", "https://kdautoform.example.evil.example", "http://localhost:8765"):
            with self.subTest(origin=origin):
                self.assertEqual(self.request("POST", "/api/auth/login", {"key": self.raw_key}, origin=origin)[0], 403)
        self.assertEqual(self.request("POST", "/api/auth/login", {"key": "invalid"})[0], 401)
        self.login()

    def test_local_login_and_cookie_remain_compatible(self):
        server = self.launch(cloud=False)
        _, _, html = self.request("GET", "/", server=server)
        token = re.search(r'const TOKEN="([^"]+)"', html).group(1)
        status, headers, data = self.request("POST", "/api/auth/login", {"key": self.raw_key},
                                           origin=f"http://localhost:{server.server_port}", server=server, token=token)
        self.assertEqual(status, 200, data)
        self.assertNotIn("; Secure", headers["Set-Cookie"])

    def test_cloud_forces_headless_and_avoids_overlapping_browsers(self):
        self.login()
        app = self.server.application
        schema = {
            "title": "Test", "url": "https://docs.google.com/forms/d/e/test/viewform",
            "branching": False, "pages": [{"index": 0}],
            "fields": [{"id": "entry.1", "question": "Name", "kind": "text", "options": [],
                        "rules": [], "exclusive_columns": False}],
        }
        config = tool.default_web_config(schema, 42)
        app.scans["qa-scan"] = {"id": "qa-scan", "status": "ready", "schema": schema,
                                "config": config, "access_key_id": "qa"}
        with patch.object(app, "run_job", return_value=None):
            status, _, data = self.request("POST", "/api/jobs", {
                "scan_id": "qa-scan", "count": 1, "mode": "check", "headless": False, "config": config,
            })
        self.assertEqual(status, 202, data)
        status, _, job = self.request("GET", "/api/jobs/" + data["job_id"])
        self.assertEqual(status, 200)
        self.assertTrue(job["headless"])
        status, _, _ = self.request("POST", "/api/scan", {"url": schema["url"]})
        self.assertEqual(status, 400)

    def test_custom_domain_and_render_default_url_work(self):
        with patch.dict(os.environ, {"KD_CLOUD": "1", "KD_PUBLIC_URL": "",
                                      "RENDER_EXTERNAL_URL": "https://kdautoform-test.onrender.com",
                                      "KD_ALLOWED_ORIGINS": "https://custom.example"}):
            server = tool.create_web_server(port=0, host="127.0.0.1")
        server.server_close()
        self.assertEqual(server.settings["public_url"], "https://kdautoform-test.onrender.com")
        self.assertIn("https://custom.example", server.settings["origins"])

    def test_invalid_cloud_configuration_reports_clear_error(self):
        base = {"KD_CLOUD": "1", "RENDER_EXTERNAL_URL": "", "KD_ALLOWED_ORIGINS": "",
                "KD_PUBLIC_URL": "https://kdautoform.example", "PORT": "invalid"}
        with patch.dict(os.environ, base), self.assertRaises(tool.FormError):
            tool.create_web_server()
        for public_url in ("", "https://kdautoform.example/api", "https://user:password@example.com"):
            with self.subTest(url=public_url), patch.dict(os.environ, {**base, "PORT": "0", "KD_PUBLIC_URL": public_url}):
                with self.assertRaises(tool.FormError):
                    tool.create_web_server()


if __name__ == "__main__":
    unittest.main()

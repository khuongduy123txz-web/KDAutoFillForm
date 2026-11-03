import hashlib
import http.client
from http.cookies import SimpleCookie
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import unittest
from unittest.mock import patch

import google_form_tool as tool


class ProviderExportTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store = tool.KeyStore(Path(self.folder.name) / "keys.sqlite3")
        with patch.dict(os.environ, {"KD_CLOUD": "0", "RENDER": "", "KD_PUBLIC_URL": "",
                                     "KD_ALLOWED_ORIGINS": "", "RENDER_EXTERNAL_URL": "", "PORT": "0", "KD_HOST": ""}):
            self.server = tool.create_web_server(port=0, key_store=self.store)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)
        self.cookie = ""
        status, headers, html = self.request("GET", "/")
        self.assertEqual(status, 200)
        parsed = SimpleCookie()
        parsed.load(headers["Set-Cookie"])
        raw = parsed["kd_guest"].value
        self.cookie = "kd_guest=" + raw
        self.owner = hashlib.sha256(raw.encode()).hexdigest()
        self.token = re.search(r'const TOKEN="([^"]+)"', html.decode()).group(1)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method, path, data=None, cookie=None):
        headers = {"Cookie": self.cookie if cookie is None else cookie}
        body = None
        if data is not None:
            body = json.dumps(data)
            headers.update({"Content-Type": "application/json", "X-Local-Token": self.token,
                            "Origin": f"http://127.0.0.1:{self.server.server_port}"})
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        try:
            conn.request(method, path, body, headers)
            result = conn.getresponse()
            return result.status, dict(result.getheaders()), result.read()
        finally:
            conn.close()

    def seed_job(self, confirmed=1, owner=None):
        schema = {"provider": "microsoft", "branching": False, "fields": [
            {"id": "ms.name", "question": "Họ và tên", "kind": "text", "options": [], "page": 0},
            {"id": "ms.agree", "question": "Bạn có đồng ý?", "kind": "single", "options": ["Không", "Có"], "page": 0},
        ]}
        log = Path(self.folder.name) / "job.jsonl"
        events = [{"event": "run", "synthetic": True},
                  {"event": "filled", "index": 1, "answers": {"ms.name": "Nguyễn Thị Mai", "ms.agree": "Có"},
                   "report": {"page_indices": [0], "field_ids": ["ms.name", "ms.agree"]}}]
        if confirmed:
            events.append({"event": "confirmed", "index": 1})
        log.write_text("".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events), encoding="utf-8")
        self.server.application.jobs["test-job"] = {
            "id": "test-job", "status": "completed", "confirmed": confirmed,
            "owner_id": self.owner if owner is None else owner, "schema": schema, "log_path": str(log),
            "mode": "send", "counts": {"ms.agree": {"Có": 1}} if confirmed else {},
            "answered_counts": {"ms.name": 1, "ms.agree": 1} if confirmed else {},
        }

    def test_state_advertises_providers_and_preserves_free_quota(self):
        status, _, body = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        info = json.loads(body)
        self.assertEqual(info["providers"], ["google", "microsoft", "html"])
        self.assertEqual((info["plan"], info["remaining"]), ("free", 10))
        self.assertEqual(info["spss_export"], tool.spss_available())

    def test_scan_dispatches_selected_provider_and_keeps_guest_ownership(self):
        app = self.server.application
        app.scans["mock"] = {"id": "mock", "status": "scanning"}
        def queued_scan(url, provider, owner_id):
            app.scans["mock"]["owner_id"] = owner_id
            return {"scan_id": "mock"}
        with patch.object(app, "scan", side_effect=queued_scan) as scan:
            status, _, _ = self.request("POST", "/api/scan", {"url": "https://survey.example/survey", "provider": "html"})
        self.assertEqual(status, 202)
        scan.assert_called_once_with("https://survey.example/survey", provider="html", owner_id=self.owner)
        self.assertEqual(app.scans["mock"]["owner_id"], self.owner)
        self.assertEqual(self.store.guest_usage(self.owner)["used"], 0)

    def test_export_rejects_unconfirmed_and_other_guest_data(self):
        self.seed_job(confirmed=0)
        self.assertEqual(self.request("GET", "/api/jobs/test-job/export.sav")[0], 400)
        self.seed_job(owner="different-owner")
        self.assertEqual(self.request("GET", "/api/jobs/test-job/export.sav")[0], 400)

    @unittest.skipUnless(tool.spss_available(), "SPSS dependencies not installed")
    def test_http_sav_download_roundtrip_and_no_extra_quota(self):
        import pyreadstat
        self.seed_job()
        status, headers, body = self.request("GET", "/api/jobs/test-job/export.sav")
        self.assertEqual(status, 200)
        self.assertIn(".sav", headers["Content-Disposition"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        target = Path(self.folder.name) / "download.sav"
        target.write_bytes(body)
        frame, meta = pyreadstat.read_sav(target)
        self.assertEqual(len(frame), 1)
        self.assertIn("Nguyễn Thị Mai", frame.iloc[0].tolist())
        self.assertIn("Bạn có đồng ý?", meta.column_labels)
        self.assertEqual(self.store.guest_usage(self.owner)["used"], 0)

    def test_export_serializes_expensive_generation(self):
        self.seed_job()
        app = self.server.application
        self.assertTrue(app.export_lock.acquire(blocking=False))
        entered, completed = threading.Event(), threading.Event()
        results = []
        original = app.export_job
        def exporting(*args, **kwargs):
            entered.set()
            return original(*args, **kwargs)
        def download():
            try:
                results.append(self.request("GET", "/api/jobs/test-job/export.zip"))
            finally:
                completed.set()
        with patch.object(app, "export_job", side_effect=exporting):
            thread = threading.Thread(target=download, daemon=True)
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertFalse(completed.wait(.05))
            finally:
                app.export_lock.release()
            self.assertTrue(completed.wait(3))
            thread.join(timeout=1)
        self.assertEqual(results[0][0], 200)
        self.assertEqual(self.store.guest_usage(self.owner, fresh=True)["used"], 0)

    def test_csv_bundle_and_report_keep_confirmed_counts_and_ownership(self):
        import io
        import zipfile
        self.seed_job()
        status, _, body = self.request("GET", "/api/jobs/test-job/report")
        self.assertEqual(status, 200)
        report = json.loads(body)
        self.assertEqual((report["confirmed"], report["fields"][0]["valid_n"]), (1, 1))
        status, headers, body = self.request("GET", "/api/jobs/test-job/export.zip")
        self.assertEqual(status, 200)
        self.assertIn(".zip", headers["Content-Disposition"])
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            self.assertIn("codebook.csv", archive.namelist())
            self.assertTrue(json.loads(archive.read("metadata.json"))["synthetic"])
            self.assertEqual(json.loads(archive.read("summary.json"))["confirmed"], 1)
        self.seed_job(owner="other-owner")
        self.assertEqual(self.request("GET", "/api/jobs/test-job/report")[0], 400)
        self.assertEqual(self.request("GET", "/api/jobs/test-job/export.zip")[0], 400)
        self.assertEqual(self.store.guest_usage(self.owner, fresh=True)["used"], 0)


if __name__ == "__main__":
    unittest.main()

"""Multi-user scheduling regressions. All browser work and scans are simulated."""
import copy
import hashlib
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import google_form_tool as tool


ACTIVE = ("queued", "running", "stopping")


class MultiUserTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store = tool.KeyStore(Path(self.folder.name) / "keys.sqlite3",
                                   export_dir=Path(self.folder.name) / "vault")
        self.guard = threading.Lock()
        self.gates = {}
        self.started = []
        self.submitted = []
        self.logs = {}
        self.exit_hook = None
        self.live = 0
        self.peak = 0
        test = self

        class Browser:
            def __init__(self, **kwargs):
                self.metrics = {}
                self.title = None

            def __enter__(self):
                with test.guard:
                    test.live += 1
                    test.peak = max(test.peak, test.live)
                return self

            def __exit__(self, *args):
                try:
                    if test.exit_hook is not None:
                        test.exit_hook(args)
                finally:
                    with test.guard:
                        test.live -= 1

            def fill_field(self, field, value):
                pass

            def _prepare(self, schema, answers):
                self.title = schema["title"]
                with test.guard:
                    occurrence = sum(value["owner"] == self.title for value in test.started)
                    test.started.append({"owner": self.title, "answers": copy.deepcopy(answers),
                                         "time": time.monotonic(), "thread": threading.current_thread().name})
                gate = test.gates.get(self.title if occurrence == 0 else self.title + "-retry")
                if gate is not None:
                    if not gate.wait(6):
                        raise RuntimeError("Test browser gate was not released")
                self.metrics = {"engine": getattr(self, "engine", "fast"),
                                "fill_ms": 2.0, "batch_fields": len(schema["fields"])}
                for field in schema["fields"]:
                    self.fill_field(field, answers[field["id"]])
                return {"pages": 1, "page_indices": [0], "questions": len(schema["fields"]),
                        "fields": len(schema["fields"]),
                        "field_ids": [field["id"] for field in schema["fields"]]}

            def submit(self, schema):
                with test.guard:
                    test.submitted.append(schema["title"])
                return True

        class Log:
            def __init__(self, path):
                self.path = Path(path)
                test.logs.setdefault(str(self.path), [])

            def write(self, event):
                test.logs[str(self.path)].append(copy.deepcopy(event))

        for name, value in (("FormBrowser", Browser), ("RunLog", Log)):
            replacement = patch.object(tool, name, value)
            replacement.start()
            self.addCleanup(replacement.stop)
        self.app = tool.WebApplication(key_store=self.store, browser_workers=1, scan_workers=2)
        self.addCleanup(self.close)

    def close(self):
        for gate in self.gates.values():
            gate.set()
        self.app.close()

    def wait(self, predicate, timeout=3, message="Expected scheduling state was not reached"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.005)
        self.fail(message)

    def schema(self, owner):
        return {"title": owner, "url": "https://docs.google.com/forms/d/e/qa-" + owner + "/viewform",
                "provider": "google", "branching": False, "pages": [{"index": 0}], "question_count": 2,
                "fields": [
                    {"id": "entry.1", "item_id": 1, "question": "Full name",
                     "description": "", "kind": "text", "options": [], "page": 0, "row": None,
                     "required": True, "rules": [], "exclusive_columns": False},
                    {"id": "entry.2", "item_id": 2, "question": "Do you agree?",
                     "description": "", "kind": "single", "options": ["Yes", "No"], "page": 0,
                     "row": None, "required": True, "rules": [], "exclusive_columns": False},
                ]}

    def owner(self, name, guest_id=None):
        guest = guest_id or hashlib.sha256(name.encode()).hexdigest()
        self.store.ensure_guest(guest)
        schema = self.schema(name)
        scan_id = "scan-" + name
        self.app.scans[scan_id] = {"id": scan_id, "status": "ready", "schema": schema,
                                   "config": tool.default_web_config(schema, 42), "owner_id": guest}
        return {"name": name, "guest": guest, "scan_id": scan_id}

    def payload(self, owner, **overrides):
        return {"scan_id": owner["scan_id"], "count": 1,
                "config": self.app.scans[owner["scan_id"]]["config"], "delay": 0,
                "seed": 42, "reference_date": "2026-11-03",
                "_owner_id": owner["guest"], "_guest_id": owner["guest"], **overrides}

    def start(self, owner, **overrides):
        result = self.app.start_job(self.payload(owner, **overrides))
        return self.app.jobs[result["job_id"]]

    def finish(self, job):
        self.wait(lambda: job["status"] not in ACTIVE)
        self.assertEqual(job["status"], "completed", job.get("error"))

    def used(self, owner):
        return self.store.guest_usage(owner["guest"], fresh=True)["used"]

    def owners_started(self):
        with self.guard:
            return [entry["owner"] for entry in self.started]

    def test_different_owners_queue_fairly_between_responses(self):
        first, second = self.owner("A"), self.owner("B")
        self.gates["A"] = threading.Event()
        a = self.start(first, count=2)
        self.wait(lambda: self.owners_started() == ["A"])
        b = self.start(second)
        self.wait(lambda: self.app.job_state(b["id"])["queue_waiting"])
        state = self.app.job_state(b["id"], compact=True)
        self.assertGreaterEqual(state["queue_position"], 1)
        self.assertEqual(state["worker_capacity"], 1)
        self.gates["A"].set()
        self.finish(a)
        self.finish(b)
        self.assertEqual(self.owners_started(), ["A", "B", "A"])
        self.assertEqual(self.submitted, ["A", "B", "A"])
        self.assertEqual((self.used(first), self.used(second)), (2, 1))
        self.assertEqual((self.peak, self.live), (1, 0))

    def test_same_owner_active_duplicate_is_rejected_without_affecting_other_owner(self):
        first, second = self.owner("A"), self.owner("B")
        self.gates["A"] = threading.Event()
        a = self.start(first)
        self.wait(lambda: self.owners_started() == ["A"])
        with self.assertRaises(tool.FormError):
            self.start(first)
        b = self.start(second)
        self.assertEqual(self.used(second), 0)
        self.gates["A"].set()
        self.finish(a)
        self.finish(b)
        self.assertEqual(self.owners_started(), ["A", "B"])

    def test_queued_stop_finishes_immediately_without_browser_or_quota(self):
        first, second = self.owner("A"), self.owner("B")
        self.gates["A"] = threading.Event()
        a = self.start(first)
        self.wait(lambda: self.owners_started() == ["A"])
        b = self.start(second)
        self.wait(lambda: self.app.job_state(b["id"])["queue_waiting"])
        self.app.stop_job(b["id"])
        self.wait(lambda: b["status"] == "stopped", timeout=.75)
        self.assertEqual(self.owners_started(), ["A"])
        self.assertEqual(self.used(second), 0)
        self.gates["A"].set()
        self.finish(a)
        self.assertEqual(self.submitted, ["A"])

    def test_pacing_wait_does_not_hold_a_browser_or_prevent_other_owner(self):
        first, second = self.owner("A"), self.owner("B")
        a = self.start(first, count=2, schedule={"mode": "fixed", "min_seconds": 30})
        self.wait(lambda: a["processed"] == 1 and a["waiting"])
        self.wait(lambda: self.live == 0)
        b = self.start(second)
        self.finish(b)
        self.assertEqual(self.owners_started(), ["A", "B"])
        self.app.stop_job(a["id"])
        self.wait(lambda: a["status"] == "stopped", timeout=.75)
        self.assertEqual((self.used(first), self.used(second)), (1, 1))
        self.assertEqual(self.peak, 1)

    def test_pause_during_prepare_closes_browser_and_replays_same_answers_after_resume(self):
        first, second = self.owner("A"), self.owner("B")
        self.gates["A"] = threading.Event()
        a = self.start(first)
        self.wait(lambda: self.owners_started() == ["A"])
        self.app.pause_job(a["id"])
        self.gates["A"].set()
        self.wait(lambda: a["paused"] and self.live == 0)
        self.assertEqual(self.used(first), 0)
        b = self.start(second)
        self.finish(b)
        self.assertEqual(self.owners_started(), ["A", "B"])
        self.app.pause_job(a["id"], paused=False)
        self.finish(a)
        self.assertEqual(self.owners_started(), ["A", "B", "A"])
        a_answers = [entry["answers"] for entry in self.started if entry["owner"] == "A"]
        self.assertEqual(a_answers[0], a_answers[1])
        self.assertEqual((self.used(first), self.used(second)), (1, 1))
        self.assertEqual(self.submitted, ["B", "A"])
        self.assertEqual((self.peak, self.live), (1, 0))

    def test_paused_queued_job_does_not_block_next_owner(self):
        first, second, third = self.owner("A"), self.owner("B"), self.owner("C")
        self.gates["A"] = threading.Event()
        a = self.start(first)
        self.wait(lambda: self.owners_started() == ["A"])
        b = self.start(second)
        self.app.pause_job(b["id"])
        c = self.start(third)
        self.gates["A"].set()
        self.finish(a)
        self.finish(c)
        self.assertEqual(self.owners_started(), ["A", "C"])
        self.assertEqual(self.used(second), 0)
        self.app.pause_job(b["id"], paused=False)
        self.finish(b)
        self.assertEqual(self.owners_started(), ["A", "C", "B"])

    def test_resume_during_pause_unwind_does_not_restore_stale_paused_flags(self):
        owner = self.owner("A")
        unwinding, release_unwind = threading.Event(), threading.Event()
        self.gates.update({"A": threading.Event(), "A-retry": threading.Event(),
                           "unwind-release": release_unwind})
        def exit_hook(args):
            if args and args[0] is tool.WebPauseRequested:
                unwinding.set()
                if not release_unwind.wait(6):
                    raise RuntimeError("Pause unwind gate was not released")
        self.exit_hook = exit_hook
        job = self.start(owner)
        self.wait(lambda: self.owners_started() == ["A"])
        self.app.pause_job(job["id"])
        self.gates["A"].set()
        self.assertTrue(unwinding.wait(2))
        # Resume while the old browser is unwinding, before run_job catches pause.
        self.app.pause_job(job["id"], paused=False)
        release_unwind.set()
        self.wait(lambda: self.owners_started() == ["A", "A"])
        state = self.app.job_state(job["id"])
        self.assertFalse(state["paused"])
        self.assertFalse(state["pause_requested"])
        self.assertEqual(self.submitted, [])
        self.gates["A-retry"].set()
        self.finish(job)
        self.assertEqual(self.submitted, ["A"])
        self.assertEqual(self.used(owner), 1)

    def test_pooled_session_cleanup_errors_preserve_confirmed_counts_and_retire_launcher(self):
        fixture_browser = tool.FormBrowser
        test = self
        launches, retires = [], []
        class BadSession:
            def close(self):
                raise RuntimeError("Simulated context cleanup failure")
        class PooledBrowser(fixture_browser):
            def __init__(self, headless=True):
                super().__init__(headless=headless)
                self.headless = headless
                self.session = None
                self.browser = self
            def new_session(self):
                pass
            def is_connected(self):
                return True
            def __enter__(self):
                launches.append(self)
                return self
            def __exit__(self, *args):
                retires.append(self)
                raise RuntimeError("Simulated runtime cleanup failure")
            def _prepare(self, schema, answers):
                result = super()._prepare(schema, answers)
                self.session = BadSession()
                return result
        owner = self.owner("A")
        with patch.object(tool, "FormBrowser", PooledBrowser):
            job = self.start(owner, count=2)
            self.finish(job)
        self.assertEqual((job["processed"], job["confirmed"]), (2, 2))
        self.assertEqual(self.submitted, ["A", "A"])
        self.assertEqual(self.used(owner), 2)
        self.assertEqual((len(launches), len(retires)), (2, 2))
        confirmed = [event for event in test.logs[job["log_path"]] if event["event"] == "confirmed"]
        self.assertEqual([event["index"] for event in confirmed], [1, 2])

    def test_revoked_key_in_queue_never_starts_browser_or_submits(self):
        first, second = self.owner("A"), self.owner("B")
        records, _ = self.store.issue(["paid-B"])
        digest = tool.access_key_hash(records[0]["key"])
        self.store.activate(digest, "activation-B")
        self.gates["A"] = threading.Event()
        a = self.start(first)
        self.wait(lambda: self.owners_started() == ["A"])
        b = self.start(second, _key_id="paid-B", _key_hash=digest, _activation_id="activation-B")
        self.wait(lambda: self.app.job_state(b["id"])["queue_waiting"])
        self.store.change("paid-B", enabled=False)
        self.gates["A"].set()
        self.finish(a)
        self.wait(lambda: b["status"] not in ACTIVE)
        self.assertNotEqual(b["status"], "completed")
        self.assertEqual(self.owners_started(), ["A"])
        self.assertEqual(self.submitted, ["A"])
        self.assertEqual(self.used(second), 0)

    def test_configured_worker_capacity_allows_two_and_bounds_browser_count(self):
        self.app.close()
        self.app = tool.WebApplication(key_store=self.store, browser_workers=2, scan_workers=2)
        first, second, third = self.owner("A"), self.owner("B"), self.owner("C")
        self.gates.update(A=threading.Event(), B=threading.Event())
        a, b = self.start(first), self.start(second)
        self.wait(lambda: len(self.owners_started()) == 2)
        self.assertEqual(self.live, 2)
        c = self.start(third)
        self.wait(lambda: self.app.job_state(c["id"])["queue_waiting"])
        self.assertEqual(self.app.job_state(c["id"])["worker_capacity"], 2)
        self.gates["A"].set()
        self.gates["B"].set()
        for job in (a, b, c):
            self.finish(job)
        self.assertEqual((self.peak, self.live), (2, 0))
        self.assertCountEqual(self.owners_started(), ["A", "B", "C"])

    def test_many_waiting_users_do_not_create_one_thread_or_browser_per_job(self):
        first = self.owner("A")
        self.gates["A"] = threading.Event()
        a = self.start(first)
        self.wait(lambda: self.owners_started() == ["A"])
        baseline_threads = threading.active_count()
        queued = []
        for index in range(20):
            owner = self.owner("queued-" + str(index))
            queued.append(self.start(owner))
        self.assertLessEqual(threading.active_count() - baseline_threads, 2)
        self.assertEqual((self.live, self.peak), (1, 1))
        for job in queued:
            self.app.stop_job(job["id"])
        self.wait(lambda: all(job["status"] == "stopped" for job in queued), timeout=1)
        self.gates["A"].set()
        self.finish(a)
        self.assertEqual(self.owners_started(), ["A"])

    def test_scans_are_owner_isolated_and_accepted_during_another_job(self):
        first, second, third = self.owner("A"), self.owner("B"), self.owner("C")
        self.gates["A"] = threading.Event()
        a = self.start(first)
        self.wait(lambda: self.owners_started() == ["A"])
        with patch.object(self.app, "read_form_schema", side_effect=lambda url: self.schema(url.split("/qa-")[-1].split("/")[0])):
            b = self.app.scan(self.schema("B")["url"], owner_id=second["guest"])["scan_id"]
            c = self.app.scan(self.schema("C")["url"], owner_id=third["guest"])["scan_id"]
            self.wait(lambda: self.app.scan_state(b)["status"] == "ready")
            self.wait(lambda: self.app.scan_state(c)["status"] == "ready")
        self.assertEqual(self.app.scan_state(b)["owner_id"], second["guest"])
        self.assertEqual(self.app.scan_state(c)["owner_id"], third["guest"])
        self.assertEqual(self.live, 1)
        self.assertEqual(self.owners_started(), ["A"])
        self.gates["A"].set()
        self.finish(a)

    def test_http_owners_cannot_read_or_stop_others_but_can_submit_own_queued_job(self):
        raw_a, raw_b = tool.secrets.token_urlsafe(32), tool.secrets.token_urlsafe(32)
        first = self.owner("A", hashlib.sha256(raw_a.encode()).hexdigest())
        second = self.owner("B", hashlib.sha256(raw_b.encode()).hexdigest())
        self.gates["A"] = threading.Event()
        a = self.start(first)
        self.wait(lambda: self.owners_started() == ["A"])
        local_env = {"KD_CLOUD": "0", "RENDER": "", "KD_PUBLIC_URL": "", "RENDER_EXTERNAL_URL": "",
                     "KD_ALLOWED_ORIGINS": "", "KD_HOST": "", "PORT": "0"}
        with patch.dict(os.environ, local_env), patch.object(tool, "WebApplication", return_value=self.app):
            server = tool.create_web_server(port=0, host="127.0.0.1", key_store=self.store)
        serving = threading.Thread(target=lambda: server.serve_forever(poll_interval=.02), daemon=True)
        serving.start()

        def request(method, path, cookie, payload=None):
            body = None if payload is None else json.dumps(payload)
            headers = {"Cookie": "kd_guest=" + cookie}
            if body is not None:
                headers.update({"Content-Type": "application/json", "X-Local-Token": self.app.token,
                                "Origin": "http://127.0.0.1:" + str(server.server_port)})
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            try:
                connection.request(method, path, body, headers)
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close()
        try:
            self.assertGreaterEqual(request("GET", "/api/jobs/" + a["id"], raw_b)[0], 400)
            self.assertGreaterEqual(request("GET", "/api/scans/" + first["scan_id"], raw_b)[0], 400)
            self.assertGreaterEqual(request("POST", "/api/jobs/" + a["id"] + "/stop", raw_b, {})[0], 400)
            self.assertIn(a["status"], ACTIVE)
            self.assertEqual(request("GET", "/api/scans/" + second["scan_id"], raw_b)[0], 200)
            status, result = request("POST", "/api/jobs", raw_b, self.payload(second, _owner_id=first["guest"]))
            self.assertEqual(status, 202, result)
            b = self.app.jobs[result["job_id"]]
            self.assertEqual(b["owner_id"], second["guest"])
            self.assertGreaterEqual(request("GET", "/api/jobs/" + b["id"], raw_a)[0], 400)
            self.assertEqual(request("POST", "/api/jobs/" + b["id"] + "/stop", raw_b, {})[0], 200)
            self.wait(lambda: b["status"] == "stopped")
            # server_close now closes its application/dispatcher too. Finish
            # the deliberately gated response before tearing down that owner.
            self.gates["A"].set()
            self.finish(a)
        finally:
            self.gates["A"].set()
            server.shutdown()
            server.server_close()
            serving.join(timeout=1)


if __name__ == "__main__":
    unittest.main()

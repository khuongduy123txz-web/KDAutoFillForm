import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import google_form_tool as tool


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store = tool.KeyStore(Path(self.folder.name) / "keys.sqlite3")
        self.guest = hashlib.sha256(b"workflow").hexdigest()
        self.store.ensure_guest(self.guest)
        self.app = tool.WebApplication(key_store=self.store)
        self.schema = {"title": "Workflow", "url": "https://docs.google.com/forms/d/e/workflow/viewform",
                       "provider": "google", "branching": False, "pages": [{"index": 0}], "question_count": 2,
                       "fields": [
                           {"id": "entry.1", "item_id": 1, "question": "Full name", "kind": "text", "options": [],
                            "description": "", "page": 0, "rules": [], "row": None, "required": True, "exclusive_columns": False},
                           {"id": "entry.2", "item_id": 2, "question": "Do you agree?", "kind": "single", "options": ["Yes", "No"],
                            "description": "", "page": 0, "rules": [], "row": None, "required": True, "exclusive_columns": False},
                       ]}
        self.app.scans["scan"] = {"id": "scan", "status": "ready", "schema": self.schema,
                                  "config": tool.default_web_config(self.schema, 42), "owner_id": self.guest}
        self.logs, self.starts, self.submits = {}, [], []
        self.engines = []
        self.skip = set()
        test = self

        class Browser:
            def __init__(self, **kwargs):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def fill_field(self, field, value):
                pass
            def _prepare(self, schema, answers):
                test.starts.append(time.monotonic())
                test.engines.append(self.engine)
                self.metrics = {"engine": self.engine, "fill_ms": 1.25, "batch_fields": 2}
                ids = []
                for field in schema["fields"]:
                    if field["id"] not in test.skip:
                        self.fill_field(field, answers[field["id"]])
                        ids.append(field["id"])
                return {"page_indices": [0], "field_ids": ids, "questions": len(ids), "pages": 1, "fields": len(ids)}
            def submit(self, schema):
                test.submits.append(time.monotonic())

        class Log:
            def __init__(self, path):
                self.path = Path(path)
                test.logs[str(path)] = []
            def write(self, event):
                test.logs[str(self.path)].append(event)

        for name, replacement in (("FormBrowser", Browser), ("RunLog", Log)):
            mock = patch.object(tool, name, replacement)
            mock.start()
            self.addCleanup(mock.stop)
        self.addCleanup(lambda: [job["control"].stop() for job in self.app.jobs.values()])
        self.addCleanup(self.app.close)

    def payload(self, **overrides):
        return {"scan_id": "scan", "count": 2, "config": self.app.scans["scan"]["config"], "delay": 0,
                "seed": 42, "reference_date": "2026-11-03", "_owner_id": self.guest, "_guest_id": self.guest, **overrides}

    def start(self, **overrides):
        result = self.app.start_job(self.payload(**overrides))
        return self.app.jobs[result["job_id"]]

    def wait(self, predicate, seconds=3):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.005)
        self.fail("Worker did not reach expected state")

    def finish(self, job):
        self.wait(lambda: job["status"] not in ("queued", "running", "stopping"))
        self.assertEqual(job["status"], "completed", job.get("error"))

    def test_fixed_pacing_is_between_starts_and_seed_repeats_answers(self):
        schedule = {"mode": "fixed", "min_seconds": .04, "max_seconds": .04}
        first = self.start(schedule=schedule)
        self.finish(first)
        self.assertGreaterEqual(self.starts[1] - self.starts[0], .03)
        first_answers = [event["answers"] for event in self.logs[first["log_path"]] if event["event"] == "filled"]
        second = self.start(schedule=schedule)
        self.finish(second)
        second_answers = [event["answers"] for event in self.logs[second["log_path"]] if event["event"] == "filled"]
        self.assertEqual(first_answers, second_answers)
        self.assertEqual(self.logs[first["log_path"]][0]["reference_date"], "2026-11-03")
        self.assertEqual(self.store.guest_usage(self.guest, fresh=True)["used"], 4)
        self.assertEqual(self.app.report_job(second["id"])["confirmed"], 2)

    def test_pause_freezes_wait_and_stop_wakes_long_interval(self):
        job = self.start(schedule={"mode": "fixed", "min_seconds": 30})
        self.wait(lambda: job["waiting"] and job["confirmed"] == 1)
        self.app.pause_job(job["id"])
        self.wait(lambda: job["paused"])
        before = len(self.submits)
        time.sleep(.05)
        self.assertEqual(len(self.submits), before)
        self.assertTrue(self.app.job_state(job["id"], compact=True)["pause_requested"])
        self.app.pause_job(job["id"], paused=False)
        self.wait(lambda: job["waiting"])
        began = time.monotonic()
        self.app.stop_job(job["id"])
        self.wait(lambda: job["status"] == "stopped", seconds=1)
        self.assertLess(time.monotonic() - began, 1)
        self.assertEqual(self.store.guest_usage(self.guest, fresh=True)["used"], 1)

    def test_skipped_question_on_same_page_does_not_enter_statistics(self):
        self.schema["branching"] = True
        self.skip.add("entry.2")
        job = self.start()
        self.finish(job)
        self.assertEqual(job["counts"], {})
        item = self.app.report_job(job["id"])["fields"][0]
        self.assertEqual((item["valid_n"], item["missing"]), (0, 2))

    def test_preview_never_changes_quota_or_confirmed_statistics(self):
        job = self.start(mode="check")
        self.finish(job)
        self.assertEqual(job["confirmed"], 0)
        self.assertEqual(job["counts"], {})
        self.assertEqual(self.app.report_job(job["id"])["confirmed"], 0)
        self.assertEqual(self.submits, [])
        self.assertEqual(self.store.guest_usage(self.guest, fresh=True)["used"], 0)

    def test_engine_selection_and_measured_timing_reach_log_and_compact_poll(self):
        for engine in ("fast", "compatible"):
            job = self.start(count=1, engine=engine)
            self.finish(job)
            state = self.app.job_state(job["id"], compact=True)
            self.assertEqual(self.engines[-1], engine)
            self.assertEqual(state["engine"], engine)
            self.assertGreaterEqual(state["last_response_seconds"], 0)
            self.assertEqual(state["last_performance"]["fill_ms"], 1.25)
            events = self.logs[job["log_path"]]
            self.assertEqual(events[0]["engine"], engine)
            performance = [event for event in events if event["event"] == "performance"]
            self.assertEqual(len(performance), 1)
            self.assertEqual(performance[0]["seconds"], state["last_response_seconds"])
        self.assertEqual(self.store.guest_usage(self.guest, fresh=True)["used"], 2)

    def test_invalid_seed_and_schedule_do_not_start_or_charge(self):
        for values in ({"seed": True}, {"seed": 1.2}, {"seed": -1}, {"seed": 9007199254740992},
                       {"schedule": {"mode": "random", "min_seconds": 180, "max_seconds": 30}},
                       {"schedule": {"mode": "fixed", "min_seconds": float("nan")}},
                       {"reference_date": False}, {"reference_date": "2026-02-31"},
                       {"engine": "unknown"}, {"engine": None}, {"engine": True}):
            with self.subTest(values=values), self.assertRaises((tool.FormError, ValueError)):
                self.start(**values)
        self.assertEqual(self.app.jobs, {})
        self.assertEqual(self.store.guest_usage(self.guest, fresh=True)["used"], 0)

    def test_pause_during_quota_write_never_submits_and_stop_releases_reservation(self):
        entered, release = threading.Event(), threading.Event()
        consume = self.store.consume_guest
        def slow_consume(identifier):
            result = consume(identifier)
            entered.set()
            release.wait(2)
            return result
        with patch.object(self.store, "consume_guest", side_effect=slow_consume):
            job = self.start(count=1)
            self.assertTrue(entered.wait(1))
            self.app.pause_job(job["id"])
            release.set()
            self.wait(lambda: job["paused"])
            self.assertEqual(self.submits, [])
            self.assertEqual(self.store.guest_usage(self.guest, fresh=True)["used"], 0)
            self.app.stop_job(job["id"])
            self.wait(lambda: job["status"] == "stopped")
        self.assertEqual(self.submits, [])
        self.assertEqual(self.store.guest_usage(self.guest, fresh=True)["used"], 0)

    def test_compact_snapshot_avoids_large_counts_and_thread_controls(self):
        job = self.start(count=1)
        self.finish(job)
        job["counts"] = {"large": {str(i): i for i in range(2000)}}
        full = json.dumps(self.app.job_state(job["id"]))
        compact = self.app.job_state(job["id"], compact=True)
        self.assertNotIn("counts", compact)
        self.assertNotIn("answered_counts", compact)
        self.assertNotIn("last_profile", compact)
        self.assertNotIn("control", compact)
        self.assertLess(len(json.dumps(compact)), len(full) / 5)


if __name__ == "__main__":
    unittest.main()

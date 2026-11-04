"""End-to-end scheduling measurements against controlled browser operations."""
import copy
import hashlib
import threading
import time
import unittest
from unittest.mock import patch

import google_form_tool as tool
import test_workflow as workflow


class ActualTimingTests(unittest.TestCase):
    # Reuse the fixture helpers without subclassing its TestCase, which would
    # rediscover every unrelated workflow test under a second class name.
    payload = workflow.WorkflowTests.payload
    start = workflow.WorkflowTests.start
    wait = workflow.WorkflowTests.wait
    finish = workflow.WorkflowTests.finish

    def setUp(self):
        workflow.WorkflowTests.setUp(self)
        self.records = []
        self.delays = [(0.02, 0.01, 0.04), (0.04, 0.02, 0.06)]
        self.access_delays = [0.015, 0.06]
        self.admissions = []
        self.worker_local = threading.local()
        self.entered = threading.Event()
        self.prepare_gate = None
        self.active = self.max_active = 0
        self.record_lock = threading.Lock()
        original = tool.FormBrowser
        test = self

        original_run_job = self.app.run_job

        def admitted(job):
            # Independently observe dispatch admission before run_job performs
            # key checks, answer generation, logging, or browser creation.
            record = {"admitted": time.monotonic(), "access_checked": False}
            test.admissions.append(record)
            test.worker_local.admission = record
            return original_run_job(job)

        mock = patch.object(self.app, "run_job", side_effect=admitted)
        mock.start()
        self.addCleanup(mock.stop)
        original_access = tool.active_job_key

        def slow_initial_access(job, store):
            record = getattr(test.worker_local, "admission", None)
            if record is not None and not record["access_checked"]:
                record["access_checked"] = True
                delay = test.access_delays[min(len(test.admissions) - 1, len(test.access_delays) - 1)]
                record["access_delay"] = delay
                time.sleep(delay)
            return original_access(job, store)

        mock = patch.object(tool, "active_job_key", side_effect=slow_initial_access)
        mock.start()
        self.addCleanup(mock.stop)

        class MeasuredBrowser(original):
            def __enter__(self):
                with test.record_lock:
                    self.record = test.worker_local.admission
                    self.record["entered"] = time.monotonic()
                    self.delays = test.delays[min(len(test.records), len(test.delays) - 1)]
                    test.records.append(self.record)
                    test.active += 1
                    test.max_active = max(test.max_active, test.active)
                return super().__enter__()

            def _prepare(self, schema, answers):
                self.record["title"] = schema["title"]
                test.entered.set()
                if test.prepare_gate is not None and len(test.records) == 1:
                    if not test.prepare_gate.wait(2):
                        raise AssertionError("Test preparation gate timed out")
                time.sleep(self.delays[0])
                return super()._prepare(schema, answers)

            def submit(self, schema):
                time.sleep(self.delays[1])
                super().submit(schema)
                self.record["confirmed"] = time.monotonic()

            def __exit__(self, *args):
                time.sleep(self.delays[2])
                self.record["closed"] = time.monotonic()
                with test.record_lock:
                    test.active -= 1
                return super().__exit__(*args)

        mock = patch.object(tool, "FormBrowser", MeasuredBrowser)
        mock.start()
        self.addCleanup(mock.stop)

    def test_actual_duration_and_start_intervals_include_admission_overhead_and_cleanup(self):
        job = self.start(schedule={"mode": "fixed", "min_seconds": 0.04})
        self.finish(job)
        state = self.app.job_state(job["id"], compact=True)
        first, second = self.records
        observed = second["closed"] - second["admitted"]
        browser_duration = second["closed"] - second["entered"]
        self.assertGreaterEqual(state["last_response_seconds"],
                                sum(self.delays[1]) + second["access_delay"] - 0.004)
        self.assertGreaterEqual(state["last_response_seconds"],
                                browser_duration + second["access_delay"] - 0.004)
        self.assertAlmostEqual(state["last_response_seconds"], observed, delta=0.02)
        self.assertAlmostEqual(state["last_start_interval_seconds"],
                               second["admitted"] - first["admitted"], delta=0.02)
        self.assertAlmostEqual(state["last_confirmation_interval_seconds"],
                               second["confirmed"] - first["confirmed"], delta=0.02)
        self.assertGreaterEqual(state["processing_overrun_seconds"], 0.07)
        self.assertEqual(self.max_active, 1)
        self.assertGreaterEqual(second["entered"], first["closed"])
        events = [event for event in self.logs[job["log_path"]] if event["event"] == "performance"]
        self.assertEqual(events[-1]["seconds"], state["last_response_seconds"])
        self.assertEqual(events[-1]["start_interval_seconds"], state["last_start_interval_seconds"])
        self.assertEqual(events[-1]["confirmation_interval_seconds"], state["last_confirmation_interval_seconds"])

    def test_completion_cooldown_starts_after_browser_cleanup(self):
        interval = 0.08
        job = self.start(schedule={"mode": "fixed", "min_seconds": interval, "basis": "completion"})
        self.finish(job)
        first, second = self.records
        self.assertGreaterEqual(second["entered"] - first["closed"], interval - 0.003)
        self.assertEqual(job["target_interval_seconds"], interval)
        self.assertEqual(job["processing_overrun_seconds"], 0)
        self.assertGreater(job["last_start_interval_seconds"], interval + 0.05)

    def test_start_overrun_yields_to_another_guest_without_overlapping_browsers(self):
        self.prepare_gate = threading.Event()
        self.addCleanup(self.prepare_gate.set)
        first = self.start(schedule={"mode": "fixed", "min_seconds": 0.01})
        self.assertTrue(self.entered.wait(1))
        guest = hashlib.sha256(b"timing-second-guest").hexdigest()
        self.store.ensure_guest(guest)
        schema = copy.deepcopy(self.schema)
        schema["title"] = "Second guest"
        self.app.scans["second-scan"] = {"id": "second-scan", "status": "ready", "schema": schema,
                                         "config": tool.default_web_config(schema, 42), "owner_id": guest}
        result = self.app.start_job(self.payload(scan_id="second-scan", count=1, _owner_id=guest,
                                                 _guest_id=guest, schedule={"min_seconds": 0}))
        second = self.app.jobs[result["job_id"]]
        self.prepare_gate.set()
        self.finish(first)
        self.finish(second)
        self.assertEqual([row["title"] for row in self.records], ["Workflow", "Second guest", "Workflow"])
        self.assertEqual(self.max_active, 1)
        for previous, following in zip(self.records, self.records[1:]):
            self.assertGreaterEqual(following["entered"], previous["closed"])
        self.assertGreaterEqual(first["last_schedule_overrun_seconds"], 0.1)
        self.assertEqual((first["confirmed"], second["confirmed"]), (2, 1))

    def test_pause_preserves_completion_cooldown_until_resume(self):
        job = self.start(schedule={"min_seconds": 0.12, "basis": "completion"})
        self.wait(lambda: job["waiting"] and job["confirmed"] == 1)
        self.app.pause_job(job["id"])
        self.wait(lambda: job["paused"])
        remaining = job["_paused_remaining"]
        self.assertGreater(remaining, 0.07)
        time.sleep(0.15)
        self.assertEqual(len(self.records), 1)
        resumed = time.monotonic()
        self.app.pause_job(job["id"], paused=False)
        self.finish(job)
        self.assertGreaterEqual(self.records[1]["entered"] - resumed, remaining - 0.004)


if __name__ == "__main__":
    unittest.main()

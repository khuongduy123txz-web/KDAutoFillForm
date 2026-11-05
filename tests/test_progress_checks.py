"""Keep field progress cheap while enforcing access and quota before sending."""
from pathlib import Path
import unittest
from unittest.mock import patch

import google_form_tool as tool
import test_workflow as workflow


class ProgressAccessTests(unittest.TestCase):
    payload = workflow.WorkflowTests.payload
    start = workflow.WorkflowTests.start
    wait = workflow.WorkflowTests.wait
    finish = workflow.WorkflowTests.finish

    def setUp(self):
        workflow.WorkflowTests.setUp(self)

    def test_many_guest_fields_do_not_repeat_access_checks(self):
        template = self.schema["fields"][0]
        self.schema["fields"] = [dict(template, id=f"entry.{index}", item_id=index)
                                 for index in range(1, 101)]
        self.schema["question_count"] = 100
        self.app.scans["scan"]["config"] = tool.default_web_config(self.schema, 42)
        with patch.object(tool, "active_job_key", wraps=tool.active_job_key) as access:
            job = self.start(count=1)
            self.finish(job)
        self.assertLessEqual(access.call_count, 8)
        self.assertEqual(len(self.submits), 1)
        self.assertEqual(self.store.guest_usage(self.guest, fresh=True)["used"], 1)

    def browser_after_prepare(self, callback):
        original = tool.FormBrowser
        class Browser(original):
            def _prepare(self, schema, answers):
                report = super()._prepare(schema, answers)
                callback()
                return report
        mock = patch.object(tool, "FormBrowser", Browser)
        mock.start()
        self.addCleanup(mock.stop)

    def test_deleted_guest_is_stopped_before_submit(self):
        def remove_guest():
            with self.store.connection() as connection:
                self.store.execute(connection, "DELETE FROM kd_guest_usage WHERE guest_id = ?", (self.guest,))
            self.store.clear_cache()
        self.browser_after_prepare(remove_guest)
        job = self.start(count=1)
        self.wait(lambda: job["status"] == "stopped")
        self.assertEqual(self.submits, [])
        self.assertEqual(job["confirmed"], 0)

    def test_quota_consumed_elsewhere_is_still_rejected_atomically(self):
        other = tool.KeyStore(Path(self.folder.name) / "keys.sqlite3")
        self.addCleanup(other.close)
        def exhaust_quota():
            for _ in range(tool.FREE_LIMIT):
                other.consume_guest(self.guest)
        self.browser_after_prepare(exhaust_quota)
        job = self.start(count=1)
        self.wait(lambda: job["status"] == "error")
        self.assertEqual(self.submits, [])
        self.assertEqual(job["confirmed"], 0)
        self.assertEqual(self.store.guest_usage(self.guest, fresh=True)["remaining"], 0)


if __name__ == "__main__":
    unittest.main()

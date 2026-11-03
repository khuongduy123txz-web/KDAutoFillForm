import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import google_form_tool as tool


class GuestReadPerformanceTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "keys.sqlite3"
        self.store = tool.KeyStore(self.path)
        self.guest = hashlib.sha256(b"performance-guest").hexdigest()
        self.store.ensure_guest(self.guest)

    def test_hundred_field_checks_share_one_database_read(self):
        self.store.clear_cache()
        job = {"guest_id": self.guest}
        with patch.object(self.store, "connection", wraps=self.store.connection) as calls:
            for _ in range(100):
                self.assertTrue(tool.active_job_key(job, self.store))
        self.assertEqual(calls.call_count, 1)

    def test_cache_expires_and_fresh_read_observes_other_store(self):
        other = tool.KeyStore(self.path)
        self.store.clear_cache()
        with patch.object(tool.time, "monotonic", return_value=100):
            self.assertEqual(self.store.guest_usage(self.guest)["used"], 0)
            other.consume_guest(self.guest)
            self.assertEqual(self.store.guest_usage(self.guest)["used"], 0)
            self.assertEqual(self.store.guest_usage(self.guest, fresh=True)["used"], 1)
        other.consume_guest(self.guest)
        with patch.object(tool.time, "monotonic", return_value=101.1):
            self.assertEqual(self.store.guest_usage(self.guest)["used"], 2)

    def test_quota_increment_is_one_atomic_database_operation(self):
        with patch.object(self.store, "connection", wraps=self.store.connection) as calls:
            usage = self.store.consume_guest(self.guest)
        self.assertEqual(calls.call_count, 1)
        self.assertEqual(usage["used"], 1)
        with patch.object(self.store, "connection", wraps=self.store.connection) as calls:
            self.assertEqual(self.store.guest_usage(self.guest)["used"], 1)
        self.assertEqual(calls.call_count, 0)

    def test_stale_cache_never_allows_an_eleventh_submission(self):
        other = tool.KeyStore(self.path)
        for _ in range(10):
            other.consume_guest(self.guest)
        with self.assertRaises(tool.QuotaExceeded):
            self.store.consume_guest(self.guest)
        self.assertEqual(self.store.guest_usage(self.guest, fresh=True)["remaining"], 0)

    def test_mutating_returned_usage_cannot_change_cached_quota(self):
        usage = self.store.guest_usage(self.guest)
        usage["remaining"] = 10000
        self.assertEqual(self.store.guest_usage(self.guest)["remaining"], 10)
        usage = self.store.consume_guest(self.guest)
        usage["used"] = -100
        self.assertEqual(self.store.guest_usage(self.guest)["used"], 1)

    def test_guest_cache_is_bounded_even_for_unknown_cookie_ids(self):
        for index in range(2050):
            self.assertIsNone(self.store.guest_usage(f"unknown-{index}"))
        self.assertLessEqual(len(self.store.guest_cache), 2048)

    def test_cancelled_reserved_slot_is_released_atomically_and_cache_updates(self):
        self.store.consume_guest(self.guest)
        with patch.object(self.store, "connection", wraps=self.store.connection) as calls:
            usage = self.store.release_guest(self.guest)
        self.assertEqual(calls.call_count, 1)
        self.assertEqual((usage["used"], usage["remaining"]), (0, 10))
        with patch.object(self.store, "connection", wraps=self.store.connection) as calls:
            self.assertEqual(self.store.guest_usage(self.guest)["used"], 0)
        self.assertEqual(calls.call_count, 0)
        usage["used"] = -100
        self.assertEqual(self.store.guest_usage(self.guest)["used"], 0)

    def test_release_unknown_or_zero_usage_never_creates_negative_quota(self):
        self.assertIsNone(self.store.release_guest("unknown"))
        self.assertIsNone(self.store.release_guest(self.guest))
        self.assertEqual(self.store.guest_usage(self.guest, fresh=True)["used"], 0)
        self.store.consume_guest(self.guest)
        self.assertEqual(self.store.release_guest(self.guest)["used"], 0)
        self.assertIsNone(self.store.release_guest(self.guest))
        self.assertEqual(self.store.guest_usage(self.guest, fresh=True)["remaining"], 10)


if __name__ == "__main__":
    unittest.main()

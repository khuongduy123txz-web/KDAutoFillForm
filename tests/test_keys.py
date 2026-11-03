import csv
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import tempfile
import threading
import unittest

import google_form_tool as tool


class KeyStoreTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "keys.sqlite3"
        self.exports = Path(self.folder.name) / "vault"
        self.store = tool.KeyStore(self.path, export_dir=self.exports)

    def issue(self, name="customer"):
        records, path = self.store.issue([name])
        return records[0], path

    def test_issue_saves_raw_key_privately_and_only_hash_in_database(self):
        record, export = self.issue()
        with export.open(encoding="utf-8-sig", newline="") as handle:
            self.assertEqual(list(csv.DictReader(handle))[0]["key"], record["key"])
        row = self.store.get(record["name"])
        self.assertEqual(row["sha256"], tool.access_key_hash(record["key"]))
        self.assertIsNone(row["claimed_job"])
        self.assertNotIn(record["key"].encode(), self.path.read_bytes())
        self.assertIsNotNone(self.store.unused_by_hash(row["sha256"]))

    def test_claim_survives_new_store_and_database_reopen(self):
        record, _ = self.issue()
        digest = tool.access_key_hash(record["key"])
        self.store.claim("customer", digest, "job-first")
        self.store.finish("customer", digest, "job-first", "completed")
        reopened = tool.KeyStore(self.path, export_dir=self.exports)
        self.assertIsNone(reopened.unused_by_hash(digest))
        self.assertEqual(reopened.get("customer")["outcome"], "completed")
        with self.assertRaises(tool.KeyAlreadyUsed):
            reopened.claim("customer", digest, "job-second")

    def test_two_independent_stores_racing_have_exactly_one_winner(self):
        record, _ = self.issue()
        digest = tool.access_key_hash(record["key"])
        stores = [tool.KeyStore(self.path, export_dir=self.exports) for _ in range(8)]
        gate = threading.Barrier(len(stores))

        def claim(index):
            gate.wait()
            try:
                stores[index].claim("customer", digest, f"job-{index}")
                return True
            except tool.KeyAlreadyUsed:
                return False

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(claim, range(8)))
        self.assertEqual(sum(results), 1)

    def test_unlock_does_not_reset_usage_and_rotation_creates_different_key(self):
        first, _ = self.issue()
        digest = tool.access_key_hash(first["key"])
        self.store.claim("customer", digest, "used")
        self.store.change("customer", enabled=False)
        self.store.change("customer", enabled=True)
        with self.assertRaises(tool.KeyAlreadyUsed):
            self.store.claim("customer", digest, "retry")
        records, _ = self.store.issue(["customer"], rotate=True)
        second = records[0]
        self.assertNotEqual(first["key"], second["key"])
        self.assertIsNone(self.store.unused_by_hash(digest))
        self.assertIsNotNone(self.store.unused_by_hash(tool.access_key_hash(second["key"])))

    def test_legacy_import_runs_once_and_deleted_keys_do_not_reappear(self):
        raw = tool.create_access_key()
        seed = {"legacy": {"sha256": tool.access_key_hash(raw), "enabled": True}}
        other_path = self.path.with_name("legacy.sqlite3")
        seeded = tool.KeyStore(other_path, export_dir=self.exports, legacy=seed)
        self.assertIsNotNone(seeded.get("legacy"))
        seeded.change("legacy", delete=True)
        reopened = tool.KeyStore(other_path, export_dir=self.exports, legacy=seed)
        self.assertIsNone(reopened.get("legacy"))

    def test_sessions_can_read_results_after_claim_but_same_key_cannot_login_again(self):
        record, _ = self.issue()
        access = tool.AccessSessions(self.store)
        token, session, retry = access.login(record["key"], "first")
        self.assertTrue(token)
        self.assertEqual(retry, 0)
        self.store.claim(session["key_id"], session["key_hash"], "one-job")
        current = access.current("kd_access=" + token)
        self.assertTrue(current["key_used"])
        self.assertIsNone(access.login(record["key"], "second")[0])
        self.store.change("customer", enabled=False)
        self.assertIsNone(access.current("kd_access=" + token))

    def test_failed_or_stopped_job_remains_used(self):
        for index, outcome in enumerate(("error", "stopped")):
            record, _ = self.issue(f"customer-{index}")
            digest = tool.access_key_hash(record["key"])
            self.store.claim(record["name"], digest, f"job-{index}")
            self.store.finish(record["name"], digest, f"job-{index}", outcome)
            self.assertIsNone(self.store.unused_by_hash(digest))

    def test_duplicate_batch_rolls_back_without_half_created_keys(self):
        self.issue()
        with self.assertRaises(tool.FormError):
            self.store.issue(["another", "customer"])
        self.assertIsNone(self.store.get("another"))


@unittest.skipUnless(os.environ.get("KD_TEST_DATABASE_URL"), "PostgreSQL test database not configured")
class PostgreSQLKeyTests(unittest.TestCase):
    def test_persistent_atomic_claim_with_real_postgresql(self):
        with tempfile.TemporaryDirectory() as folder:
            url = os.environ["KD_TEST_DATABASE_URL"]
            first = tool.KeyStore(database_url=url, export_dir=folder)
            second = tool.KeyStore(database_url=url, export_dir=folder)
            name = "qa-" + tool.secrets.token_hex(8)
            records, _ = first.issue([name])
            digest = tool.access_key_hash(records[0]["key"])
            try:
                first.claim(name, digest, "postgres-job")
                with self.assertRaises(tool.KeyAlreadyUsed):
                    second.claim(name, digest, "other-job")
                second.finish(name, digest, "postgres-job", "completed")
                self.assertIsNone(first.unused_by_hash(digest))
                self.assertEqual(second.get(name)["outcome"], "completed")
            finally:
                first.change(name, delete=True)


if __name__ == "__main__":
    unittest.main()

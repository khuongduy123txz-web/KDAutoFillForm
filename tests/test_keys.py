import csv
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import tempfile
import threading
import unittest
import sqlite3

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

    def activate(self, record):
        activation_id = tool.secrets.token_hex(32)
        self.assertIsNotNone(self.store.activate(tool.access_key_hash(record["key"]), activation_id))
        return activation_id

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
        activation_id = self.activate(record)
        self.store.claim("customer", digest, "job-first", activation_id)
        self.store.finish("customer", digest, "job-first", "completed")
        reopened = tool.KeyStore(self.path, export_dir=self.exports)
        self.assertIsNone(reopened.unused_by_hash(digest))
        self.assertEqual(reopened.get("customer")["outcome"], "completed")
        with self.assertRaises(tool.KeyAlreadyUsed):
            reopened.claim("customer", digest, "job-second", activation_id)

    def test_two_independent_stores_racing_have_exactly_one_winner(self):
        record, _ = self.issue()
        digest = tool.access_key_hash(record["key"])
        activation_id = self.activate(record)
        stores = [tool.KeyStore(self.path, export_dir=self.exports) for _ in range(8)]
        gate = threading.Barrier(len(stores))

        def claim(index):
            gate.wait()
            try:
                stores[index].claim("customer", digest, f"job-{index}", activation_id)
                return True
            except tool.KeyAlreadyUsed:
                return False

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(claim, range(8)))
        self.assertEqual(sum(results), 1)

    def test_unlock_does_not_reset_usage_and_rotation_creates_different_key(self):
        first, _ = self.issue()
        digest = tool.access_key_hash(first["key"])
        activation_id = self.activate(first)
        self.store.claim("customer", digest, "used", activation_id)
        self.store.change("customer", enabled=False)
        self.store.change("customer", enabled=True)
        with self.assertRaises(tool.KeyAlreadyUsed):
            self.store.claim("customer", digest, "retry", activation_id)
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
        self.store.claim(session["key_id"], session["key_hash"], "one-job", session["activation_id"])
        current = access.current("kd_access=" + token)
        self.assertTrue(current["key_used"])
        self.assertIsNone(access.login(record["key"], "second")[0])
        self.store.change("customer", enabled=False)
        self.assertIsNone(access.current("kd_access=" + token))

    def test_failed_or_stopped_job_remains_used(self):
        for index, outcome in enumerate(("error", "stopped")):
            record, _ = self.issue(f"customer-{index}")
            digest = tool.access_key_hash(record["key"])
            activation_id = self.activate(record)
            self.store.claim(record["name"], digest, f"job-{index}", activation_id)
            self.store.finish(record["name"], digest, f"job-{index}", outcome)
            self.assertIsNone(self.store.unused_by_hash(digest))

    def test_duplicate_batch_rolls_back_without_half_created_keys(self):
        self.issue()
        with self.assertRaises(tool.FormError):
            self.store.issue(["another", "customer"])
        self.assertIsNone(self.store.get("another"))

    def test_activation_is_consumed_before_any_job_and_survives_restart(self):
        record, _ = self.issue()
        access = tool.AccessSessions(self.store)
        token, _, _ = access.login(record["key"], "first")
        self.assertTrue(token)
        self.assertIsNotNone(self.store.get("customer")["activated_at"])
        self.assertIsNone(self.store.get("customer")["claimed_job"])
        access.logout("kd_access=" + token)
        reopened = tool.KeyStore(self.path, export_dir=self.exports)
        self.assertIsNone(tool.AccessSessions(reopened).login(record["key"], "second")[0])

    def test_simultaneous_activation_has_exactly_one_winner(self):
        record, _ = self.issue()
        stores = [tool.KeyStore(self.path, export_dir=self.exports) for _ in range(8)]
        gate = threading.Barrier(8)
        def activate(index):
            gate.wait()
            return stores[index].activate(tool.access_key_hash(record["key"]), tool.secrets.token_hex(32)) is not None
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(activate, range(8))), 1)

    def test_only_activation_owner_can_claim_job(self):
        record, _ = self.issue()
        digest = tool.access_key_hash(record["key"])
        activation_id = self.activate(record)
        with self.assertRaises(tool.KeyAlreadyUsed):
            self.store.claim("customer", digest, "wrong-owner", tool.secrets.token_hex(32))
        self.store.claim("customer", digest, "correct-owner", activation_id)

    def test_existing_database_migrates_without_restoring_used_keys(self):
        old_path = self.path.with_name("old-schema.sqlite3")
        fresh, used = tool.create_access_key(), tool.create_access_key()
        with sqlite3.connect(old_path) as connection:
            connection.execute("""CREATE TABLE kd_access_keys (
                name TEXT PRIMARY KEY, sha256 TEXT NOT NULL UNIQUE, enabled INTEGER NOT NULL,
                created_at DOUBLE PRECISION NOT NULL, claimed_at DOUBLE PRECISION,
                claimed_job TEXT, finished_at DOUBLE PRECISION, outcome TEXT)""")
            connection.execute("INSERT INTO kd_access_keys (name,sha256,enabled,created_at) VALUES (?,?,1,1)",
                               ("fresh", tool.access_key_hash(fresh)))
            connection.execute("INSERT INTO kd_access_keys (name,sha256,enabled,created_at,claimed_job) VALUES (?,?,1,1,'old-job')",
                               ("used", tool.access_key_hash(used)))
        connection.close()
        migrated = tool.KeyStore(old_path, export_dir=self.exports)
        self.assertIsNone(migrated.get("fresh")["activated_at"])
        self.assertIsNotNone(migrated.activate(tool.access_key_hash(fresh), "fresh-activation"))
        self.assertIsNone(migrated.activate(tool.access_key_hash(used), "used-activation"))
        self.assertEqual(migrated.get("used")["claimed_job"], "old-job")


@unittest.skipUnless(os.environ.get("KD_TEST_DATABASE_URL"), "PostgreSQL test database not configured")
class PostgreSQLKeyTests(unittest.TestCase):
    def test_simultaneous_postgresql_activation_has_one_winner(self):
        with tempfile.TemporaryDirectory() as folder:
            url = os.environ["KD_TEST_DATABASE_URL"]
            stores = [tool.KeyStore(database_url=url, export_dir=folder) for _ in range(8)]
            name = "qa-race-" + tool.secrets.token_hex(8)
            records, _ = stores[0].issue([name])
            digest = tool.access_key_hash(records[0]["key"])
            gate = threading.Barrier(8)
            def activate(index):
                gate.wait()
                return stores[index].activate(digest, tool.secrets.token_hex(32)) is not None
            try:
                with ThreadPoolExecutor(max_workers=8) as pool:
                    self.assertEqual(sum(pool.map(activate, range(8))), 1)
            finally:
                stores[0].change(name, delete=True)

    def test_persistent_atomic_claim_with_real_postgresql(self):
        with tempfile.TemporaryDirectory() as folder:
            url = os.environ["KD_TEST_DATABASE_URL"]
            first = tool.KeyStore(database_url=url, export_dir=folder)
            second = tool.KeyStore(database_url=url, export_dir=folder)
            name = "qa-" + tool.secrets.token_hex(8)
            records, _ = first.issue([name])
            digest = tool.access_key_hash(records[0]["key"])
            activation_id = tool.secrets.token_hex(32)
            try:
                self.assertIsNotNone(first.activate(digest, activation_id))
                self.assertIsNone(second.activate(digest, "another-activation"))
                first.claim(name, digest, "postgres-job", activation_id)
                with self.assertRaises(tool.KeyAlreadyUsed):
                    second.claim(name, digest, "other-job", activation_id)
                second.finish(name, digest, "postgres-job", "completed")
                self.assertIsNone(first.unused_by_hash(digest))
                self.assertEqual(second.get(name)["outcome"], "completed")
            finally:
                first.change(name, delete=True)


if __name__ == "__main__":
    unittest.main()

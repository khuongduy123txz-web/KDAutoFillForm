import csv
from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from pathlib import Path
import sqlite3
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
        self.assertEqual(row["unlimited"], 1)
        self.assertNotIn(record["key"].encode(), self.path.read_bytes())

    def test_unlimited_key_allows_multiple_activations_and_batches(self):
        record, _ = self.issue()
        digest = tool.access_key_hash(record["key"])
        for index in range(3):
            activation = f"activation-{index}"
            self.assertIsNotNone(self.store.activate(digest, activation))
            self.store.claim("customer", digest, f"job-{index}", activation)
            self.store.finish("customer", digest, f"job-{index}", "completed")
        reopened = tool.KeyStore(self.path, export_dir=self.exports)
        self.assertIsNotNone(reopened.activate(digest, "after-restart"))

    def test_multiple_sessions_remain_valid_and_logout_does_not_consume_key(self):
        record, _ = self.issue()
        access = tool.AccessSessions(self.store)
        first, _, _ = access.login(record["key"], "first")
        second, _, _ = access.login(record["key"], "second")
        self.assertTrue(first)
        self.assertTrue(second)
        self.assertIsNotNone(access.current("kd_access=" + first))
        self.assertIsNotNone(access.current("kd_access=" + second))
        access.logout("kd_access=" + first)
        self.assertIsNone(access.current("kd_access=" + first))
        self.assertIsNotNone(access.current("kd_access=" + second))
        self.assertTrue(access.login(record["key"], "again")[0])

    def test_disabled_and_rotated_keys_revoke_sessions(self):
        first, _ = self.issue()
        access = tool.AccessSessions(self.store)
        token, _, _ = access.login(first["key"], "first")
        self.store.change("customer", enabled=False)
        self.assertIsNone(access.current("kd_access=" + token))
        self.assertIsNone(access.login(first["key"], "disabled")[0])
        self.store.change("customer", enabled=True)
        token, _, _ = access.login(first["key"], "enabled")
        records, _ = self.store.issue(["customer"], rotate=True)
        self.assertEqual(self.store.get("customer")["unlimited"], 1)
        self.assertIsNone(access.current("kd_access=" + token))
        self.assertIsNone(access.login(first["key"], "old")[0])
        self.assertTrue(access.login(records[0]["key"], "new")[0])

    def test_legacy_import_runs_once_and_deleted_keys_do_not_reappear(self):
        raw = tool.create_access_key()
        seed = {"legacy": {"sha256": tool.access_key_hash(raw), "enabled": True}}
        other_path = self.path.with_name("legacy.sqlite3")
        seeded = tool.KeyStore(other_path, export_dir=self.exports, legacy=seed)
        self.assertIsNotNone(seeded.get("legacy"))
        seeded.change("legacy", delete=True)
        reopened = tool.KeyStore(other_path, export_dir=self.exports, legacy=seed)
        self.assertIsNone(reopened.get("legacy"))

    def test_duplicate_batch_rolls_back_without_half_created_keys(self):
        self.issue()
        with self.assertRaises(tool.FormError):
            self.store.issue(["another", "customer"])
        self.assertIsNone(self.store.get("another"))

    def test_older_schema_migrates_unused_keys_but_does_not_restore_used_keys(self):
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
        self.assertEqual(migrated.get("fresh")["unlimited"], 1)
        self.assertEqual(migrated.get("used")["unlimited"], 0)
        self.assertIsNotNone(migrated.activate(tool.access_key_hash(fresh), "fresh-login"))
        self.assertIsNone(migrated.activate(tool.access_key_hash(used), "used-login"))
        reopened = tool.KeyStore(old_path, export_dir=self.exports)
        self.assertEqual(reopened.get("used")["claimed_job"], "old-job")
        self.assertIsNone(reopened.activate(tool.access_key_hash(used), "again"))

    def test_activated_legacy_key_remains_retired_until_rotated(self):
        old_path = self.path.with_name("activated.sqlite3")
        raw = tool.create_access_key()
        with sqlite3.connect(old_path) as connection:
            connection.execute("""CREATE TABLE kd_access_keys (
                name TEXT PRIMARY KEY, sha256 TEXT NOT NULL UNIQUE, enabled INTEGER NOT NULL,
                created_at DOUBLE PRECISION NOT NULL, claimed_at DOUBLE PRECISION,
                claimed_job TEXT, finished_at DOUBLE PRECISION, outcome TEXT,
                activated_at DOUBLE PRECISION, activation_id TEXT)""")
            connection.execute("""INSERT INTO kd_access_keys
                (name,sha256,enabled,created_at,activated_at,activation_id)
                VALUES (?,?,1,1,2,'old-session')""", ("retired", tool.access_key_hash(raw)))
        connection.close()
        migrated = tool.KeyStore(old_path, export_dir=self.exports)
        self.assertEqual(migrated.get("retired")["unlimited"], 0)
        self.assertIsNone(migrated.activate(tool.access_key_hash(raw), "new-session"))
        records, _ = migrated.issue(["retired"], rotate=True)
        self.assertIsNotNone(migrated.activate(tool.access_key_hash(records[0]["key"]), "new-key"))

    def test_guest_quota_is_cumulative_and_survives_reopen(self):
        guest = hashlib.sha256(b"guest-one").hexdigest()
        self.assertIsNone(self.store.guest_usage(guest))
        usage = self.store.ensure_guest(guest)
        self.assertEqual((usage["used"], usage["remaining"], usage["limit"]), (0, 10, 10))
        for index in range(10):
            usage = self.store.consume_guest(guest)
            self.assertEqual(usage["used"], index + 1)
            self.assertEqual(usage["remaining"], 9 - index)
        with self.assertRaises(tool.QuotaExceeded):
            self.store.consume_guest(guest)
        reopened = tool.KeyStore(self.path, export_dir=self.exports)
        self.assertEqual(reopened.ensure_guest(guest)["remaining"], 0)
        with self.assertRaises(tool.QuotaExceeded):
            reopened.consume_guest(guest)

    def test_guest_quota_atomic_across_independent_stores(self):
        guest = hashlib.sha256(b"guest-race").hexdigest()
        self.store.ensure_guest(guest)
        stores = [tool.KeyStore(self.path, export_dir=self.exports) for _ in range(20)]
        gate = threading.Barrier(len(stores))
        def consume(index):
            gate.wait()
            try:
                stores[index].consume_guest(guest)
                return True
            except tool.QuotaExceeded:
                return False
        with ThreadPoolExecutor(max_workers=len(stores)) as pool:
            self.assertEqual(sum(pool.map(consume, range(len(stores)))), 10)
        self.assertEqual(self.store.guest_usage(guest)["used"], 10)


@unittest.skipUnless(os.environ.get("KD_TEST_DATABASE_URL"), "PostgreSQL test database not configured")
class PostgreSQLKeyTests(unittest.TestCase):
    def test_postgresql_unlimited_key_survives_multiple_logins_and_reopen(self):
        with tempfile.TemporaryDirectory() as folder:
            first = tool.KeyStore(database_url=os.environ["KD_TEST_DATABASE_URL"], export_dir=folder)
            second = tool.KeyStore(database_url=os.environ["KD_TEST_DATABASE_URL"], export_dir=folder)
            name = "qa-" + tool.secrets.token_hex(8)
            records, _ = first.issue([name])
            digest = tool.access_key_hash(records[0]["key"])
            try:
                for index, store in enumerate((first, second, first)):
                    self.assertIsNotNone(store.activate(digest, f"activation-{index}"))
                    store.claim(name, digest, f"job-{index}", f"activation-{index}")
                    store.finish(name, digest, f"job-{index}", "completed")
            finally:
                first.change(name, delete=True)

    def test_postgresql_guest_quota_is_atomic_across_connections(self):
        with tempfile.TemporaryDirectory() as folder:
            stores = [tool.KeyStore(database_url=os.environ["KD_TEST_DATABASE_URL"], export_dir=folder)
                      for _ in range(20)]
            guest = hashlib.sha256(tool.secrets.token_bytes(32)).hexdigest()
            stores[0].ensure_guest(guest)
            gate = threading.Barrier(len(stores))
            def consume(index):
                gate.wait()
                try:
                    stores[index].consume_guest(guest)
                    return True
                except tool.QuotaExceeded:
                    return False
            try:
                with ThreadPoolExecutor(max_workers=len(stores)) as pool:
                    self.assertEqual(sum(pool.map(consume, range(len(stores)))), 10)
                self.assertEqual(stores[0].guest_usage(guest)["remaining"], 0)
            finally:
                with stores[0].connection() as connection:
                    stores[0].execute(connection, "DELETE FROM kd_guest_usage WHERE guest_id = ?", (guest,))


if __name__ == "__main__":
    unittest.main()

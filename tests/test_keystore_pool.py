"""KeyStore transaction boundaries over the real bounded pool with a fake driver."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import google_form_tool as tool


class FakeCursor:
    def __init__(self, rowcount=1):
        self.rowcount = rowcount


class TransactionConnection:
    def __init__(self):
        self.closed = False
        self.broken = False
        self.committed = {}
        self.pending = {}
        self.executions = []
        self.commit_error = None
        self.rollback_error = None
        self.commits = 0
        self.rollbacks = 0
        self.closes = 0

    def execute(self, sql, parameters=()):
        self.executions.append((sql, tuple(parameters)))
        if sql.startswith("INSERT INTO kd_access_keys"):
            name = parameters[0]
            if name in self.committed or name in self.pending:
                return FakeCursor(0)
            self.pending[name] = parameters[1]
        elif sql == "WRITE":
            self.pending[parameters[0]] = parameters[1]
        return FakeCursor()

    def commit(self):
        self.commits += 1
        if self.commit_error:
            # Model the ambiguous outcome: a server may commit before a broken
            # socket prevents the client receiving the successful response.
            self.committed.update(self.pending)
            self.pending.clear()
            raise self.commit_error
        self.committed.update(self.pending)
        self.pending.clear()

    def rollback(self):
        self.rollbacks += 1
        if self.rollback_error:
            raise self.rollback_error
        self.pending.clear()

    def close(self):
        self.closes += 1
        self.closed = True


class KeyStorePoolBoundaryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.connections = []
        def connect(*args, **kwargs):
            connection = TransactionConnection()
            self.connections.append(connection)
            return connection
        self.connector = Mock(side_effect=connect)
        driver = patch("psycopg.connect", self.connector)
        driver.start()
        self.addCleanup(driver.stop)

    def store(self):
        # Schema migration has its own integration tests. Keep this suite
        # focused on real KeyStore commit/rollback/release behavior.
        with patch.object(tool.KeyStore, "initialize", return_value=None):
            store = tool.KeyStore(self.folder / "unused.sqlite3",
                                  database_url="postgresql://qa.invalid/test",
                                  export_dir=self.folder / "private")
        self.addCleanup(store.close)
        return store

    def test_committed_transactions_reuse_connection_without_extra_sql(self):
        store = self.store()
        for name in ("first", "second"):
            with store.connection() as connection:
                connection.execute("WRITE", (name, 1))
        self.assertIs(connection, self.connections[0])
        self.assertEqual(self.connector.call_count, 1)
        self.assertEqual(connection.committed, {"first": 1, "second": 1})
        self.assertEqual(connection.commits, 2)
        self.assertEqual(connection.rollbacks, 0)
        self.assertEqual(len(connection.executions), 2)
        self.assertEqual(store._pool.stats()["idle"], 1)
        self.assertEqual(store._pool.stats()["leased"], 0)
        store.close()
        self.assertTrue(connection.closed)

    def test_duplicate_batch_rolls_back_partial_insert_without_replaying_or_exporting(self):
        store = self.store()
        store.issue(["existing"])
        with self.assertRaises(tool.FormError):
            store.issue(["partial", "existing"])
        connection = self.connections[0]
        self.assertEqual(set(connection.committed), {"existing"})
        self.assertEqual(connection.pending, {})
        self.assertEqual(connection.rollbacks, 1)
        self.assertEqual(len(list(store.export_dir.glob("*.csv"))), 1)
        self.assertEqual([parameters[0] for sql, parameters in connection.executions
                          if sql.startswith("INSERT INTO kd_access_keys")],
                         ["existing", "partial", "existing"])
        store.issue(["later"])
        self.assertEqual(set(connection.committed), {"existing", "later"})
        self.assertEqual(self.connector.call_count, 1)

    def test_ambiguous_commit_failure_discards_connection_without_retry(self):
        store = self.store()
        failure = ConnectionError("commit response lost")
        with self.assertRaises(tool.FormError) as captured:
            with store.connection() as connection:
                connection.commit_error = failure
                connection.execute("WRITE", ("only-once", 1))
        self.assertIs(captured.exception.__cause__, failure)
        self.assertEqual(connection.committed, {"only-once": 1})
        self.assertEqual(connection.commits, 1)
        self.assertEqual(connection.rollbacks, 1)
        self.assertEqual(connection.executions, [("WRITE", ("only-once", 1))])
        self.assertTrue(connection.closed)
        self.assertEqual(self.connector.call_count, 1)
        self.assertEqual(store._pool.stats()["total"], 0)
        with store.connection() as fresh:
            self.assertIsNot(fresh, connection)
        self.assertEqual(self.connector.call_count, 2)
        self.assertEqual(fresh.executions, [])

    def test_rollback_failure_discards_connection_and_preserves_original_form_error(self):
        store = self.store()
        failure = tool.FormError("duplicate name")
        with self.assertRaises(tool.FormError) as captured:
            with store.connection() as connection:
                connection.execute("WRITE", ("partial", 1))
                connection.rollback_error = OSError("rollback socket closed")
                raise failure
        self.assertIs(captured.exception, failure)
        self.assertEqual(connection.commits, 0)
        self.assertEqual(connection.rollbacks, 1)
        self.assertTrue(connection.closed)
        self.assertEqual(store._pool.stats()["total"], 0)
        self.assertEqual(self.connector.call_count, 1)

    def test_driver_error_is_wrapped_once_and_not_replayed_even_when_rollback_fails(self):
        store = self.store()
        failure = OSError("statement socket closed")
        with self.assertRaises(tool.FormError) as captured:
            with store.connection() as connection:
                connection.execute("WRITE", ("partial", 1))
                connection.rollback_error = RuntimeError("rollback unavailable")
                raise failure
        self.assertIs(captured.exception.__cause__, failure)
        self.assertTrue(connection.closed)
        self.assertEqual(connection.rollbacks, 1)
        self.assertEqual(connection.commits, 0)
        self.assertEqual(len(connection.executions), 1)
        self.assertEqual(self.connector.call_count, 1)

    def test_body_base_exception_rolls_back_and_releases_lease_without_wrapping(self):
        store = self.store()
        failure = KeyboardInterrupt("cancel transaction")
        with self.assertRaises(KeyboardInterrupt) as captured:
            with store.connection() as connection:
                connection.execute("WRITE", ("partial", 1))
                raise failure
        self.assertIs(captured.exception, failure)
        self.assertEqual(connection.rollbacks, 1)
        self.assertEqual(connection.pending, {})
        self.assertEqual(connection.commits, 0)
        self.assertEqual(store._pool.stats()["leased"], 0)
        with store.connection() as reused:
            self.assertIs(reused, connection)
        self.assertEqual(self.connector.call_count, 1)

    def test_base_exception_during_commit_discards_ambiguous_connection(self):
        store = self.store()
        failure = KeyboardInterrupt("interrupted commit")
        with self.assertRaises(KeyboardInterrupt) as captured:
            with store.connection() as connection:
                connection.execute("WRITE", ("only-once", 1))
                connection.commit_error = failure
        self.assertIs(captured.exception, failure)
        self.assertTrue(connection.closed)
        self.assertEqual(store._pool.stats()["total"], 0)
        self.assertEqual(connection.commits, 1)
        self.assertEqual(len(connection.executions), 1)

    def test_base_exception_during_rollback_discards_unreset_connection(self):
        store = self.store()
        with self.assertRaises(BaseException):
            with store.connection() as connection:
                connection.execute("WRITE", ("partial", 1))
                connection.rollback_error = KeyboardInterrupt("interrupted rollback")
                raise tool.FormError("reject transaction")
        self.assertTrue(connection.closed)
        self.assertEqual(store._pool.stats()["total"], 0)
        self.assertEqual(connection.rollbacks, 1)
        self.assertEqual(self.connector.call_count, 1)

    def test_initialization_failure_closes_idle_connection_in_constructed_pool(self):
        failure = tool.FormError("migration failed")
        def fail_initialize(store, legacy):
            with store.connection() as connection:
                connection.execute("WRITE", ("migration", 1))
            raise failure
        with patch.object(tool.KeyStore, "initialize", fail_initialize):
            with self.assertRaises(tool.FormError) as captured:
                tool.KeyStore(database_url="postgresql://qa.invalid/test")
        self.assertIs(captured.exception, failure)
        self.assertEqual(self.connector.call_count, 1)
        self.assertTrue(self.connections[0].closed)
        self.assertEqual(self.connections[0].closes, 1)

    def test_interrupted_idle_healthcheck_discards_connection_and_frees_pool_slot(self):
        store = self.store()
        with store.connection() as connection:
            pass
        store._pool.idle_check_seconds = 0
        failure = KeyboardInterrupt("healthcheck interrupted")
        connection.execute = Mock(side_effect=failure)
        entered = []
        with self.assertRaises(KeyboardInterrupt) as captured:
            with store.connection():
                entered.append(True)
        self.assertIs(captured.exception, failure)
        self.assertEqual(entered, [])
        connection.execute.assert_called_once_with("SELECT 1")
        self.assertTrue(connection.closed)
        self.assertEqual(store._pool.stats()["total"], 0)
        self.assertEqual(store._pool.stats()["leased"], 0)
        self.assertEqual(self.connector.call_count, 1)
        with store.connection() as fresh:
            self.assertIsNot(fresh, connection)
        self.assertEqual(self.connector.call_count, 2)


if __name__ == "__main__":
    unittest.main()

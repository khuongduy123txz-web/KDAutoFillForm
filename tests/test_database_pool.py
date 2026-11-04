import os
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

from database_pool import ConnectionPool, PoolClosed, PoolTimeout


class FakeConnection:
    def __init__(self):
        self.closed = False
        self.broken = False
        self.execute = Mock(return_value=Mock())
        self.rollback = Mock()
        self.close = Mock(side_effect=self._close)

    def _close(self):
        self.closed = True


class DatabasePoolTests(unittest.TestCase):
    def pool(self, **kwargs):
        connector = Mock(side_effect=FakeConnection)
        pool = ConnectionPool(connector, **kwargs)
        self.addCleanup(pool.close)
        return pool, connector

    def test_short_idle_checkout_reuses_connection_without_extra_query(self):
        pool, connector = self.pool()
        first = pool.acquire()
        pool.release(first)
        second = pool.acquire()
        self.assertIs(first, second)
        first.execute.assert_not_called()
        connector.assert_called_once_with()
        pool.release(second)

    def test_stale_connection_is_checked_and_transaction_reset_before_lease(self):
        pool, _ = self.pool(idle_check_seconds=0)
        first = pool.acquire()
        pool.release(first)
        self.assertIs(pool.acquire(), first)
        first.execute.assert_called_once_with("SELECT 1")
        first.execute.return_value.fetchone.assert_called_once_with()
        first.execute.return_value.close.assert_called_once_with()
        first.rollback.assert_called_once_with()
        pool.release(first)

    def test_failed_idle_healthcheck_is_replaced_before_caller_operation(self):
        pool, connector = self.pool(idle_check_seconds=0)
        first = pool.acquire()
        pool.release(first)
        first.execute.side_effect = ConnectionError("server closed idle socket")
        replacement = pool.acquire()
        self.assertIsNot(replacement, first)
        first.close.assert_called_once_with()
        self.assertEqual(connector.call_count, 2)
        self.assertEqual(pool.stats()["total"], 1)
        pool.release(replacement)

    def test_broken_return_is_discarded_without_replaying_application_operation(self):
        pool, connector = self.pool()
        first = pool.acquire()
        first.execute.side_effect = ConnectionError("ambiguous write failure")
        with self.assertRaises(ConnectionError):
            first.execute("UPDATE application SET value = 1")
        pool.release(first, broken=True)
        first.execute.assert_called_once()
        self.assertEqual(connector.call_count, 1)
        self.assertEqual(pool.stats()["total"], 0)

    def test_failed_creation_releases_reserved_capacity(self):
        connector = Mock(side_effect=[ConnectionError("offline"), FakeConnection()])
        pool = ConnectionPool(connector, max_size=1)
        self.addCleanup(pool.close)
        with self.assertRaises(ConnectionError):
            pool.acquire()
        self.assertEqual(pool.stats()["total"], 0)
        connection = pool.acquire()
        pool.release(connection)
        self.assertEqual(pool.stats()["total"], 1)

    def test_capacity_is_bounded_and_waiter_can_proceed_after_return(self):
        pool, connector = self.pool(max_size=2, acquire_timeout=1)
        held = [pool.acquire(), pool.acquire()]
        entered = threading.Event()
        def checkout():
            entered.set()
            connection = pool.acquire()
            pool.release(connection)
            return connection
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(checkout)
            self.assertTrue(entered.wait(1))
            self.assertFalse(pending.done())
            self.assertEqual(connector.call_count, 2)
            pool.release(held.pop())
            self.assertIsNotNone(pending.result(2))
        pool.release(held.pop())
        self.assertEqual(pool.stats()["total"], 2)

    def test_acquisition_timeout_does_not_leak_capacity(self):
        pool, connector = self.pool(max_size=1, acquire_timeout=0.03)
        held = pool.acquire()
        with self.assertRaises(PoolTimeout):
            pool.acquire()
        self.assertEqual(connector.call_count, 1)
        self.assertEqual(pool.stats()["leased"], 1)
        pool.release(held)

    def test_close_drains_idle_and_closes_active_when_returned(self):
        pool, _ = self.pool(max_size=2)
        idle, active = pool.acquire(), pool.acquire()
        pool.release(idle)
        pool.close()
        self.assertTrue(idle.closed)
        self.assertFalse(active.closed)
        with self.assertRaises(PoolClosed):
            pool.acquire()
        pool.release(active)
        self.assertTrue(active.closed)
        self.assertEqual(pool.stats()["total"], 0)
        pool.close()

    def test_close_wakes_waiter_without_allocating_another_connection(self):
        pool, connector = self.pool(max_size=1, acquire_timeout=2)
        held = pool.acquire()
        ready = threading.Event()
        def wait():
            ready.set()
            return pool.acquire()
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(wait)
            self.assertTrue(ready.wait(1))
            pool.close()
            with self.assertRaises(PoolClosed):
                pending.result(1)
        pool.release(held)
        connector.assert_called_once()

    def test_close_during_creation_closes_new_connection_and_releases_reservation(self):
        ready, finish = threading.Event(), threading.Event()
        connection = FakeConnection()
        def connect():
            ready.set()
            if not finish.wait(2):
                raise RuntimeError("test connector timed out")
            return connection
        pool = ConnectionPool(connect, max_size=1)
        self.addCleanup(pool.close)
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(pool.acquire)
            self.assertTrue(ready.wait(1))
            pool.close()
            finish.set()
            with self.assertRaises(PoolClosed):
                pending.result(1)
        self.assertTrue(connection.closed)
        self.assertEqual(pool.stats()["total"], 0)

    def test_one_connection_is_never_leased_twice_under_contention(self):
        pool, connector = self.pool(max_size=3)
        active, lock = set(), threading.Lock()
        def use(_):
            for _ in range(10):
                connection = pool.acquire()
                with lock:
                    self.assertNotIn(id(connection), active)
                    active.add(id(connection))
                    self.assertLessEqual(len(active), 3)
                time.sleep(0.001)
                with lock:
                    active.remove(id(connection))
                pool.release(connection)
        with ThreadPoolExecutor(max_workers=12) as executor:
            list(executor.map(use, range(12)))
        self.assertLessEqual(connector.call_count, 3)
        self.assertEqual(pool.stats()["leased"], 0)


@unittest.skipUnless(os.environ.get("KD_TEST_DATABASE_URL"), "Set KD_TEST_DATABASE_URL")
class PostgreSQLPoolTests(unittest.TestCase):
    def test_real_connection_reuse_transaction_rollback_and_healthcheck(self):
        import psycopg
        connector = Mock(side_effect=lambda: psycopg.connect(os.environ["KD_TEST_DATABASE_URL"], connect_timeout=15))
        pool = ConnectionPool(connector, max_size=1, idle_check_seconds=0)
        self.addCleanup(pool.close)
        first = pool.acquire()
        first.execute("CREATE TEMP TABLE kd_pool_qa (value INTEGER)")
        first.commit()
        first.execute("INSERT INTO kd_pool_qa VALUES (7)")
        first.rollback()
        pool.release(first)
        second = pool.acquire()
        self.assertIs(first, second)
        self.assertEqual(second.execute("SELECT count(*) FROM kd_pool_qa").fetchone()[0], 0)
        second.commit()
        pool.release(second)
        connector.assert_called_once()


if __name__ == "__main__":
    unittest.main()

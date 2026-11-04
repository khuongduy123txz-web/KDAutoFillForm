"""Small bounded PostgreSQL connection pool, without retrying transactions.

The caller owns commit/rollback. Checkout may replace an unhealthy idle
connection before any application SQL runs; an operation is never replayed.
"""
from __future__ import annotations

from collections import deque
import math
import threading
import time


class PoolClosed(RuntimeError):
    pass


class PoolTimeout(TimeoutError):
    pass


class ConnectionPool:
    def __init__(self, connector, max_size=4, acquire_timeout=15, idle_check_seconds=30):
        if isinstance(max_size, bool) or not isinstance(max_size, int) or max_size < 1:
            raise ValueError("max_size must be a positive integer")
        if not math.isfinite(acquire_timeout) or acquire_timeout <= 0:
            raise ValueError("acquire_timeout must be positive and finite")
        if not math.isfinite(idle_check_seconds) or idle_check_seconds < 0:
            raise ValueError("idle_check_seconds must be nonnegative and finite")
        self.connector = connector
        self.max_size = max_size
        self.acquire_timeout = acquire_timeout
        self.idle_check_seconds = idle_check_seconds
        self._condition = threading.Condition()
        self._idle = deque()
        self._leased = {}
        # Includes connections still being created or closed outside the lock.
        self._total = 0
        self._closed = False

    @staticmethod
    def _healthy(connection):
        try:
            return not connection.closed and not getattr(connection, "broken", False)
        except Exception:
            return False

    @staticmethod
    def _close(connection):
        try:
            connection.close()
        except Exception:
            pass

    def _finished_close(self, connection):
        self._close(connection)
        with self._condition:
            self._total -= 1
            self._condition.notify_all()

    def acquire(self):
        deadline = time.monotonic() + self.acquire_timeout
        while True:
            connection = None
            idle_since = None
            create = False
            with self._condition:
                while True:
                    if self._closed:
                        raise PoolClosed("The database pool is closed")
                    if self._idle:
                        connection, idle_since = self._idle.pop()
                        self._leased[id(connection)] = connection
                        break
                    if self._total < self.max_size:
                        self._total += 1
                        create = True
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise PoolTimeout("Timed out waiting for a database connection")
                    self._condition.wait(remaining)

            if create:
                try:
                    connection = self.connector()
                except BaseException:
                    with self._condition:
                        self._total -= 1
                        self._condition.notify_all()
                    raise
                with self._condition:
                    self._leased[id(connection)] = connection
                if not self._healthy(connection):
                    self.release(connection, broken=True)
                    raise RuntimeError("The database connector returned a closed connection")
            else:
                healthy = self._healthy(connection)
                if healthy and time.monotonic() - idle_since >= self.idle_check_seconds:
                    try:
                        # Leave no healthcheck transaction or result pending.
                        cursor = connection.execute("SELECT 1")
                        try:
                            cursor.fetchone()
                        finally:
                            cursor.close()
                        connection.rollback()
                    except Exception:
                        healthy = False
                    except BaseException:
                        self.release(connection, broken=True)
                        raise
                if not healthy:
                    self.release(connection, broken=True)
                    if time.monotonic() >= deadline:
                        raise PoolTimeout("Timed out renewing a database connection")
                    continue
            with self._condition:
                closed = self._closed
            if closed:
                self.release(connection, broken=True)
                raise PoolClosed("The database pool is closed")
            return connection

    def release(self, connection, broken=False):
        with self._condition:
            if self._leased.pop(id(connection), None) is not connection:
                raise ValueError("Connection was not leased by this pool")
            discard = broken or self._closed or not self._healthy(connection)
            if not discard:
                self._idle.append((connection, time.monotonic()))
                self._condition.notify()
        if discard:
            self._finished_close(connection)

    def close(self):
        with self._condition:
            self._closed = True
            idle = [connection for connection, _ in self._idle]
            self._idle.clear()
            self._condition.notify_all()
        for connection in idle:
            self._finished_close(connection)

    def stats(self):
        with self._condition:
            return {"total": self._total, "idle": len(self._idle),
                    "leased": len(self._leased), "closed": self._closed}

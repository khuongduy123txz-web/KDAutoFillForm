"""Fair, bounded workers and cancellable resource slots for multi-user form work.

Queue entries contain callbacks and metadata only. TaskDispatcher owns a fixed
number of daemon threads; enqueueing another visitor never starts another worker.
Callbacks, readiness hooks and worker cleanup always run outside queue locks.
The queue is in-process; persistence and cross-process coordination belong to the
application's database layer.
"""
from __future__ import annotations

from collections import OrderedDict, deque
from concurrent.futures import CancelledError, Future
from contextlib import contextmanager
from dataclasses import dataclass, field
import logging
import math
import threading
import time


log = logging.getLogger(__name__)


def _capacity(value):
    if type(value) is not int or value < 1:
        raise ValueError("Worker capacity must be a positive integer.")
    return value


def _token(value):
    if not isinstance(value, str) or not value:
        raise ValueError("Work token must be a nonempty string.")
    return value


def _seconds(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("Work delay must be a finite, nonnegative number.")
    return float(value)


@dataclass(eq=False)
class _Task:
    token: str
    callback: object
    due: float
    ready: object = None
    future: Future = field(default_factory=Future)
    deferred_until: float = 0.0
    ready_state: object = None


class TaskDispatcher:
    """FIFO among due, ready tasks, with a fixed number of worker threads.

    A running callback may submit one continuation under its own token. That
    continuation goes to the end of the queue and cannot run until the current
    callback returns. Repeated submits while a continuation is pending raise
    ValueError, so the same respondent never executes twice in parallel.

    The ready hook should be a fast predicate (for example not control.paused);
    it must not block or perform database/browser work. False predicates defer
    the task without consuming a worker slot or blocking later tasks. Call wake
    after pause/resume changes for immediate reevaluation; polling is a fallback
    for flags/events changed without a wake notification.
    """

    def __init__(self, capacity=1, *, poll_seconds=.25, on_worker_exit=None, name="kd-work"):
        self.capacity = _capacity(capacity)
        self.poll_seconds = _seconds(poll_seconds)
        if not self.poll_seconds or self.poll_seconds > 2:
            raise ValueError("Queue poll interval must be greater than zero and at most two seconds.")
        self._condition = threading.Condition()
        self._pending = OrderedDict()
        self._active = {}
        self._checking = set()
        self._cancelled = set()
        self._closing = False
        self._cancel_pending = True
        self._on_worker_exit = on_worker_exit
        self._threads = [threading.Thread(target=self._worker, daemon=True,
                         name=f"{name}-{index + 1}") for index in range(self.capacity)]
        for thread in self._threads:
            thread.start()

    def submit(self, token, callback, *, delay=0, due_monotonic=None, ready=None):
        """Enqueue one callback, returning its Future; never create a new thread."""
        token = _token(token)
        if not callable(callback) or (ready is not None and not callable(ready)):
            raise ValueError("Task callback and readiness hook must be callable.")
        delay = _seconds(delay)
        if due_monotonic is None:
            due = time.monotonic() + delay
        else:
            if delay or type(due_monotonic) not in (int, float) or not math.isfinite(due_monotonic):
                raise ValueError("Use either delay or a finite monotonic deadline.")
            due = float(due_monotonic)
        task = _Task(token, callback, due, ready)
        task.future.add_done_callback(lambda future: self._future_done(task, future))
        with self._condition:
            if self._closing:
                raise RuntimeError("The work dispatcher is closed.")
            if token in self._cancelled:
                raise CancelledError("This work token was cancelled.")
            if token in self._pending:
                raise ValueError("This work token already has a pending callback.")
            self._pending[token] = task
            self._condition.notify_all()
        return task.future

    def _future_done(self, task, future):
        # A caller may cancel the Future directly rather than through cancel().
        # Remove exactly this generation, never another continuation's ticket.
        if future.cancelled():
            with self._condition:
                if self._pending.get(task.token) is task:
                    self._pending.pop(task.token)
                self._condition.notify_all()

    def cancel(self, token):
        """Remove queued work; running work must observe its application stop flag."""
        token = _token(token)
        with self._condition:
            pending = self._pending.pop(token, None)
            active = self._active.get(token)
            if active is not None:
                self._cancelled.add(token)
            self._condition.notify_all()
        # Future callbacks may execute application code, so never cancel under lock.
        if pending is not None:
            pending.future.cancel()
        if active is not None:
            active.future.cancel()  # Succeeds only before the callback actually starts.
        return pending is not None or active is not None

    def wake(self):
        """Reevaluate delayed readiness checks immediately after control changes."""
        with self._condition:
            for task in self._pending.values():
                task.deferred_until = 0.0
            self._condition.notify_all()

    def reschedule(self, token, *, due_monotonic):
        """Update a pending deadline in place, retaining the FIFO ticket order."""
        token = _token(token)
        if type(due_monotonic) not in (int, float) or not math.isfinite(due_monotonic):
            raise ValueError("Work deadline must be a finite monotonic timestamp.")
        with self._condition:
            task = self._pending.get(token)
            if task is None or token in self._active:
                return False
            task.due = float(due_monotonic)
            task.deferred_until = 0.0
            self._condition.notify_all()
            return True

    def snapshot(self, token=None):
        """Return counts and only this token's position, without exposing other IDs."""
        with self._condition:
            pending = self._pending.get(token)
            active = token in self._active if token is not None else False
            now = time.monotonic()
            eligible = [key for key, task in self._pending.items()
                        if key not in self._active and task.due <= now and task.ready_state is not False]
            position = 0 if active else (
                next((index for index, value in enumerate(eligible, 1) if value == token), None)
                if token is not None else None)
            return {"capacity": self.capacity, "running": len(self._active),
                    "waiting": len(self._pending), "queue_position": position,
                    "queued": pending is not None, "active": active,
                    "due_in_seconds": max(0.0, pending.due - time.monotonic()) if pending else 0.0}

    def _take(self):
        while True:
            with self._condition:
                if self._closing and (self._cancel_pending or not self._pending):
                    return None
                now = time.monotonic()
                task = next((task for token, task in self._pending.items()
                             if token not in self._active and token not in self._checking
                             and task.due <= now and task.deferred_until <= now), None)
                if task is None:
                    deadlines = [max(task.due, task.deferred_until)
                                 for token, task in self._pending.items()
                                 if token not in self._active and token not in self._checking]
                    timeout = self.poll_seconds
                    if deadlines:
                        timeout = min(timeout, max(.001, min(deadlines) - now))
                    self._condition.wait(timeout)
                    continue
                self._checking.add(task.token)
            readiness_error = None
            try:
                ready = task.ready is None or bool(task.ready())
            except BaseException as exc:
                ready, readiness_error = False, exc
            with self._condition:
                self._checking.discard(task.token)
                # Cancellation/shutdown may have removed or replaced the task
                # while its external readiness predicate was running.
                if self._pending.get(task.token) is not task:
                    self._condition.notify_all()
                    continue
                task.ready_state = ready
                if readiness_error is not None:
                    self._pending.pop(task.token)
                elif not ready:
                    task.deferred_until = time.monotonic() + self.poll_seconds
                else:
                    self._pending.pop(task.token)
                    self._active[task.token] = task
                    return task
            if readiness_error is not None and task.future.set_running_or_notify_cancel():
                task.future.set_exception(readiness_error)

    def _worker(self):
        try:
            while True:
                task = self._take()
                if task is None:
                    return
                try:
                    if task.future.set_running_or_notify_cancel():
                        try:
                            result = task.callback()
                        except BaseException as exc:
                            task.future.set_exception(exc)
                        else:
                            task.future.set_result(result)
                finally:
                    with self._condition:
                        if self._active.get(task.token) is task:
                            self._active.pop(task.token)
                        self._cancelled.discard(task.token)
                        self._condition.notify_all()
        finally:
            if self._on_worker_exit is not None:
                try:
                    self._on_worker_exit()
                except Exception:
                    log.exception("Browser worker cleanup failed")

    def shutdown(self, wait=True, *, cancel_pending=True):
        """Close admission, cancel pending tasks by default, and finish active calls."""
        with self._condition:
            self._closing = True
            self._cancel_pending = bool(cancel_pending)
            pending = list(self._pending.values()) if cancel_pending else []
            if cancel_pending:
                self._pending.clear()
            self._condition.notify_all()
        for task in pending:
            task.future.cancel()
        if wait:
            current = threading.current_thread()
            for thread in self._threads:
                if thread is not current:
                    thread.join()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.shutdown()


@dataclass(eq=False)
class _Ticket:
    token: str
    cancelled: bool = False


class FairWorkQueue:
    """Manual FIFO resource slots for existing worker code.

    Unlike TaskDispatcher, this primitive does not own threads. Scheduled waits
    should happen before entering acquire. Paused waiters temporarily leave the
    queue and rejoin at its tail on resume; they never block other visitors.
    """

    def __init__(self, capacity=1, *, poll_seconds=.25):
        self.capacity = _capacity(capacity)
        self.poll_seconds = _seconds(poll_seconds)
        if not 0 < self.poll_seconds <= 2:
            raise ValueError("Queue poll interval must be greater than zero and at most two seconds.")
        self._condition = threading.Condition()
        self._pending = deque()
        self._registered = {}
        self._active = {}
        self._closed = False

    def snapshot(self, token=None):
        with self._condition:
            active = token in self._active if token is not None else False
            position = 0 if active else next((index for index, ticket in enumerate(self._pending, 1)
                                             if ticket.token == token), None)
            return {"capacity": self.capacity, "running": len(self._active),
                    "waiting": len(self._pending), "queue_position": position,
                    "queued": any(ticket.token == token for ticket in self._pending),
                    "active": active}

    def notify(self):
        with self._condition:
            self._condition.notify_all()

    wake = notify

    def cancel(self, token):
        with self._condition:
            ticket = self._registered.get(token)
            if ticket is None:
                return False
            ticket.cancelled = True
            if ticket in self._pending:
                self._pending.remove(ticket)
            self._condition.notify_all()
            return True

    @contextmanager
    def acquire(self, token, *, stop_event=None, paused=None, checkpoint=None, on_state=None):
        token = _token(token)
        # The ordinary RunControl checkpoint may wait through a pause. Execute
        # it before registering any queue position or reserving browser capacity.
        if checkpoint is not None and checkpoint() is False:
            raise CancelledError("Work stopped before queue admission.")
        ticket = _Ticket(token)
        with self._condition:
            if self._closed:
                raise RuntimeError("The resource queue is closed.")
            if token in self._registered:
                raise ValueError("This work token already owns or awaits a slot.")
            self._registered[token] = ticket
            self._pending.append(ticket)
            self._condition.notify_all()
        previous_state = None
        acquired = False
        try:
            while not acquired:
                if stop_event is not None and stop_event.is_set():
                    raise CancelledError("Queued work stopped.")
                is_paused = bool(paused and paused())
                with self._condition:
                    if self._closed or ticket.cancelled:
                        raise CancelledError("Queued work cancelled.")
                    if is_paused:
                        if ticket in self._pending:
                            self._pending.remove(ticket)
                            self._condition.notify_all()
                    else:
                        if ticket not in self._pending:
                            self._pending.append(ticket)
                        if len(self._active) < self.capacity and self._pending[0] is ticket:
                            self._pending.popleft()
                            self._active[token] = ticket
                            acquired = True
                            self._condition.notify_all()
                state = self.snapshot(token)
                if on_state is not None and state != previous_state:
                    previous_state = state
                    on_state(state)
                if acquired:
                    break
                if is_paused and checkpoint is not None:
                    if checkpoint() is False:
                        raise CancelledError("Queued work stopped while paused.")
                    continue
                with self._condition:
                    self._condition.wait(self.poll_seconds)
            yield self.snapshot(token)
        finally:
            with self._condition:
                if ticket in self._pending:
                    self._pending.remove(ticket)
                if self._active.get(token) is ticket:
                    self._active.pop(token)
                if self._registered.get(token) is ticket:
                    self._registered.pop(token)
                self._condition.notify_all()

    def shutdown(self):
        with self._condition:
            self._closed = True
            self._condition.notify_all()

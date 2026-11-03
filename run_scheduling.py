"""Validated start-to-start pacing and interruptible controls for sequential jobs."""
from __future__ import annotations

import math
import random
import threading
import time


MAX_INTERVAL_SECONDS = 86_400


class ScheduleError(ValueError):
    """A scheduling error that can be shown directly in the interface."""


def _seconds(value, label="Khoảng cách giữa các lượt"):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= MAX_INTERVAL_SECONDS:
        raise ScheduleError(label + " phải là số từ 0 đến 86.400 giây.")
    return float(value)


def parse_schedule(payload=None, legacy_delay=2):
    """Normalize a fixed/random interval without accepting booleans or NaN.

    Missing schedules preserve the legacy numeric ``delay`` input. Intervals are
    measured between actual starts, rather than adding another delay after the
    preceding form has finished. A fixed schedule uses ``min_seconds`` only.
    """
    if payload is None:
        interval = _seconds(legacy_delay)
        return {"mode": "fixed", "min_seconds": interval, "max_seconds": interval}
    if not isinstance(payload, dict):
        raise ScheduleError("Cấu hình tốc độ gửi phải là một đối tượng hợp lệ.")
    mode = payload.get("mode", "fixed")
    if mode not in ("fixed", "random"):
        raise ScheduleError("Chọn tốc độ cố định hoặc ngẫu nhiên trong một khoảng.")
    minimum = _seconds(payload.get("min_seconds", legacy_delay), "Khoảng cách nhỏ nhất")
    maximum = _seconds(payload.get("max_seconds", minimum), "Khoảng cách lớn nhất")
    if mode == "fixed":
        maximum = minimum
    elif maximum < minimum:
        raise ScheduleError("Khoảng cách lớn nhất phải lớn hơn hoặc bằng khoảng cách nhỏ nhất.")
    return {"mode": mode, "min_seconds": minimum, "max_seconds": maximum}


def sample_interval(schedule, rng=None):
    """Choose one interval; callers record a new actual start after each wait."""
    schedule = parse_schedule(schedule)
    if schedule["mode"] == "fixed":
        return schedule["min_seconds"]
    return (rng or random).uniform(schedule["min_seconds"], schedule["max_seconds"])


def estimate_duration(count, schedule, seconds_per_response=0):
    """Estimate a sequential batch with an explicit per-response duration.

    A count of zero means an unlimited job and has no completion estimate. With
    no duration measurement, the result is only the spacing component of ETA.
    Slow forms never overlap: every next start needs both the interval and the
    preceding response to have completed.
    """
    if type(count) is not int or count < 0:
        raise ScheduleError("Số lượt phải là số nguyên từ 0 trở lên.")
    duration = _seconds(seconds_per_response, "Thời gian dự kiến điền một form")
    schedule = parse_schedule(schedule)
    if count == 0:
        return {"minimum_seconds": None, "maximum_seconds": None}
    return {"minimum_seconds": duration + (count - 1) * max(duration, schedule["min_seconds"]),
            "maximum_seconds": duration + (count - 1) * max(duration, schedule["max_seconds"])}


class RunControl:
    """Pause at checkpoints, wake on stop, and freeze an active cooldown.

    Browser work is not interrupted midway through an I/O call. The worker must
    call :meth:`checkpoint` between fields and immediately before submission.
    Access checks and state callbacks always run outside the condition lock.
    """

    def __init__(self, stop_event=None, *, clock=time.monotonic, wall_clock=time.time, poll_seconds=1):
        if type(poll_seconds) not in (int, float) or not math.isfinite(poll_seconds) or not 0 < poll_seconds <= 2:
            raise ScheduleError("Chu kỳ kiểm tra điều khiển phải lớn hơn 0 và tối đa 2 giây.")
        self.stop_event = threading.Event() if stop_event is None else stop_event
        self._clock = clock
        self._wall_clock = wall_clock
        self._poll_seconds = float(poll_seconds)
        self._condition = threading.Condition()
        self._paused = False

    @property
    def paused(self):
        with self._condition:
            return self._paused

    @property
    def stopped(self):
        return self.stop_event.is_set()

    def pause(self):
        with self._condition:
            if self.stop_event.is_set():
                return False
            changed = not self._paused
            self._paused = True
            self._condition.notify_all()
            return changed

    def resume(self):
        with self._condition:
            changed = self._paused
            self._paused = False
            self._condition.notify_all()
            return changed

    def stop(self):
        with self._condition:
            self.stop_event.set()
            self._condition.notify_all()

    def checkpoint(self, *, access_check=None, on_state=None):
        return self.wait_until(None, access_check=access_check, on_state=on_state)

    def await_next(self, previous_started, interval, *, access_check=None, on_state=None):
        interval = _seconds(interval)
        if previous_started is None:
            return self.checkpoint(access_check=access_check, on_state=on_state)
        if type(previous_started) not in (int, float) or not math.isfinite(previous_started):
            raise ScheduleError("Mốc thời gian bắt đầu lượt trước không hợp lệ.")
        return self.wait_until(previous_started + interval, access_check=access_check, on_state=on_state)

    def wait_until(self, deadline, *, access_check=None, on_state=None):
        """Wait for a monotonic deadline, extending it by pauses during this wait.

        Returns ``False`` for a stop request or revoked access. A supplied stop
        event can also be set directly; polling observes that within at most two
        seconds. Calling :meth:`stop` wakes the condition immediately.
        """
        if deadline is not None and (type(deadline) not in (int, float) or not math.isfinite(deadline)):
            raise ScheduleError("Mốc chờ lượt tiếp theo không hợp lệ.")
        pause_began = None
        previous_state = None

        def emit(paused=False, waiting=False, remaining=0):
            nonlocal previous_state
            state = {"paused": paused, "waiting": waiting,
                     "next_run_at": self._wall_clock() + remaining if waiting else None,
                     "remaining_seconds": max(0.0, remaining)}
            # Checkpoints do not need to emit duplicate clear states repeatedly
            # within one wait, but countdown progress remains visible each poll.
            if on_state is not None and state != previous_state:
                previous_state = state
                on_state(state)

        while True:
            if self.stop_event.is_set():
                emit()
                return False
            if access_check is not None and not access_check():
                self.stop()
                emit()
                return False
            with self._condition:
                if self.stop_event.is_set():
                    should_stop, paused, remaining = True, False, 0.0
                else:
                    should_stop, paused = False, self._paused
                    now = self._clock()
                    if paused and pause_began is None:
                        pause_began = now
                    elif not paused and pause_began is not None:
                        if deadline is not None:
                            deadline += max(0, now - pause_began)
                        pause_began = None
                    reference = pause_began if paused else now
                    remaining = max(0.0, deadline - reference) if deadline is not None else 0.0
            if should_stop:
                emit()
                return False
            emit(paused=paused, waiting=not paused and remaining > 0, remaining=remaining)
            if not paused and remaining <= 0:
                return True
            with self._condition:
                # Do not lose a resume/stop performed by a callback while the
                # condition lock was deliberately released.
                if self.stop_event.is_set() or self._paused != paused:
                    continue
                timeout = self._poll_seconds
                if not paused and deadline is not None:
                    timeout = min(timeout, max(0, deadline - self._clock()))
                if timeout > 0:
                    self._condition.wait(timeout)

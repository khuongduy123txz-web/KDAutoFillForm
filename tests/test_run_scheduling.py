import math
import random
import threading
import time
import unittest

from run_scheduling import RunControl, ScheduleError, estimate_duration, parse_schedule, sample_interval


class ScheduleTests(unittest.TestCase):
    def test_legacy_delay_and_fixed_schedule_are_normalized(self):
        self.assertEqual(parse_schedule(legacy_delay=2), {"mode": "fixed", "min_seconds": 2.0, "max_seconds": 2.0})
        self.assertEqual(parse_schedule({"mode": "fixed", "min_seconds": 30, "max_seconds": 180})["max_seconds"], 30)
        self.assertEqual(sample_interval({"mode": "fixed", "min_seconds": 0}), 0)

    def test_random_range_is_reproducible_and_stays_between_30_and_180(self):
        schedule = parse_schedule({"mode": "random", "min_seconds": 30, "max_seconds": 180})
        first = [sample_interval(schedule, random.Random(seed)) for seed in range(100)]
        second = [sample_interval(schedule, random.Random(seed)) for seed in range(100)]
        self.assertEqual(first, second)
        self.assertTrue(all(30 <= value <= 180 for value in first))
        self.assertGreater(len(set(first)), 90)

    def test_bad_ranges_and_non_finite_or_boolean_values_are_rejected(self):
        for value in (-1, 86401, float("inf"), float("nan"), True, "30"):
            with self.subTest(value=value), self.assertRaises(ScheduleError):
                parse_schedule({"mode": "random", "min_seconds": value, "max_seconds": 180})
        for payload in ([], "random", {"mode": "fast"}, {"mode": "random", "min_seconds": 180, "max_seconds": 30}):
            with self.subTest(payload=payload), self.assertRaises(ScheduleError):
                parse_schedule(payload)

    def test_estimate_accounts_for_start_intervals_slow_forms_and_unlimited_jobs(self):
        schedule = {"mode": "random", "min_seconds": 30, "max_seconds": 180}
        self.assertEqual(estimate_duration(3, schedule, 10), {"minimum_seconds": 70, "maximum_seconds": 370})
        self.assertEqual(estimate_duration(3, schedule, 200), {"minimum_seconds": 600, "maximum_seconds": 600})
        self.assertEqual(estimate_duration(1, schedule, 10), {"minimum_seconds": 10, "maximum_seconds": 10})
        self.assertEqual(estimate_duration(0, schedule), {"minimum_seconds": None, "maximum_seconds": None})


class RunControlTests(unittest.TestCase):
    def run_wait(self, control, deadline, **kwargs):
        outcome = []
        completed = threading.Event()

        def target():
            outcome.append(control.wait_until(deadline, **kwargs))
            completed.set()
        worker = threading.Thread(target=target, daemon=True)
        worker.start()
        self.addCleanup(control.stop)
        self.addCleanup(lambda: worker.join(timeout=1))
        return outcome, completed, worker

    def test_long_interval_stop_wakes_immediately_without_waiting_30_seconds(self):
        control = RunControl(poll_seconds=2)
        entered = threading.Event()
        outcome, completed, _ = self.run_wait(control, time.monotonic() + 180,
                                             on_state=lambda state: entered.set() if state["waiting"] else None)
        self.assertTrue(entered.wait(1))
        started = time.monotonic()
        control.stop()
        self.assertTrue(completed.wait(.5))
        self.assertLess(time.monotonic() - started, .5)
        self.assertEqual(outcome, [False])

    def test_checkpoint_blocks_while_paused_and_resumes_without_deadlocking_callback(self):
        control = RunControl(poll_seconds=.01)
        control.pause()
        observed = threading.Event()

        def state_changed(state):
            if state["paused"]:
                # Acquiring the public property confirms callback execution is
                # outside the condition lock (also safe with other app locks).
                self.assertTrue(control.paused)
                observed.set()
        outcome, completed, _ = self.run_wait(control, None, on_state=state_changed)
        self.assertTrue(observed.wait(1))
        self.assertFalse(completed.is_set())
        control.resume()
        self.assertTrue(completed.wait(1))
        self.assertEqual(outcome, [True])

    def test_pause_freezes_remaining_cooldown_and_updates_next_run_time(self):
        control = RunControl(poll_seconds=.005)
        waiting, paused = threading.Event(), threading.Event()
        remaining = []

        def state_changed(state):
            if state["waiting"]:
                waiting.set()
            if state["paused"]:
                remaining.append(state["remaining_seconds"])
                self.assertIsNone(state["next_run_at"])
                paused.set()
        outcome, completed, _ = self.run_wait(control, time.monotonic() + .12, on_state=state_changed)
        self.assertTrue(waiting.wait(1))
        control.pause()
        self.assertTrue(paused.wait(1))
        time.sleep(.15)
        self.assertFalse(completed.is_set())
        self.assertTrue(remaining and remaining[0] > .05)
        self.assertTrue(all(abs(value - remaining[0]) < .002 for value in remaining))
        control.resume()
        self.assertFalse(completed.wait(.02))
        self.assertTrue(completed.wait(1))
        self.assertEqual(outcome, [True])

    def test_access_revocation_during_a_long_wait_stops_within_poll_interval(self):
        control = RunControl(poll_seconds=.01)
        allowed = threading.Event()
        allowed.set()
        waiting = threading.Event()
        outcome, completed, _ = self.run_wait(control, time.monotonic() + 180,
                                             access_check=allowed.is_set,
                                             on_state=lambda state: waiting.set() if state["waiting"] else None)
        self.assertTrue(waiting.wait(1))
        allowed.clear()
        self.assertTrue(completed.wait(.5))
        self.assertEqual(outcome, [False])
        self.assertTrue(control.stopped)

    def test_overrun_does_not_add_an_extra_interval_or_create_catchup_starts(self):
        ticks = iter((200, 201, 202, 203))
        control = RunControl(clock=lambda: next(ticks), wall_clock=lambda: 1000)
        self.assertTrue(control.await_next(previous_started=100, interval=30))
        # The caller records each new actual start. After a slow first form,
        # the next interval starts at the new timestamp rather than a stale grid.
        states = []
        control = RunControl(clock=lambda: 200, wall_clock=lambda: 1000)
        self.assertTrue(control.wait_until(199, on_state=states.append))
        self.assertFalse(states[-1]["waiting"])
        self.assertEqual(states[-1]["remaining_seconds"], 0)

    def test_callback_can_resume_and_stop_control_without_losing_wakeup(self):
        control = RunControl(poll_seconds=2)
        control.pause()

        def callback(state):
            if state["paused"]:
                control.resume()
                control.stop()
        self.assertFalse(control.checkpoint(on_state=callback))

    def test_external_stop_event_is_respected(self):
        event = threading.Event()
        event.set()
        control = RunControl(event)
        self.assertFalse(control.await_next(None, 30))
        self.assertFalse(control.pause())


if __name__ == "__main__":
    unittest.main()

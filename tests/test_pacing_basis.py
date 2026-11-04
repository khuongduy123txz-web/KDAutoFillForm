"""Pacing contracts with measured processing times, independent of wall time."""
import math
import random
import unittest

from run_scheduling import (ScheduleError, estimate_duration,
                            next_start_deadline, parse_schedule, sample_interval)


class PacingBasisTests(unittest.TestCase):
    def test_legacy_dictionary_and_explicit_basis_round_trip(self):
        legacy = {"mode": "fixed", "min_seconds": 2.0, "max_seconds": 2.0}
        self.assertEqual(parse_schedule(), legacy)
        self.assertEqual(parse_schedule(legacy), legacy)
        for basis in ("start", "completion"):
            schedule = dict(legacy, basis=basis)
            self.assertEqual(parse_schedule(schedule), schedule)

    def test_start_interval_accounts_for_processing_without_adding_idle_time(self):
        schedule = {"mode": "fixed", "min_seconds": 2}
        # A quick form waits until the requested start interval; a slow form can
        # continue as soon as it completes, with no catch-up or extra idle time.
        self.assertEqual(next_start_deadline(schedule, started=100, completed=100.7, interval=2), 102)
        self.assertEqual(next_start_deadline(schedule, started=100, completed=108, interval=2), 108)
        self.assertEqual(next_start_deadline(schedule, started=108, completed=109, interval=2), 110)

    def test_completion_interval_is_a_full_cooldown_after_processing(self):
        schedule = {"mode": "fixed", "min_seconds": 60, "basis": "completion"}
        self.assertEqual(next_start_deadline(schedule, started=100, completed=108, interval=60), 168)
        self.assertEqual(next_start_deadline(schedule, started=100, completed=108, interval=0), 108)
        self.assertEqual(estimate_duration(3, schedule, 8),
                         {"minimum_seconds": 144, "maximum_seconds": 144})

    def test_random_interval_is_sampled_once_and_reused_for_deadline(self):
        schedule = {"mode": "random", "min_seconds": 30, "max_seconds": 180,
                    "basis": "completion"}
        rng = random.Random(123)
        sampled = sample_interval(schedule, rng)
        self.assertEqual(next_start_deadline(schedule, started=100, completed=108,
                                             interval=sampled), 108 + sampled)
        self.assertEqual(estimate_duration(3, schedule, 8),
                         {"minimum_seconds": 84, "maximum_seconds": 384})

    def test_bad_basis_and_timestamps_do_not_silently_change_pacing(self):
        for basis in (None, True, "finish", "", [], {}):
            with self.subTest(basis=basis), self.assertRaises(ScheduleError):
                parse_schedule({"basis": basis})
        schedule = {"mode": "fixed", "min_seconds": 2}
        for started, completed in ((True, 2), (0, False), (math.nan, 2),
                                   (0, math.inf), ("0", 2), (4, 3)):
            with self.subTest(started=started, completed=completed), self.assertRaises(ScheduleError):
                next_start_deadline(schedule, started=started, completed=completed, interval=2)
        for interval in (-1, True, math.inf, math.nan, "2"):
            with self.subTest(interval=interval), self.assertRaises(ScheduleError):
                next_start_deadline(schedule, started=0, completed=1, interval=interval)


if __name__ == "__main__":
    unittest.main()

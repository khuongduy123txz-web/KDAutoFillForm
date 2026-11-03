"""Ten simultaneous users and FIFO overflow; browser actions are simulated only."""
from collections import Counter
import threading
import unittest

import google_form_tool as tool
import test_multiuser as helpers


class TenUserCapacityTests(unittest.TestCase):
    def fixture(self):
        fixture = helpers.MultiUserTests("runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def test_ten_users_run_concurrently_and_eleventh_twelfth_wait_fifo(self):
        fixture = self.fixture()
        fixture.app.close()
        fixture.app = tool.WebApplication(key_store=fixture.store, browser_workers=10, scan_workers=2)
        owners = [fixture.owner("capacity-user-" + str(index + 1).zfill(2)) for index in range(12)]
        for owner in owners:
            fixture.gates[owner["name"]] = threading.Event()

        jobs = [fixture.start(owner) for owner in owners[:10]]
        fixture.wait(lambda: len(fixture.owners_started()) == 10)
        self.assertEqual(set(fixture.owners_started()), {owner["name"] for owner in owners[:10]})
        with fixture.guard:
            self.assertEqual((fixture.live, fixture.peak), (10, 10))

        jobs.extend(fixture.start(owner) for owner in owners[10:])
        for position, (owner, job) in enumerate(zip(owners[10:], jobs[10:]), 1):
            state = fixture.app.job_state(job["id"], compact=True)
            self.assertTrue(state["queue_waiting"])
            self.assertEqual(state["queue_position"], position)
            self.assertEqual(state["worker_capacity"], 10)
            self.assertEqual(fixture.used(owner), 0)
        self.assertEqual(len(fixture.owners_started()), 10)

        fixture.gates[owners[0]["name"]].set()
        fixture.wait(lambda: owners[10]["name"] in fixture.owners_started())
        fixture.finish(jobs[0])
        self.assertEqual(fixture.owners_started()[-1], owners[10]["name"])
        self.assertNotIn(owners[11]["name"], fixture.owners_started())
        self.assertEqual(fixture.app.job_state(jobs[11]["id"])["queue_position"], 1)
        self.assertEqual(fixture.used(owners[10]), 0)
        self.assertEqual(fixture.used(owners[11]), 0)
        with fixture.guard:
            self.assertEqual(fixture.live, 10)

        fixture.gates[owners[10]["name"]].set()
        fixture.wait(lambda: owners[11]["name"] in fixture.owners_started())
        fixture.finish(jobs[10])
        self.assertEqual(fixture.owners_started()[-2:], [owners[10]["name"], owners[11]["name"]])
        with fixture.guard:
            self.assertEqual(fixture.live, 10)
            self.assertEqual(fixture.peak, 10)
        self.assertEqual(fixture.used(owners[11]), 0)

        for gate in fixture.gates.values():
            gate.set()
        for job in jobs:
            fixture.finish(job)
        self.assertEqual(Counter(fixture.owners_started()), Counter(owner["name"] for owner in owners))
        self.assertEqual(Counter(fixture.submitted), Counter(owner["name"] for owner in owners))
        self.assertEqual([fixture.used(owner) for owner in owners], [1] * 12)
        fixture.wait(lambda: fixture.live == 0)
        self.assertEqual(fixture.peak, 10)

    def test_worker_configuration_accepts_ten_and_rejects_invalid_or_over_limit(self):
        fixture = self.fixture()
        app = tool.WebApplication(key_store=fixture.store, browser_workers=10, scan_workers=2)
        self.addCleanup(app.close)
        self.assertEqual(app.browser_queue.capacity, 10)
        for invalid in (0, 11, True, False, 1.5, "1.5"):
            with self.subTest(value=invalid), self.assertRaises(tool.FormError):
                unexpected = tool.WebApplication(key_store=fixture.store, browser_workers=invalid, scan_workers=2)
                try:
                    self.fail("Invalid browser worker configuration was accepted")
                finally:
                    unexpected.close()


if __name__ == "__main__":
    unittest.main()

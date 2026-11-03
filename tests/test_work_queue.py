import threading
import time
import unittest
from concurrent.futures import CancelledError

from work_queue import FairWorkQueue, TaskDispatcher


class DispatcherTests(unittest.TestCase):
    def dispatcher(self, capacity=1, **kwargs):
        queue = TaskDispatcher(capacity, poll_seconds=.01, **kwargs)
        self.addCleanup(queue.shutdown)
        return queue

    def wait(self, predicate, timeout=2):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return
            time.sleep(.002)
        self.fail("Expected dispatcher state was not reached")

    def test_capacity_is_bounded_and_many_visitors_add_no_worker_threads(self):
        queue = self.dispatcher(2, name="qa-fixed")
        entered = threading.Barrier(3)
        release = threading.Event()
        self.addCleanup(release.set)
        lock = threading.Lock()
        running = peak = 0
        def work():
            nonlocal running, peak
            with lock:
                running += 1
                peak = max(peak, running)
            if not release.is_set():
                entered.wait(timeout=2)
                release.wait(2)
            with lock:
                running -= 1
            return threading.current_thread().name
        first = [queue.submit(str(i), work) for i in range(2)]
        entered.wait(timeout=2)
        futures = first + [queue.submit(str(i), work) for i in range(2, 202)]
        state = queue.snapshot("201")
        self.assertEqual(state["running"], 2)
        self.assertEqual(state["waiting"], 200)
        self.assertEqual(state["queue_position"], 200)
        self.assertEqual(len(queue._threads), 2)
        self.assertTrue(all(thread.daemon for thread in queue._threads))
        release.set()
        names = {future.result(timeout=3) for future in futures}
        self.assertLessEqual(len(names), 2)
        self.assertEqual(peak, 2)

    def test_one_response_continuation_yields_fifo_to_other_visitors(self):
        queue = self.dispatcher()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        seen, continuations = [], []
        def first():
            seen.append("A1")
            entered.set()
            release.wait(2)
            continuations.append(queue.submit("A", lambda: seen.append("A2")))
        initial = queue.submit("A", first)
        self.assertTrue(entered.wait(1))
        other = queue.submit("B", lambda: seen.append("B1"))
        release.set()
        initial.result(timeout=1)
        other.result(timeout=1)
        continuations[0].result(timeout=1)
        self.assertEqual(seen, ["A1", "B1", "A2"])

    def test_future_deadline_does_not_block_immediate_work(self):
        queue = self.dispatcher()
        seen = []
        began = time.monotonic()
        delayed = queue.submit("later", lambda: seen.append(("later",time.monotonic())), delay=.1)
        self.assertIsNone(queue.snapshot("later")["queue_position"])
        immediate = queue.submit("now", lambda: seen.append(("now",time.monotonic())))
        immediate.result(timeout=1)
        self.assertEqual(seen[0][0], "now")
        self.assertFalse(delayed.done())
        delayed.result(timeout=1)
        self.assertGreaterEqual(seen[1][1] - began, .08)

    def test_paused_ready_predicate_runs_outside_lock_and_skips_head(self):
        queue = self.dispatcher()
        resumed, checked = threading.Event(), threading.Event()
        seen = []
        def ready():
            outside = threading.Event()
            probe = threading.Thread(target=lambda: (queue.snapshot(),outside.set()))
            probe.start()
            self.assertTrue(outside.wait(.5), "Readiness hook held the dispatcher lock")
            probe.join(timeout=1)
            checked.set()
            return resumed.is_set()
        paused = queue.submit("paused", lambda: seen.append("resumed"), ready=ready)
        self.assertTrue(checked.wait(1))
        other = queue.submit("other", lambda: seen.append("other"))
        other.result(timeout=1)
        self.assertEqual(seen, ["other"])
        self.assertFalse(paused.done())
        self.assertIsNone(queue.snapshot("paused")["queue_position"])
        resumed.set()
        queue.wake()
        paused.result(timeout=1)
        self.assertEqual(seen, ["other","resumed"])

    def test_cancelled_pending_and_active_tokens_do_not_leak_or_requeue(self):
        queue = self.dispatcher()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        blocked = []
        def running():
            entered.set()
            release.wait(2)
            try:
                queue.submit("active", lambda: blocked.append("unexpected"))
            except CancelledError:
                blocked.append("cancelled")
        active = queue.submit("active", running)
        self.assertTrue(entered.wait(1))
        pending = queue.submit("pending", lambda: blocked.append("pending"))
        self.assertTrue(queue.cancel("pending"))
        self.assertTrue(pending.cancelled())
        self.assertTrue(queue.cancel("active"))
        self.assertFalse(queue.cancel("missing"))
        release.set()
        active.result(timeout=1)
        self.wait(lambda: queue.snapshot()["running"] == 0)
        self.assertEqual(blocked, ["cancelled"])
        self.assertEqual(queue._cancelled, set())
        self.assertEqual(queue.snapshot()["waiting"], 0)
        self.assertEqual(queue.submit("active", lambda: "new").result(timeout=1), "new")

    def test_future_cancel_removes_delayed_ticket_without_cancelling_new_generation(self):
        queue = self.dispatcher()
        delayed = queue.submit("same",lambda: None,delay=3600)
        self.assertTrue(delayed.cancel())
        self.assertEqual(queue.snapshot()["waiting"],0)
        self.assertEqual(queue.submit("same",lambda: "current").result(timeout=1),"current")

    def test_readiness_and_callback_errors_do_not_reduce_worker_capacity(self):
        queue = self.dispatcher()
        def bad_ready():
            raise LookupError("readiness failed")
        first = queue.submit("bad-ready", lambda: None, ready=bad_ready)
        with self.assertRaises(LookupError):
            first.result(timeout=1)
        def bad_callback():
            raise RuntimeError("callback failed")
        second = queue.submit("bad-callback", bad_callback)
        with self.assertRaises(RuntimeError):
            second.result(timeout=1)
        self.assertEqual(queue.submit("valid", lambda: 123).result(timeout=1), 123)
        self.assertTrue(queue._threads[0].is_alive())

    def test_reschedule_freezes_cooldown_without_losing_fifo_ticket(self):
        queue = self.dispatcher()
        resumed = threading.Event()
        seen = []
        first = queue.submit("first", lambda: seen.append("first"), delay=.05, ready=resumed.is_set)
        second = queue.submit("second", lambda: seen.append("second"), delay=.05, ready=resumed.is_set)
        time.sleep(.08)
        self.assertFalse(first.done())
        future_due = time.monotonic() + .08
        self.assertTrue(queue.reschedule("first", due_monotonic=future_due))
        self.assertTrue(queue.reschedule("second", due_monotonic=future_due))
        self.assertFalse(queue.reschedule("absent", due_monotonic=future_due))
        resumed.set()
        queue.wake()
        self.assertFalse(first.done())
        first.result(timeout=1)
        second.result(timeout=1)
        self.assertEqual(seen, ["first","second"])

    def test_duplicate_pending_rejected_and_same_token_never_overlaps(self):
        queue = self.dispatcher(2)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        first = queue.submit("same", lambda: (entered.set(),release.wait(2)))
        self.assertTrue(entered.wait(1))
        second = queue.submit("same", lambda: "second")
        with self.assertRaises(ValueError):
            queue.submit("same", lambda: "duplicate")
        time.sleep(.03)
        self.assertFalse(second.done())
        self.assertFalse(queue.reschedule("same",due_monotonic=time.monotonic()))
        release.set()
        first.result(timeout=1)
        self.assertEqual(second.result(timeout=1), "second")

    def test_shutdown_cancels_waiters_and_runs_cleanup_on_each_owned_thread(self):
        names, lock = [], threading.Lock()
        def cleanup():
            with lock:
                names.append(threading.current_thread().name)
        queue = self.dispatcher(3,on_worker_exit=cleanup)
        pending = queue.submit("later",lambda: None,delay=3600)
        queue.shutdown()
        self.assertTrue(pending.cancelled())
        self.assertEqual(len(names),3)
        self.assertEqual(set(names),{thread.name for thread in queue._threads})
        self.assertTrue(all(not thread.is_alive() for thread in queue._threads))
        with self.assertRaises(RuntimeError):
            queue.submit("closed",lambda: None)

    def test_invalid_configuration_and_delays_are_rejected(self):
        for bad in (0,-1,True,1.5):
            with self.subTest(capacity=bad), self.assertRaises(ValueError):
                TaskDispatcher(bad)
        queue = self.dispatcher()
        for value in (-1,True,float("nan"),float("inf"),"1"):
            with self.subTest(delay=value), self.assertRaises(ValueError):
                queue.submit("invalid",lambda: None,delay=value)
        with self.assertRaises(ValueError):
            queue.submit("invalid",lambda: None,delay=1,due_monotonic=time.monotonic())


class FairSlotTests(unittest.TestCase):
    def test_exception_releases_resource_and_cancelled_waiter_does_not_block_tail(self):
        queue = FairWorkQueue(1,poll_seconds=.01)
        entered, first_release, waiting = threading.Event(),threading.Event(),threading.Event()
        self.addCleanup(first_release.set)
        seen, errors = [], []
        def first():
            try:
                with queue.acquire("first"):
                    entered.set()
                    first_release.wait(2)
                    raise RuntimeError("expected")
            except RuntimeError:
                pass
        def second():
            try:
                with queue.acquire("second",on_state=lambda state: waiting.set()):
                    seen.append("second")
            except CancelledError:
                errors.append("cancelled")
        def third():
            with queue.acquire("third"):
                seen.append("third")
        a=threading.Thread(target=first)
        b=threading.Thread(target=second)
        c=threading.Thread(target=third)
        for thread in (a,b,c):
            self.addCleanup(lambda thread=thread: thread.join(timeout=2))
        a.start()
        self.assertTrue(entered.wait(1))
        b.start()
        self.assertTrue(waiting.wait(1))
        c.start()
        self.assertTrue(queue.cancel("second"))
        first_release.set()
        for thread in (a,b,c):
            thread.join(timeout=1)
            self.assertFalse(thread.is_alive())
        self.assertEqual(seen,["third"])
        self.assertEqual(errors,["cancelled"])
        self.assertEqual(queue.snapshot()["running"],0)

    def test_paused_waiter_leaves_queue_and_rejoins_after_resume(self):
        queue = FairWorkQueue(1,poll_seconds=.01)
        paused, entered = threading.Event(),threading.Event()
        paused.set()
        seen=[]
        def paused_work():
            with queue.acquire("paused",paused=paused.is_set,on_state=lambda state:entered.set()):
                seen.append("paused")
        thread=threading.Thread(target=paused_work)
        thread.start()
        self.addCleanup(lambda:thread.join(timeout=1))
        self.addCleanup(paused.clear)
        self.assertTrue(entered.wait(1))
        with queue.acquire("other"):
            seen.append("other")
        paused.clear()
        queue.notify()
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(seen,["other","paused"])


if __name__ == "__main__":
    unittest.main()

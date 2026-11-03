"""Opt-in real Chromium audit for ten workers; every form request is routed locally.

Run with KD_TEN_BROWSER_TESTS=1 on a host/container with sufficient memory.
This intentionally stays separate from the normal 512 MB regression suite.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import google_form_tool as tool
from test_google_speed import four_page_data, four_page_html


@unittest.skipUnless(os.environ.get("KD_TEN_BROWSER_TESTS") == "1",
                     "Set KD_TEN_BROWSER_TESTS=1 on a sufficiently provisioned host")
class TenUserRealBrowserTests(unittest.TestCase):
    def test_ten_real_chromium_workers_complete_four_pages_and_overflow_fifo(self):
        began = time.monotonic()
        names = ["user" + str(index + 1).zfill(2) for index in range(12)]
        urls = {name: "https://docs.google.com/forms/d/e/qa-ten-" + name + "/viewform" for name in names}
        data = {name: four_page_data() for name in names}
        for name in names:
            data[name][1][8] = name
        schemas = {name: tool.parse_schema(data[name], urls[name]) for name in names}
        documents = {urls[name]: four_page_html(data[name]) for name in names}
        by_url = {url: name for name, url in urls.items()}
        gates = {name: threading.Event() for name in names}
        guard = threading.Lock()
        ready, posts, answers_by_name, reports = [], [], {}, {}
        launches, closes, contexts = [], [], []
        monitor_stop = threading.Event()
        memory = {"baseline_bytes": None, "peak_bytes": None}
        memory_path = Path("/sys/fs/cgroup/memory.current")

        def measure():
            if not memory_path.is_file():
                return
            try:
                value = int(memory_path.read_text())
            except (OSError, ValueError):
                return
            if memory["baseline_bytes"] is None:
                memory["baseline_bytes"] = value
            memory["peak_bytes"] = max(value, memory["peak_bytes"] or 0)

        def monitor():
            while not monitor_stop.wait(.05):
                measure()
        measure()
        monitoring = threading.Thread(target=monitor, daemon=True)
        monitoring.start()
        original = tool.FormBrowser
        test = self

        class RoutedBrowser(original):
            def __enter__(self):
                result = super().__enter__()
                with guard:
                    launches.append(threading.get_ident())
                return result

            def __exit__(self, *args):
                try:
                    return super().__exit__(*args)
                finally:
                    with guard:
                        closes.append(threading.get_ident())

            def new_session(self):
                super().new_session()
                test.assertEqual(self.session.cookies(), [])
                with guard:
                    contexts.append(self.session)
                def route(r):
                    request = r.request
                    if request.method == "POST":
                        view_url = request.url.replace("/formResponse", "/viewform")
                        if view_url not in by_url:
                            r.abort()
                            raise AssertionError("Unexpected form POST destination")
                        with guard:
                            posts.append((by_url[view_url], json.loads(request.post_data)))
                        r.fulfill(status=200, content_type="text/plain", body="ok")
                    elif request.method == "GET" and request.url in documents:
                        r.fulfill(status=200, content_type="text/html; charset=utf-8", body=documents[request.url])
                    else:
                        r.abort()
                self.session.route("**/*", route)

            def _prepare(self, schema, answers):
                name = schema["title"]
                report = super()._prepare(schema, answers)
                test.assertEqual(self.page.evaluate("window.transitions"), 3)
                self.session.add_cookies([{"name": "qa_owner", "value": name, "url": schema["url"]}])
                with guard:
                    answers_by_name[name] = copy.deepcopy(answers)
                    reports[name] = copy.deepcopy(report)
                    ready.append(name)
                if not gates[name].wait(45):
                    raise AssertionError("Ten-browser fixture gate was not released: " + name)
                return report

        def wait(predicate, timeout=45):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if predicate():
                    return
                time.sleep(.01)
            test.fail("Expected ten-browser state was not reached")

        try:
            with tempfile.TemporaryDirectory(prefix="kd-ten-real-") as folder:
                store = tool.KeyStore(Path(folder) / "keys.sqlite3", export_dir=Path(folder) / "vault")
                guests = {name: hashlib.sha256(("qa-ten-" + name).encode()).hexdigest() for name in names}
                for guest in guests.values():
                    store.ensure_guest(guest)
                with patch.object(tool, "FormBrowser", RoutedBrowser):
                    app = tool.WebApplication(key_store=store, browser_workers=10, scan_workers=2)
                    jobs = {}
                    try:
                        for name in names:
                            schema = schemas[name]
                            scan_id = "scan-" + name
                            config = tool.default_web_config(schema, 42)
                            for field in schema["fields"]:
                                config[field["id"]] = ({"rates": {"Yes": 100, "No": 0}} if field["options"]
                                                       else {"mode": "fixed", "value": "Synthetic owner " + name})
                            app.scans[scan_id] = {"id": scan_id, "status": "ready", "schema": schema,
                                                  "config": config, "owner_id": guests[name]}
                        def start(name):
                            scan_id = "scan-" + name
                            result = app.start_job({"scan_id": scan_id, "count": 1,
                                "config": copy.deepcopy(app.scans[scan_id]["config"]), "delay": 0,
                                "engine": "fast", "seed": 42, "reference_date": "2026-11-03",
                                "_owner_id": guests[name], "_guest_id": guests[name], "headless": True})
                            jobs[name] = app.jobs[result["job_id"]]
                        for name in names[:10]:
                            start(name)
                        wait(lambda: len(ready) == 10)
                        self.assertEqual(set(ready), set(names[:10]))
                        self.assertEqual(len(launches), 10)
                        self.assertEqual(len(contexts), 10)
                        self.assertEqual(posts, [])
                        for name in names[10:]:
                            start(name)
                        for position, name in enumerate(names[10:], 1):
                            state = app.job_state(jobs[name]["id"], compact=True)
                            self.assertEqual(state["queue_position"], position)
                            self.assertTrue(state["queue_waiting"])
                            self.assertEqual(state["worker_capacity"], 10)
                            self.assertEqual(store.guest_usage(guests[name], fresh=True)["used"], 0)
                        measure()
                        gates[names[0]].set()
                        wait(lambda: names[10] in ready)
                        self.assertNotIn(names[11], ready)
                        self.assertEqual(ready[-1], names[10])
                        self.assertEqual(app.job_state(jobs[names[11]]["id"])["queue_position"], 1)
                        self.assertEqual(len(launches), 10)
                        gates[names[10]].set()
                        wait(lambda: names[11] in ready)
                        self.assertEqual(ready[-2:], names[10:])
                        self.assertEqual(len(launches), 10)
                        for gate in gates.values():
                            gate.set()
                        wait(lambda: all(job["status"] not in ("queued", "running", "stopping") for job in jobs.values()))
                        for name, job in jobs.items():
                            self.assertEqual(job["status"], "completed", job.get("error"))
                            self.assertEqual(job["confirmed"], 1)
                            self.assertEqual(store.guest_usage(guests[name], fresh=True)["used"], 1)
                            self.assertEqual(reports[name], {"pages": 4, "page_indices": [0,1,2,3],
                                                            "questions": 40, "fields": 40})
                            self.assertEqual(job["last_performance"]["batch_fields"], 40)
                            self.assertEqual(job["last_performance"]["fallback_fields"], 0)
                        self.assertEqual(len(posts), 12)
                        for name, payload in posts:
                            self.assertEqual(payload, answers_by_name[name])
                            self.assertEqual(len(payload), 40)
                        self.assertEqual(len(contexts), 12)
                        self.assertEqual(len({id(context) for context in contexts}), 12)
                        self.assertEqual(len(launches), 10)
                    finally:
                        for gate in gates.values():
                            gate.set()
                        app.close()
                    self.assertCountEqual(closes, launches)
                    self.assertFalse(any(thread.is_alive() for thread in app.browser_queue._threads))
                    self.assertFalse(any(thread.is_alive() for thread in app.scan_queue._threads))
            measure()
            print("REAL_TEN_BROWSER_AUDIT " + json.dumps({
                "concurrent_workers": 10, "owners": 12, "overflow_order": names[10:],
                "pages_per_response": 4, "fields_per_response": 40, "mocked_posts": len(posts),
                "launchers": len(launches), "contexts": len(contexts),
                "baseline_mib": round(memory["baseline_bytes"] / 1048576, 1) if memory["baseline_bytes"] else None,
                "peak_mib": round(memory["peak_bytes"] / 1048576, 1) if memory["peak_bytes"] else None,
                "elapsed_seconds": round(time.monotonic() - began, 3),
                "external_requests": 0
            }), flush=True)
        finally:
            monitor_stop.set()
            monitoring.join(timeout=1)


if __name__ == "__main__":
    unittest.main()

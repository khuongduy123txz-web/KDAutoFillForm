"""Exercise the real pooled Chromium worker; all Google traffic stays in routes."""
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


@unittest.skipUnless(os.environ.get("KD_BROWSER_TESTS") == "1", "Set KD_BROWSER_TESTS=1")
class MultiUserBrowserTests(unittest.TestCase):
    def test_real_chromium_is_reused_while_fair_jobs_get_fresh_respondent_contexts(self):
        with tempfile.TemporaryDirectory(prefix="kd-multiuser-browser-") as folder:
            store = tool.KeyStore(Path(folder)/"keys.sqlite3",export_dir=Path(folder)/"vault")
            guests = {name:hashlib.sha256(("browser-"+name).encode()).hexdigest() for name in ("A","B")}
            for guest in guests.values():
                store.ensure_guest(guest)
            urls = {name:"https://docs.google.com/forms/d/e/qa-multiuser-"+name+"/viewform" for name in guests}
            data = {name:four_page_data() for name in guests}
            for name in data:
                data[name][1][8] = name
            schemas = {urls[name]:tool.parse_schema(data[name],urls[name]) for name in guests}
            documents = {urls[name]:four_page_html(data[name]) for name in guests}
            entered, release = threading.Event(),threading.Event()
            lock = threading.Lock()
            starts, posts, reports, contexts, context_closes, launches, launcher_closes = [],[],[],[],[],[],[]
            original_browser = tool.FormBrowser
            test = self

            class RoutedBrowser(original_browser):
                def __enter__(self):
                    result = super().__enter__()
                    with lock:
                        launches.append((self.browser,threading.get_ident()))
                    return result

                def __exit__(self,*args):
                    try:
                        return super().__exit__(*args)
                    finally:
                        with lock:
                            launcher_closes.append(threading.get_ident())

                def new_session(self):
                    super().new_session()
                    # Cookies from a previous owner/response must never carry over.
                    test.assertEqual(self.session.cookies(),[])
                    with lock:
                        contexts.append(self.session)
                    self.session.on("close",lambda:context_closes.append(threading.get_ident()))
                    def route(r):
                        request = r.request
                        if request.method == "POST":
                            view_url = request.url.replace("/formResponse","/viewform")
                            if view_url not in schemas:
                                r.abort()
                                raise AssertionError("Unexpected POST destination")
                            with lock:
                                posts.append((schemas[view_url]["title"],json.loads(request.post_data)))
                            r.fulfill(status=200,content_type="text/plain",body="ok")
                        elif request.method=="GET" and request.url in documents:
                            r.fulfill(status=200,content_type="text/html; charset=utf-8",body=documents[request.url])
                        else:
                            # No external request can escape the fixture.
                            r.abort()
                    self.session.route("**/*",route)

                def _prepare(self,schema,answers):
                    with lock:
                        occurrence = sum(row[0]==schema["title"] for row in starts)
                        starts.append((schema["title"],copy.deepcopy(answers),threading.get_ident()))
                    if schema["title"]=="A" and occurrence==0:
                        entered.set()
                        if not release.wait(5):
                            raise AssertionError("First-owner fixture gate was not released")
                    report = super()._prepare(schema,answers)
                    test.assertEqual(self.page.evaluate("window.transitions"),3)
                    self.session.add_cookies([{"name":"fixture_owner","value":schema["title"],"url":schema["url"]}])
                    with lock:
                        reports.append(copy.deepcopy(report))
                    return report

            def wait(predicate,timeout=15):
                deadline = time.monotonic()+timeout
                while time.monotonic()<deadline:
                    if predicate():
                        return
                    time.sleep(.005)
                test.fail("Real browser worker did not reach expected state")

            with patch.object(tool,"FormBrowser",RoutedBrowser):
                app = tool.WebApplication(key_store=store,browser_workers=1,scan_workers=2)
                try:
                    with patch.object(app,"read_form_schema",side_effect=lambda url:copy.deepcopy(schemas[url])):
                        scan_a = app.scan(urls["A"],owner_id=guests["A"])["scan_id"]
                        wait(lambda:app.scan_state(scan_a)["status"]=="ready")
                        def start(name,scan_id,count):
                            config = copy.deepcopy(app.scans[scan_id]["config"])
                            for field in schemas[urls[name]]["fields"]:
                                if field["options"]:
                                    config[field["id"]] = {"rates":{"Yes":100,"No":0}}
                                else:
                                    config[field["id"]] = {"mode":"fixed","value":"Synthetic owner "+name}
                            result = app.start_job({"scan_id":scan_id,"count":count,"config":config,"delay":0,
                                "engine":"fast","seed":42,"reference_date":"2026-11-03",
                                "_owner_id":guests[name],"_guest_id":guests[name],"headless":True})
                            return app.jobs[result["job_id"]]
                        a = start("A",scan_a,2)
                        self.assertTrue(entered.wait(5),"A never entered the real browser")
                        # Metadata scans remain available while another user owns Chromium.
                        scan_b = app.scan(urls["B"],owner_id=guests["B"])["scan_id"]
                        wait(lambda:app.scan_state(scan_b)["status"]=="ready",timeout=2)
                        b = start("B",scan_b,1)
                        self.assertEqual(app.job_state(b["id"])["queue_position"],1)
                        self.assertEqual((a["confirmed"],b["confirmed"]),(0,0))
                        self.assertEqual(store.guest_usage(guests["A"],fresh=True)["used"],0)
                        self.assertEqual(store.guest_usage(guests["B"],fresh=True)["used"],0)
                        release.set()
                        wait(lambda:all(job["status"] not in ("queued","running","stopping") for job in (a,b)))
                    self.assertEqual(a["status"],"completed",a.get("error"))
                    self.assertEqual(b["status"],"completed",b.get("error"))
                    self.assertEqual([row[0] for row in starts],["A","B","A"])
                    self.assertEqual([row[0] for row in posts],["A","B","A"])
                    for began,posted in zip(starts,posts):
                        self.assertEqual(began[0],posted[0])
                        self.assertEqual(began[1],posted[1])
                        self.assertEqual(len(posted[1]),40)
                    self.assertEqual(len(reports),3)
                    for report in reports:
                        self.assertEqual(report,{"pages":4,"page_indices":[0,1,2,3],"questions":40,"fields":40})
                    self.assertEqual((a["confirmed"],b["confirmed"]),(2,1))
                    for job in (a,b):
                        self.assertEqual(job["last_performance"]["batch_fields"],40)
                        self.assertEqual(job["last_performance"]["fallback_fields"],0)
                    self.assertEqual(store.guest_usage(guests["A"],fresh=True)["used"],2)
                    self.assertEqual(store.guest_usage(guests["B"],fresh=True)["used"],1)
                    self.assertEqual(len(launches),1)
                    self.assertEqual(len(contexts),3)
                    self.assertEqual(len({id(context) for context in contexts}),3)
                    self.assertEqual(len(context_closes),3)
                    self.assertEqual({row[2] for row in starts},{launches[0][1]})
                    self.assertNotEqual(launches[0][1],threading.get_ident())
                    self.assertEqual(launcher_closes,[])
                finally:
                    release.set()
                    app.close()
                self.assertEqual(launcher_closes,[launches[0][1]])
                self.assertFalse(any(thread.is_alive() for thread in app.browser_queue._threads))
                self.assertFalse(any(thread.is_alive() for thread in app.scan_queue._threads))
                print("REAL MULTIUSER CHROMIUM PASS: A1/B1/A2; 4 pages x40 exact answers; one launcher; three isolated contexts; worker-owned teardown",flush=True)


if __name__=="__main__":
    unittest.main()

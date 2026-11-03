import gzip
import io
import json
from email.message import Message
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import form_scanning as scanning
import google_form_tool as tool
from test_google_regression import comprehensive_data


URL = "https://docs.google.com/forms/d/e/fixture-scan/viewform"


def html(data=None):
    data = comprehensive_data() if data is None else data
    return ('<!doctype html><html><body><form action="' + URL.replace("/viewform","/formResponse")
            + '"></form><script>var FB_PUBLIC_LOAD_DATA_ = '
            + json.dumps(data, ensure_ascii=False)
            + '; window.extra = "This JavaScript is never executed";</script></body></html>')


class Response:
    def __init__(self, source, url=None, headers=None):
        self.source = source.encode("utf-8") if isinstance(source,str) else source
        self.url = url
        self.headers = Message()
        for key,value in (headers or {"Content-Type":"text/html; charset=utf-8"}).items():
            self.headers[key] = value
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self,*args):
        self.closed = True

    def geturl(self):
        return self.url

    def read(self, size):
        return self.source[:size]


def redirect(url, location):
    headers = Message()
    if location is not None:
        headers["Location"] = location
    return HTTPError(url,302,"Moved",headers,io.BytesIO())


class GoogleMetadataScanTests(unittest.TestCase):
    def opener(self, outcomes):
        calls = []
        class Opener:
            def open(self, request, timeout):
                calls.append(request)
                outcome = outcomes.pop(0)
                if isinstance(outcome,BaseException):
                    raise outcome
                if outcome.url is None:
                    outcome.url = request.full_url
                return outcome
        patcher = patch.object(scanning,"build_opener",return_value=Opener())
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def test_normalizes_final_form_urls_and_keeps_short_links(self):
        for suffix in ("viewform","formResponse","edit"):
            self.assertEqual(scanning.normalize_google_url(URL.replace("viewform",suffix)+"?usp=sharing#part"),URL)
        for host in ("forms.gle","forms.google.com"):
            link = "https://"+host+"/fixture"
            self.assertEqual(scanning.normalize_google_url(link),link)

    def test_rejects_external_hosts_credentials_ports_and_non_form_paths(self):
        for url in ("http://docs.google.com/forms/d/e/x/viewform","https://docs.google.com.evil.example/forms/d/e/x/viewform",
                    "https://127.0.0.1/private","https://docs.google.com@evil.example/viewform",
                    "https://user:password@docs.google.com/forms/d/e/x/viewform",
                    "https://docs.google.com:443/forms/d/e/x/viewform","https://docs.google.com:8443/forms/d/e/x/viewform",
                    "https://docs.google.com/document/d/x/edit","https://forms.gle/","https://accounts.google.com/login"):
            with self.subTest(url=url), self.assertRaises(scanning.ScanError):
                scanning.normalize_google_url(url)

    def test_json_decoder_handles_unicode_and_trailing_javascript(self):
        data = comprehensive_data()
        for assignment in ("var FB_PUBLIC_LOAD_DATA_ = ","window.FB_PUBLIC_LOAD_DATA_=","\nconst FB_PUBLIC_LOAD_DATA_ = "):
            source = "<script>"+assignment+json.dumps(data,ensure_ascii=False)+";throw Error('never run')</script>"
            self.assertEqual(scanning.extract_google_data(source),data)
        self.assertEqual(scanning.extract_google_data("<script>window.x=1;\nFB_PUBLIC_LOAD_DATA_="+json.dumps(data)+";</script>"),data)

    def test_text_markers_and_malformed_json_are_not_executed_or_accepted(self):
        for source in ("<p>FB_PUBLIC_LOAD_DATA_="+json.dumps(comprehensive_data())+"</p>",
                       "<script>var FB_PUBLIC_LOAD_DATA_ = alert('never run');</script>",
                       "<script>var FB_PUBLIC_LOAD_DATA_ = [undefined];</script>",
                       "<script>var FB_PUBLIC_LOAD_DATA_ = {\"other\":[]};</script>",
                       "<script>var FB_PUBLIC_LOAD_DATA_ = [null];</script>"):
            with self.subTest(source=source[:60]), self.assertRaises(scanning.ScanError):
                scanning.extract_google_data(source)

    def test_get_scan_preserves_existing_schema_parser_all_pages_and_fields(self):
        response = Response(html())
        calls = self.opener([response])
        data, resolved = scanning.scan_google(URL)
        schema = tool.parse_schema(data,resolved)
        self.assertEqual(len(schema["pages"]),2)
        self.assertEqual(len(schema["fields"]),12)
        self.assertEqual(resolved,URL)
        self.assertEqual([request.method for request in calls],["GET"])
        self.assertIsNone(calls[0].data)
        self.assertTrue(response.closed)

    def test_shortlink_redirect_chain_is_validated_before_each_get(self):
        short = "https://forms.gle/fixture"
        intermediary = "https://forms.google.com/fixture"
        calls = self.opener([redirect(short,intermediary),redirect(intermediary,URL),Response(html())])
        data, resolved = scanning.scan_google(short)
        self.assertEqual(resolved,URL)
        self.assertEqual(data,comprehensive_data())
        self.assertEqual([request.full_url for request in calls],[short,intermediary,URL])
        self.assertTrue(all(request.method=="GET" for request in calls))

    def test_external_private_or_login_redirect_is_never_requested(self):
        for destination in ("https://evil.example/","http://127.0.0.1/private","https://accounts.google.com/signin"):
            with self.subTest(destination=destination):
                calls = self.opener([redirect(URL,destination)])
                with self.assertRaises(scanning.ScanError):
                    scanning.scan_google(URL)
                self.assertEqual(len(calls),1)

    def test_redirect_loop_limit_or_missing_location_stops(self):
        calls = self.opener([redirect(URL,URL)])
        with self.assertRaises(scanning.ScanError):
            scanning.scan_google(URL)
        self.assertEqual(len(calls),1)
        calls = self.opener([redirect(URL,None)])
        with self.assertRaises(scanning.ScanError):
            scanning.scan_google(URL)
        self.assertEqual(len(calls),1)

    def test_closed_form_without_response_form_is_explicit_error(self):
        source = html().replace('<form action="'+URL.replace("/viewform","/formResponse")+'"></form>',"")
        self.opener([Response(source)])
        with self.assertRaisesRegex(scanning.ScanError,"đóng|đăng nhập"):
            scanning.scan_google(URL)

    def test_response_size_and_compressed_size_are_bounded(self):
        for response in (Response(b"x"*129),
                         Response(b"x",headers={"Content-Length":"129"}),
                         Response(gzip.compress(b"x"*129),headers={"Content-Encoding":"gzip"})):
            with self.subTest(headers=str(response.headers)):
                self.opener([response])
                with self.assertRaises(scanning.ScanError):
                    scanning.scan_google(URL,max_bytes=128)

    def test_small_gzip_response_is_decoded_without_losing_metadata(self):
        self.opener([Response(gzip.compress(html().encode()),headers={"Content-Encoding":"gzip"})])
        data, resolved = scanning.scan_google(URL)
        self.assertEqual(data,comprehensive_data())
        self.assertEqual(resolved,URL)

    def test_unexpected_content_type_encoding_or_utf8_is_rejected(self):
        for response in (Response(html(),headers={"Content-Type":"application/json"}),
                         Response(html(),headers={"Content-Encoding":"br"}),
                         Response(b"\xff\xfe"),Response(b"x",headers={"Content-Length":"bad"})):
            with self.subTest(headers=str(response.headers)):
                self.opener([response])
                with self.assertRaises(scanning.ScanError):
                    scanning.scan_google(URL)

    def test_silent_redirect_in_response_object_is_rejected(self):
        self.opener([Response(html(),url="https://evil.example/")])
        with self.assertRaises(scanning.ScanError):
            scanning.scan_google(URL)

    def test_network_and_http_errors_are_actionable(self):
        for error in (URLError("offline"),TimeoutError(),HTTPError(URL,403,"Forbidden",Message(),io.BytesIO())):
            with self.subTest(error=type(error).__name__):
                self.opener([error])
                with self.assertRaises(scanning.ScanError):
                    scanning.scan_google(URL)

    def test_invalid_resource_limits_do_not_fetch(self):
        calls = self.opener([])
        for kwargs in ({"timeout":True},{"timeout":0},{"timeout":float("inf")},{"timeout":float("nan")},
                       {"max_bytes":0},{"max_bytes":True},{"max_bytes":scanning.MAX_RESPONSE_BYTES+1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(scanning.ScanError):
                scanning.scan_google(URL,**kwargs)
        self.assertEqual(calls,[])


if __name__ == "__main__":
    unittest.main()

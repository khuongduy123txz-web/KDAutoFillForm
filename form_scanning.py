"""Read public Google Forms metadata with bounded GET requests, without Chromium.

Only allowlisted Google form URLs are fetched. Redirect destinations are checked
before following them. This module does not execute scripts or send responses.
"""
from __future__ import annotations

import gzip
from html.parser import HTMLParser
import io
import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_REDIRECTS = 5
_GOOGLE_HOSTS = {"docs.google.com", "forms.gle", "forms.google.com"}
_FORM_PATH = re.compile(r"/forms/(?:u/\d+/)?d/(?:e/)?[A-Za-z0-9_-]+/(?:viewform|formResponse|edit)/?")
_ASSIGNMENT = re.compile(
    r"(?:^|[;\n])\s*(?:(?:var|let|const)\s+)?(?:window\s*\.\s*)?FB_PUBLIC_LOAD_DATA_\s*=\s*",
    re.MULTILINE,
)


class ScanError(ValueError):
    """The public metadata could not be safely fetched or parsed."""


def normalize_google_url(value):
    """Normalize a public form link; never allow auth, private hosts or ports."""
    try:
        value = str(value).strip()
        parts = urlsplit(value)
        host = (parts.hostname or "").lower()
        port = parts.port
    except (ValueError, TypeError) as exc:
        raise ScanError("Link Google Forms không hợp lệ.") from exc
    if (len(value) > 4096 or parts.scheme != "https" or host not in _GOOGLE_HOSTS
            or parts.username is not None or parts.password is not None
            or port not in (None, 443) or parts.netloc.lower() not in _GOOGLE_HOSTS):
        raise ScanError("Cần link Google Forms HTTPS công khai, không kèm tài khoản hoặc cổng riêng.")
    if host == "docs.google.com":
        if not _FORM_PATH.fullmatch(parts.path):
            raise ScanError("Link không phải trang điền Google Forms công khai.")
        path = re.sub(r"/(?:formResponse|edit)/?$", "/viewform", parts.path).rstrip("/")
        return urlunsplit(("https", host, path, "", ""))
    if not parts.path or parts.path == "/" or "\\" in parts.path:
        raise ScanError("Link rút gọn Google Forms chưa có mã biểu mẫu.")
    return urlunsplit(("https", host, parts.path, parts.query, ""))


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


class _Scripts(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.in_script = False
        self.current = []
        self.scripts = []
        self.response_form = False

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "form":
            action = dict(attrs).get("action") or ""
            try:
                parts = urlsplit(action)
            except ValueError:
                parts = None
            if parts and parts.path.rstrip("/").endswith("/formResponse") and (not parts.netloc or parts.hostname == "docs.google.com"):
                self.response_form = True
        if tag.lower() == "script":
            self.in_script = True
            self.current = []

    def handle_endtag(self, tag):
        if tag.lower() == "script" and self.in_script:
            self.scripts.append("".join(self.current))
            self.current = []
            self.in_script = False

    def handle_data(self, data):
        if self.in_script:
            self.current.append(data)


def extract_google_data(source, *, require_form=False):
    """Decode Google's JSON literal while ignoring trailing JavaScript."""
    parser = _Scripts()
    try:
        parser.feed(source)
        parser.close()
    except Exception as exc:
        raise ScanError("Không đọc được trang Google Forms.") from exc
    if parser.in_script:
        parser.scripts.append("".join(parser.current))
    if require_form and not parser.response_form:
        raise ScanError("Google Forms chưa mở trang trả lời; form có thể đã đóng hoặc yêu cầu đăng nhập.")
    decoder = json.JSONDecoder()
    for script in parser.scripts:
        for match in _ASSIGNMENT.finditer(script):
            try:
                data, _ = decoder.raw_decode(script, match.end())
            except (ValueError, RecursionError):
                continue
            if isinstance(data, list) and len(data) > 1 and isinstance(data[1], list):
                return data
    raise ScanError("Không đọc được cấu trúc Google Forms; form có thể đã đóng hoặc yêu cầu đăng nhập.")


def _read_html(response, max_bytes):
    length = response.headers.get("Content-Length")
    if length is not None:
        try:
            declared_length = int(length)
        except ValueError as exc:
            raise ScanError("Độ dài phản hồi Google Forms không hợp lệ.") from exc
        if declared_length < 0 or declared_length > max_bytes:
            raise ScanError("Trang Google Forms quá lớn để đọc.")
    payload = response.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise ScanError("Trang Google Forms quá lớn để đọc.")
    encoding = (response.headers.get("Content-Encoding") or "identity").lower().strip()
    if encoding == "gzip":
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(payload)) as zipped:
                payload = zipped.read(max_bytes + 1)
        except (OSError, EOFError) as exc:
            raise ScanError("Dữ liệu nén từ Google Forms không hợp lệ.") from exc
        if len(payload) > max_bytes:
            raise ScanError("Trang Google Forms quá lớn sau khi giải nén.")
    elif encoding not in ("", "identity"):
        raise ScanError("Kiểu nén phản hồi Google Forms chưa hỗ trợ.")
    content_type = (response.headers.get("Content-Type") or "text/html").split(";", 1)[0].lower().strip()
    if content_type not in ("text/html", "application/xhtml+xml"):
        raise ScanError("Google Forms chưa trả về trang biểu mẫu.")
    try:
        return payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ScanError("Trang Google Forms không có định dạng UTF-8 hợp lệ.") from exc


def scan_google(url, *, timeout=20, max_bytes=MAX_RESPONSE_BYTES):
    """Return (raw_data, canonical_viewform_url) using GET requests only.

    The caller applies its existing schema parser and all field validation.
    No cookies, user credentials, form submissions or browser processes are used.
    """
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout <= 120:
        raise ScanError("Thời gian đọc form không hợp lệ.")
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or not 1 <= max_bytes <= MAX_RESPONSE_BYTES:
        raise ScanError("Giới hạn kích thước form không hợp lệ.")
    current = normalize_google_url(url)
    opener = build_opener(_NoRedirect())
    deadline = time.monotonic() + timeout
    seen = set()
    for redirect_count in range(MAX_REDIRECTS + 1):
        if current in seen:
            raise ScanError("Link Google Forms chuyển hướng thành vòng lặp.")
        seen.add(current)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ScanError("Hết thời gian đọc Google Forms. Vui lòng thử lại.")
        request = Request(current, method="GET", headers={
            "User-Agent": "Mozilla/5.0 (compatible; KDautoform-Metadata/1.0)",
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Encoding": "identity",
        })
        try:
            response = opener.open(request, timeout=remaining)
        except HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308):
                location = exc.headers.get("Location")
                exc.close()
                if not location or redirect_count >= MAX_REDIRECTS:
                    raise ScanError("Google Forms chuyển hướng quá nhiều lần hoặc thiếu địa chỉ đích.") from exc
                destination = urljoin(current, location)
                if (urlsplit(destination).hostname or "").lower() in ("accounts.google.com", "consent.google.com"):
                    raise ScanError("Form yêu cầu đăng nhập hoặc đồng ý truy cập Google; hãy dùng form công khai.") from exc
                current = normalize_google_url(destination)
                continue
            exc.close()
            raise ScanError("Google Forms trả về lỗi HTTP " + str(exc.code) + ". Hãy kiểm tra form còn mở công khai.") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise ScanError("Không kết nối được Google Forms hoặc đã hết thời gian đọc. Vui lòng thử lại.") from exc
        try:
            with response:
                final_url = normalize_google_url(response.geturl())
                # urllib must not silently redirect to another host/URL.
                if final_url != current:
                    raise ScanError("Phản hồi Google Forms chuyển hướng ngoài luồng kiểm tra.")
                source = _read_html(response, max_bytes)
        except ScanError:
            raise
        except (URLError, TimeoutError, OSError) as exc:
            raise ScanError("Không tải xong trang Google Forms. Vui lòng thử lại.") from exc
        if urlsplit(final_url).hostname != "docs.google.com":
            raise ScanError("Link rút gọn chưa chuyển đến trang Google Forms công khai.")
        return extract_google_data(source, require_form=True), final_url
    raise ScanError("Không xác định được trang Google Forms công khai.")

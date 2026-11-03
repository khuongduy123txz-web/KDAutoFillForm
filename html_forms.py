"""Beta adapter for ordinary single-page, native HTML POST surveys.

Custom survey widgets, embedded providers and multi-step applications need their
own adapter. Network access is pinned to checked public IPs, and submission is
permitted only during submit(), to the inspected form action, once.
"""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import math
import re
import socket
import ssl
from urllib.parse import urlsplit, urlunsplit


MAX_RESPONSE_BYTES = 8 * 1024 * 1024


def _public_addresses(host):
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        try:
            addresses = {item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
        except OSError as exc:
            raise ValueError("Không xác định được địa chỉ công khai của website form.") from exc
    else:
        addresses = {str(literal)}
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ValueError("Không cho phép form hoặc tài nguyên trỏ tới địa chỉ nội bộ, localhost hay IP riêng.")
    return sorted(addresses)


def normalize_html_url(value, resolved=False):
    del resolved
    try:
        parts = urlsplit(str(value).strip())
        host = (parts.hostname or "").rstrip(".").encode("idna").decode().lower()
        port = parts.port
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Đường dẫn form HTML không hợp lệ.") from exc
    if (parts.scheme != "https" or not host or parts.username is not None or parts.password is not None
            or port not in (None, 443) or len(str(value)) > 4096
            or host == "localhost" or host.endswith((".localhost", ".local", ".internal"))):
        raise ValueError("Form HTML cần link HTTPS công khai, không kèm tài khoản hoặc cổng riêng.")
    _public_addresses(host)
    authority = "[" + host + "]" if ":" in host else host
    return urlunsplit(("https", authority, parts.path or "/", parts.query, ""))


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, address):
        super().__init__(host, timeout=20, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        stream = socket.create_connection((self.address, 443), timeout=self.timeout)
        try:
            self.sock = self._context.wrap_socket(stream, server_hostname=self.host)
        except Exception:
            stream.close()
            raise


def _fetch_public(request):
    """Resolve once and connect to that exact IP; never follow redirects."""
    url = normalize_html_url(request.url)
    parts = urlsplit(url)
    addresses = _public_addresses(parts.hostname)
    connection = _PinnedHTTPSConnection(parts.hostname, addresses[0])
    headers = {name: value for name, value in request.all_headers().items()
               if name.lower() not in {"host", "connection", "content-length", "accept-encoding", "transfer-encoding"}
               and not name.lower().startswith("proxy-")}
    headers["Accept-Encoding"] = "identity"
    path = parts.path + (("?" + parts.query) if parts.query else "")
    try:
        body = request.post_data_buffer
        if body and len(body) > MAX_RESPONSE_BYTES:
            raise ValueError("Nội dung gửi quá lớn cho form HTML beta.")
        connection.request(request.method, path, body=body, headers=headers)
        response = connection.getresponse()
        if 300 <= response.status < 400:
            raise ValueError("Form HTML đang chuyển hướng. Hãy mở link bằng trình duyệt và dán URL cuối cùng; beta không tự theo chuyển hướng.")
        payload = response.read(MAX_RESPONSE_BYTES + 1)
        if len(payload) > MAX_RESPONSE_BYTES:
            raise ValueError("Tài nguyên form HTML quá lớn cho bản beta.")
        result_headers = {name: value for name, value in response.getheaders()
                          if name.lower() not in {"connection", "transfer-encoding", "content-length"}}
        return response.status, result_headers, payload
    finally:
        connection.close()


_EXTRACT = r"""() => {
 const visible = el => !!el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden';
 const clean = text => String(text || '').replace(/\s+/g, ' ').trim();
 const label = el => clean(el.getAttribute('aria-label') ||
   (el.getAttribute('aria-labelledby') || '').split(/\s+/).map(id => document.getElementById(id)?.textContent || '').join(' ') ||
   Array.from(el.labels || []).map(l => l.textContent).join(' ') || el.placeholder || el.name);
 const candidates = Array.from(document.forms).map((form, index) => ({form, index})).filter(({form}) =>
   visible(form) && form.method.toLowerCase() === 'post' && Array.from(form.elements).some(el => visible(el) && ['INPUT','SELECT','TEXTAREA'].includes(el.tagName)));
 if (candidates.length !== 1) return {error: document.querySelector('iframe') && !candidates.length ?
   'Form nhúng trong iframe cần bộ kết nối riêng. Hãy dùng link trực tiếp của nền tảng.' :
   'Beta cần đúng một form HTML POST hiển thị trên trang; chưa hỗ trợ form GET hoặc nhiều form không rõ phạm vi.'};
 const {form, index} = candidates[0];
 if (form.querySelector('input[type=password],input[autocomplete=current-password],input[autocomplete=new-password]')) return {error:'Không hỗ trợ form đăng nhập hoặc mật khẩu.'};
 if (document.querySelector('[id*=captcha i],[name*=captcha i],.g-recaptcha,.h-captcha,iframe[src*=captcha i]')) return {error:'Form có CAPTCHA; cần xử lý thủ công.'};
 const buttons = Array.from(form.elements).map((el, i) => ({el, i})).filter(({el}) => visible(el) &&
   (el.tagName === 'BUTTON' || (el.tagName === 'INPUT' && ['submit','image','button'].includes(el.type))));
 const controls = Array.from(form.elements).map((el, i) => {
   const type = el.tagName === 'SELECT' ? (el.multiple ? 'select-multiple' : 'select') :
     el.tagName === 'TEXTAREA' ? 'textarea' : el.type;
   if (!['INPUT','SELECT','TEXTAREA'].includes(el.tagName) || ['hidden','submit','reset','button','image'].includes(type)) return null;
   const legend = el.closest('fieldset')?.querySelector(':scope > legend');
   return {index:i,type,name:el.name || '',label:label(el),question:clean(legend?.textContent),value:el.value,
     visible:visible(el),disabled:el.matches(':disabled'),readonly:!!el.readOnly,required:!!el.required,
     min:el.getAttribute('min'),max:el.getAttribute('max'),step:el.getAttribute('step'),pattern:el.getAttribute('pattern'),
     minlength:el.getAttribute('minlength'),maxlength:el.getAttribute('maxlength'),
     options:el.tagName === 'SELECT' ? Array.from(el.options).filter(o=>!o.disabled && o.value !== '').map(o=>({label:clean(o.textContent),value:o.value})) : null};
 }).filter(Boolean);
 return {title:clean(document.querySelector('h1')?.textContent || document.title || 'Form HTML'),
   index,action:form.action,method:form.method,target:form.target,controls,
   buttons:buttons.map(({el,i})=>({index:i,text:clean(el.textContent || el.value || el.getAttribute('aria-label')),
     type:el.type,action:el.getAttribute('formaction') ? el.formAction : null,method:el.getAttribute('formmethod'),target:el.getAttribute('formtarget')}))};
}"""


def parse_html_schema(data, url, error_type=ValueError, plain=None):
    plain = plain or (lambda value: str(value).casefold())
    if not isinstance(data, dict) or data.get("error"):
        raise error_type((data or {}).get("error", "Không đọc được cấu trúc form HTML."))
    if data.get("target") not in ("", None, "_self"):
        raise error_type("Beta chưa hỗ trợ form mở kết quả trong cửa sổ khác.")
    try:
        action = normalize_html_url(data["action"])
    except (ValueError, KeyError) as exc:
        raise error_type("Địa chỉ nhận phản hồi của form không an toàn hoặc không hợp lệ.") from exc
    buttons = data.get("buttons", [])
    if any(re.search(r"\b(next|continue|tiep|tiep tuc)\b", plain(button["text"])) for button in buttons):
        raise error_type("Form HTML nhiều bước cần bộ kết nối riêng; beta chỉ hỗ trợ form một trang.")
    submitters = [button for button in buttons if button["type"] == "submit"]
    if len(submitters) != 1:
        raise error_type("Beta cần đúng một nút Gửi HTML chuẩn; chưa hỗ trợ nút hoặc luồng gửi tùy biến.")
    submit = submitters[0]
    if ((submit.get("action") and submit["action"] != data["action"])
            or (submit.get("method") and submit["method"].lower() != "post")
            or submit.get("target") not in (None, "", "_self")):
        raise error_type("Nút gửi ghi đè địa chỉ/phương thức hoặc mở cửa sổ khác; beta chưa hỗ trợ.")
    controls = data.get("controls", [])
    if len(controls) > 500:
        raise error_type("Form có quá nhiều ô nhập cho bộ kết nối HTML beta.")
    fields, handled, names = [], set(), set()
    for control in controls:
        if control["index"] in handled:
            continue
        title = control["question"] or control["label"] or control["name"]
        kind = control["type"]
        if control["disabled"] or control["readonly"]:
            if control["required"]:
                raise error_type("Câu bắt buộc đang bị khóa hoặc chỉ đọc: " + title)
            continue
        if not control["visible"]:
            raise error_type("Form có câu hỏi ẩn hoặc nhiều bước chưa hỗ trợ: " + title)
        if not control["name"]:
            raise error_type("Ô nhập thiếu tên để gửi phản hồi: " + title)
        if kind in ("password", "file") or re.search(r"\b(password|mat khau|credit card|so the|cvv|cvc|otp|passcode)\b", plain(title + " " + control["name"])):
            raise error_type("Không hỗ trợ câu tải tệp, đăng nhập hoặc thanh toán: " + title)
        if control.get("pattern"):
            raise error_type("Mẫu kiểm tra định dạng HTML tùy biến chưa được hỗ trợ: " + title)
        group = ([other for other in controls if other["name"] == control["name"] and other["type"] == kind]
                 if kind in ("radio", "checkbox") else [control])
        if any(not other["visible"] or other["disabled"] or other["readonly"] for other in group):
            raise error_type("Nhóm lựa chọn có đáp án ẩn hoặc bị khóa: " + title)
        handled.update(other["index"] for other in group)
        if control["name"] in names:
            raise error_type("Các câu hỏi trùng tên ô gửi; beta không xác định được duy nhất: " + title)
        names.add(control["name"])
        identifier = "html." + hashlib.sha256((control["name"] + ":" + kind).encode()).hexdigest()[:20]
        field = {"id": identifier, "item_id": identifier, "question": title, "description": "",
                 "kind": "text", "options": [], "required": any(other["required"] for other in group),
                 "other": False, "page": 0, "row": None, "row_index": 0, "actions": {}, "rules": [],
                 "exclusive_columns": False, "html_controls": [other["index"] for other in group],
                 "html_type": kind, "html_values": []}
        if kind in ("radio", "checkbox", "select", "select-multiple"):
            choices = ([{"label": other["label"], "value": other["value"]} for other in group]
                       if kind in ("radio", "checkbox") else control["options"])
            labels, values = [choice["label"] for choice in choices], [choice["value"] for choice in choices]
            if not choices or not all(labels) or len(set(labels)) != len(labels) or len(set(values)) != len(values):
                raise error_type("Nhãn hoặc giá trị đáp án trống/trùng, chưa thể xác định đúng lựa chọn: " + title)
            field["options"], field["html_values"] = labels, values
            field["kind"] = {"radio": "single", "checkbox": "multiple", "select": "dropdown", "select-multiple": "multiple"}[kind]
            if field["kind"] == "multiple":
                field["minimum"] = sum(other["required"] for other in group) if kind == "checkbox" else 1
                field["minimum"] = max(1, field["minimum"])
                field["maximum"] = len(choices)
                field["html_required_options"] = [other["label"] for other in group if other["required"]] if kind == "checkbox" else []
                if field["html_required_options"] and len(field["html_required_options"]) != len(choices):
                    raise error_type("Nhóm checkbox bắt buộc riêng từng đáp án cần cấu hình chuyên biệt: " + title)
        elif kind in ("date", "datetime-local", "time"):
            field["kind"] = "time" if kind == "time" else "date"
            field.update(has_year=True, has_time=kind == "datetime-local", duration=False)
            if control.get("min") or control.get("max") or control.get("step") not in (None, "", "any", "60"):
                raise error_type("Ngày/giờ có giới hạn riêng chưa được hỗ trợ trong beta: " + title)
        elif kind in ("text", "search", "email", "url", "tel", "textarea", "number"):
            field["kind"] = "paragraph" if kind == "textarea" else "text"
            if kind == "email":
                field["rules"].append([2, 102, []])
            if kind == "url":
                field["rules"].append([2, 103, []])
            for attribute, code in (("minlength", 203), ("maxlength", 202)):
                if control.get(attribute) is not None:
                    try:
                        length = int(control[attribute])
                    except ValueError as exc:
                        raise error_type("Giới hạn độ dài HTML không hợp lệ: " + title) from exc
                    if length < 0 or length > 100000:
                        raise error_type("Giới hạn độ dài HTML chưa hỗ trợ: " + title)
                    field["rules"].append([6, code, [str(length)]])
            if kind == "number":
                try:
                    low = float(control["min"]) if control.get("min") is not None else None
                    high = float(control["max"]) if control.get("max") is not None else None
                    low = float(low if low is not None else min(0, high - 100) if high is not None else 0)
                    high = float(high if high is not None else max(100, low + 100))
                    integer = control.get("step") != "any"
                    if (not math.isfinite(low) or not math.isfinite(high) or low > high
                            or control.get("step") not in (None, "", "1", "any") or (integer and not low.is_integer())):
                        raise ValueError()
                except ValueError as exc:
                    raise error_type("Giới hạn hoặc bước số HTML chưa được hỗ trợ: " + title) from exc
                field["rules"].append([1, 7, [str(low), str(high)]])
                if integer:
                    field["rules"].append([1, 10, []])
        else:
            raise error_type("Kiểu ô nhập HTML chưa được hỗ trợ: " + kind + " — " + title)
        fields.append(field)
    if not fields:
        raise error_type("Không có câu hỏi HTML chuẩn được hỗ trợ. Form JavaScript tùy biến cần bộ kết nối riêng.")
    return {"url": url, "provider": "html", "title": data["title"], "fields": fields,
            "pages": [{"index": 0, "id": 0, "title": "Trang duy nhất"}], "question_count": len(fields),
            "branching": False, "confirmation": "", "html_form_index": data["index"],
            "html_action": action, "html_submit_index": submit["index"]}


def create_html_browser(base_class, error_type, normalized, plain, fingerprint, validate_answers):
    class HtmlFormsBrowser(base_class):
        def new_session(self):
            self.report = None
            self._blocked_reason = None
            self._can_post = self._posted = False
            if self.session is not None:
                self.session.close()
            self.session = self.browser.new_context(locale="vi-VN", service_workers="block", accept_downloads=False)
            self.session.route("**/*", self._safe_route)
            self.session.add_init_script("""for(const name of ['WebSocket','EventSource','RTCPeerConnection'])
                Object.defineProperty(window,name,{value:function(){throw Error('Unsupported network channel')}});
                navigator.sendBeacon=()=>false;""")
            self.page = self.session.new_page()
            self.page.set_default_timeout(15000)
            self.page.set_default_navigation_timeout(45000)

        def _safe_route(self, route):
            request = route.request
            try:
                if request.resource_type in ("image", "media", "font"):
                    route.abort()
                    return
                url = normalize_html_url(request.url)
                if request.method == "POST":
                    if not self._can_post or self._posted or url != self._html_action:
                        raise ValueError("Trang yêu cầu gửi dữ liệu ngoài nút Gửi của form; beta đã dừng.")
                    self._posted = True
                elif request.method not in ("GET", "HEAD"):
                    raise ValueError("Trang sử dụng phương thức mạng tùy biến chưa được hỗ trợ.")
                status, headers, body = _fetch_public(request)
                route.fulfill(status=status, headers=headers, body=body)
            except Exception as exc:
                self._blocked_reason = str(exc) if isinstance(exc, ValueError) else "Không tải được tài nguyên HTTPS công khai của form."
                route.abort()

        def _check_network(self):
            if self._blocked_reason:
                raise error_type(self._blocked_reason)

        def read_schema(self):
            self._check_network()
            try:
                url = normalize_html_url(self.page.url)
                data = self.page.evaluate(_EXTRACT)
                schema = parse_html_schema(data, url, error_type, plain)
            except ValueError as exc:
                raise error_type(str(exc)) from exc
            self._html_action = schema["html_action"]
            return schema

        def load(self, url):
            self.new_session()
            try:
                self.page.goto(normalize_html_url(url), wait_until="domcontentloaded")
            except Exception as exc:
                self._check_network()
                raise error_type("Không mở được form HTML HTTPS công khai.") from exc
            return self.read_schema()

        def _control(self, index):
            element = self.page.locator("form").nth(self._form_index)
            handle = element.evaluate_handle("(form,index)=>form.elements[index]", index)
            control = handle.as_element()
            if control is None:
                raise error_type("Ô nhập HTML đã thay đổi trong lúc điền.")
            return control

        def fill_field(self, field, value):
            current = self._html_fields[field["id"]]
            kind, indices = current["html_type"], current["html_controls"]
            control = self._control(indices[0])
            if kind in ("radio", "checkbox"):
                wanted = value if isinstance(value, list) else [value]
                for label, index in zip(current["options"], indices):
                    self._control(index).set_checked(label in wanted)
            elif kind in ("select", "select-multiple"):
                wanted = value if isinstance(value, list) else [value]
                control.select_option([current["html_values"][current["options"].index(label)] for label in wanted])
            elif kind in ("date", "datetime-local"):
                text = f'{value["year"]:04d}-{value["month"]:02d}-{value["day"]:02d}'
                if kind == "datetime-local":
                    text += f'T{value["hour"]:02d}:{value["minute"]:02d}'
                control.fill(text)
            elif kind == "time":
                control.fill(f'{value["hour"]:02d}:{value["minute"]:02d}')
            else:
                control.fill(str(value))
                if control.input_value() != str(value):
                    raise error_type("Ô nhập không nhận đủ nội dung: " + field["question"])
            self._check_network()

        def _prepare(self, schema, answers):
            self.new_session()
            self.page.goto(normalize_html_url(schema["url"]), wait_until="domcontentloaded")
            current = self.read_schema()
            if fingerprint(current) != fingerprint(schema) or current["html_action"] != schema["html_action"]:
                raise error_type("Form HTML đã thay đổi câu hỏi, quy tắc hoặc địa chỉ gửi. Hãy đọc lại.")
            self._form_index = current["html_form_index"]
            self._html_fields = {field["id"]: field for field in current["fields"]}
            for field in schema["fields"]:
                self.fill_field(field, answers[field["id"]])
            self._check_network()
            final = self.read_schema()
            if fingerprint(final) != fingerprint(current) or final["html_action"] != current["html_action"]:
                raise error_type("Form thay đổi động sau khi chọn đáp án; beta không tự bỏ qua câu hỏi mới.")
            invalid = self.page.locator("form").nth(self._form_index).evaluate(
                "form=>Array.from(form.elements).filter(el=>el.willValidate&&!el.validity.valid).map(el=>el.name)")
            if invalid:
                raise error_type("Chưa thỏa kiểm tra HTML ở ô: " + ", ".join(invalid))
            self._html_schema = final
            self.report = {"pages": 1, "page_indices": [0], "questions": len(schema["fields"]),
                           "fields": len(schema["fields"]), "field_ids": [field["id"] for field in schema["fields"]]}
            return self.report

        def submit(self, schema):
            del schema
            if not self.report:
                raise error_type("Chưa điền đủ form HTML.")
            current = self.read_schema()
            if fingerprint(current) != fingerprint(self._html_schema) or current["html_action"] != self._html_schema["html_action"]:
                raise error_type("Form HTML đã thay đổi trước khi gửi.")
            self._can_post = True
            try:
                with self.page.expect_response(lambda response: response.request.method == "POST"
                        and response.url == self._html_action, timeout=30000) as pending:
                    self._control(current["html_submit_index"]).click()
                response = pending.value
                self._check_network()
                if not 200 <= response.status < 300:
                    raise error_type("Website chưa xác nhận lưu phản hồi HTML.")
                self.page.wait_for_function(r"""() => {
                    const visible=el=>!!el.getClientRects().length;
                    const text=document.body.innerText.toLowerCase();
                    const phrase=/thank you for (your|the) (response|submission)|response (has been |was )?(recorded|submitted|saved)|submission (successful|received)|cảm ơn.{0,50}(phản hồi|trả lời)|đã (ghi nhận|lưu|gửi thành công).{0,50}(phản hồi|câu trả lời)|gửi (phản hồi )?thành công/i.test(text);
                    const marker=Array.from(document.querySelectorAll('[data-submission-success=true],.submission-success,#submission-success')).some(visible);
                    const form=Array.from(document.forms).some(el=>visible(el)&&el.querySelector('input:not([type=hidden]),textarea,select'));
                    return phrase && (marker || !form);
                }""", timeout=15000)
                return True
            except Exception as exc:
                self._check_network()
                raise error_type("Chưa xác định website đã lưu phản hồi hay chưa. Không tự gửi lại. " + str(exc)) from exc
            finally:
                self._can_post = False
    return HtmlFormsBrowser

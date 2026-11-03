"""Microsoft Forms adapter: public runtime metadata and ordinary browser controls.

This deliberately uses the same browser flow a respondent sees. Reading and
preparing a form never presses Submit; authentication, uploads and unknown
question types stop rather than silently omitting questions.
"""
from __future__ import annotations

import html
import json
import re
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


MICROSOFT_HOSTS = frozenset({"forms.office.com", "forms.office365.com", "forms.cloud.microsoft", "forms.microsoft.com"})


def is_microsoft_url(value):
    try:
        return urlsplit(str(value).strip()).hostname.lower() in MICROSOFT_HOSTS
    except (ValueError, AttributeError):
        return False


def normalize_microsoft_url(value, resolved=False):
    parts = urlsplit(str(value).strip())
    if (parts.scheme != "https" or parts.username or parts.password
            or parts.netloc.lower() not in MICROSOFT_HOSTS):
        raise ValueError("Cần link HTTPS Microsoft Forms công khai, không kèm tài khoản hoặc cổng riêng.")
    path = parts.path.rstrip("/")
    if re.fullmatch(r"/(?:r|e)/[A-Za-z0-9_-]+", path) and not resolved:
        return urlunsplit(("https", parts.netloc.lower(), path, parts.query, ""))
    if path.casefold() != "/pages/responsepage.aspx":
        raise ValueError("Cần link trả lời Microsoft Forms, không phải link thiết kế hoặc quản trị.")
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    ids = [v for k, v in pairs if k.casefold() == "id"]
    if len(ids) != 1 or not re.fullmatch(r"[A-Za-z0-9_.-]{1,1024}", ids[0]):
        raise ValueError("Link Microsoft Forms thiếu mã form hợp lệ.")
    return urlunsplit(("https", parts.netloc.lower(), "/Pages/ResponsePage.aspx", urlencode(pairs), ""))


def _get(value, name, default=None):
    if not isinstance(value, dict):
        return default
    return next((v for k, v in value.items() if k.casefold() == name.casefold()), default)


def _object(value, label, error_type):
    if value in (None, ""):
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError) as exc:
            raise error_type("Microsoft Forms trả cấu hình không hợp lệ: " + label) from exc
    if not isinstance(value, dict):
        raise error_type("Microsoft Forms trả cấu hình không hợp lệ: " + label)
    return value


def _text(value):
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", str(value or ""))).split())


def find_form_payload(payload, depth=0):
    """Find complete form definitions inside startup/prefetch wrappers."""
    if depth > 8:
        return None
    if isinstance(payload, str) and payload.lstrip().startswith(("{", "[")):
        try:
            payload = json.loads(payload)
        except ValueError:
            return None
    if isinstance(payload, dict):
        if isinstance(_get(payload, "questions"), list) and _get(payload, "id"):
            return payload
        for key, value in payload.items():
            if key.casefold() in ("answers", "responses"):
                continue
            result = find_form_payload(value, depth + 1)
            if result is not None:
                return result
    elif isinstance(payload, list):
        for value in payload:
            result = find_form_payload(value, depth + 1)
            if result is not None:
                return result
    return None


def _options(question, info, error_type):
    choices = _get(info, "Choices") or _get(question, "choices") or []
    if not isinstance(choices, list):
        raise error_type("Không đọc được các đáp án Microsoft Forms.")
    labels, actions = [], {}
    for choice in sorted(choices, key=lambda v: _get(v, "order", 0) or 0):
        if isinstance(choice, str):
            label, branch = _text(choice), None
        else:
            label = _text(_get(choice, "description") or _get(choice, "displayText") or _get(choice, "label"))
            branch = _get(_get(choice, "BranchInfo", {}), "TargetQuestionId")
        if not label or label in labels:
            raise error_type("Đáp án Microsoft Forms trống hoặc trùng; không thể chọn duy nhất.")
        labels.append(label)
        if branch not in (None, "", "Next", "next"):
            actions[label] = str(branch)
    return labels, actions


def _rules(info, title, error_type):
    validation = _get(info, "Validation") or _get(info, "NumberValidation")
    if not validation:
        return []
    validation = _object(validation, title, error_type)
    rule = _get(validation, "rule", _get(validation, "NumberValidationRule"))
    aliases = {"IsNumber": 0, "Greater": 1, "GreaterOrEqual": 2, "Less": 3,
               "LessOrEqual": 4, "Equal": 5, "NotEqual": 6, "Between": 7,
               "NotBetween": 8, "MaxLength": 9, "MinLength": 10, "Email": 11,
               "Contains": 12, "NotContains": 13, "Url": 14, "WholeNumber": 15}
    rule = aliases.get(rule, rule)
    if rule in (None, -1, "None"):
        return []
    minimum = _get(validation, "numberMinBoundary", _get(validation, "minimum"))
    maximum = _get(validation, "numberMaxBoundary", _get(validation, "maximum"))
    text = _get(validation, "textInput", "")
    if rule in (0, 15):
        return [[1, 9 if rule == 0 else 10, []]]
    if rule in range(1, 9):
        args = [minimum] + ([maximum] if rule in (7, 8) else [])
        try:
            args = [str(float(value)) for value in args]
        except (ValueError, TypeError) as exc:
            raise error_type("Quy tắc số chưa hỗ trợ: " + title) from exc
        return [[1, rule, args]]
    if rule in (9, 10):
        boundary = maximum if rule == 9 and maximum is not None else minimum
        if boundary is None:
            boundary = text
        try:
            boundary = int(boundary)
        except (ValueError, TypeError) as exc:
            raise error_type("Quy tắc độ dài chưa hỗ trợ: " + title) from exc
        return [[6, 202 if rule == 9 else 203, [str(boundary)]]]
    if rule in (11, 14):
        return [[2, 102 if rule == 11 else 103, []]]
    if rule in (12, 13) and isinstance(text, str):
        return [[2, 100 if rule == 12 else 101, [text]]]
    raise error_type("Quy tắc Microsoft Forms chưa hỗ trợ: " + title)


def parse_microsoft_schema(payload, url, error_type=ValueError):
    form = find_form_payload(payload)
    if form is None:
        raise error_type("Không đọc được toàn bộ cấu trúc Microsoft Forms; form có thể đóng hoặc yêu cầu đăng nhập.")
    settings = _object(_get(form, "settings", _get(form, "deserializedSettings")), "form", error_type)
    if (_get(settings, "RequiresUniqueResponse") or _get(settings, "FillOutTimeLimit")
            or _get(form, "FillOutTimeLimit")):
        raise error_type("Microsoft Forms giới hạn một phản hồi hoặc có bộ đếm giờ; cần xử lý thủ công.")
    questions = _get(form, "questions", [])
    descriptive = _get(form, "descriptiveQuestions", []) or []
    if not isinstance(descriptive, list):
        raise error_type("Không đọc được các phần Microsoft Forms.")
    questions = sorted(questions, key=lambda q: _get(q, "order", 0) or 0)
    by_id = {}
    for question in questions + descriptive:
        identifier = str(_get(question, "id", ""))
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", identifier) or identifier in by_id:
            raise error_type("Mã câu hỏi Microsoft Forms trống, trùng hoặc không hợp lệ.")
        by_id[identifier] = question
    children = {}
    for question in questions:
        group = _get(question, "groupId")
        if group:
            children.setdefault(str(group), []).append(question)
    ordered = sorted([q for q in questions if not _get(q, "groupId")] + descriptive,
                     key=lambda q: _get(q, "order", 0) or 0)
    pages = [{"index": 0, "id": "intro", "title": "Giới thiệu"}]
    fields, branching, question_count, section_ids = [], False, 0, []
    for question in ordered:
        identifier = str(_get(question, "id"))
        title = _text(_get(question, "title"))
        kind = _get(question, "type", "")
        if kind in ("Question.ColumnGroup", "Question.Section"):
            if fields or len(pages) > 1:
                pages.append({"index": len(pages), "id": identifier, "title": title or "Phần mới"})
            else:
                pages[0].update(id=identifier, title=title or "Giới thiệu")
            section_ids.append(identifier)
            continue
        if kind in ("Question.Text", "Question.Description"):
            continue
        if not title:
            raise error_type("Microsoft Forms có câu hỏi không có tiêu đề.")
        info = _object(_get(question, "questionInfo", _get(question, "deserializedQuestionInfo")), title, error_type)
        options, actions = _options(question, info, error_type)
        branch = _get(_get(question, "BranchInfo", {}), "TargetQuestionId") or _get(_get(info, "BranchInfo", {}), "TargetQuestionId")
        branching |= bool(actions or branch or _get(question, "branchingLogic"))
        field = {"id": "ms." + identifier, "item_id": identifier, "ms_question_id": identifier,
                 "ms_parent_id": identifier, "question": title, "description": _text(_get(question, "subtitle")),
                 "options": options, "required": bool(_get(question, "isRequired", _get(question, "required", False))),
                 "other": bool(_get(info, "AllowOtherAnswer")), "page": len(pages) - 1,
                 "row": None, "row_index": 0, "actions": actions, "rules": [], "exclusive_columns": False}
        if kind == "Question.TextField":
            field["kind"] = "paragraph" if _get(info, "Multiline") else "text"
            field["rules"] = _rules(info, title, error_type)
        elif kind == "Question.Choice":
            choice_type = _get(info, "ChoiceType", 2 if _get(question, "allowMultipleValues") else 1)
            if choice_type not in (1, 2):
                raise error_type("Kiểu chọn đáp án Microsoft Forms chưa hỗ trợ: " + title)
            field["kind"] = "multiple" if choice_type == 2 else "dropdown" if str(_get(info, "OptionDisplayStyle", "")).casefold() in ("dropdown", "drop-down") else "single"
            if choice_type == 2:
                field.update(minimum=1, maximum=len(options))
                restriction = _get(info, "ChoiceRestrictionType", "None")
                if restriction not in (None, "None", ""):
                    try:
                        limit = int(_get(info, "ChoiceRestrictionValue"))
                    except (ValueError, TypeError) as exc:
                        raise error_type("Không đọc được giới hạn chọn đáp án: " + title) from exc
                    if restriction in ("Exactly", "AtLeast"):
                        field["minimum"] = limit
                    if restriction in ("Exactly", "AtMost"):
                        field["maximum"] = limit
                    if restriction not in ("Exactly", "AtMost", "AtLeast"):
                        raise error_type("Giới hạn chọn đáp án chưa hỗ trợ: " + title)
                if not 1 <= field["minimum"] <= field["maximum"] <= len(options):
                    raise error_type("Giới hạn chọn đáp án không hợp lệ: " + title)
        elif kind in ("Question.Rating", "Question.NPS"):
            try:
                low = 0 if kind == "Question.NPS" else int(_get(info, "MinRating", 1))
                high = 10 if kind == "Question.NPS" else low + int(_get(info, "Length", 5)) - 1
            except (ValueError, TypeError) as exc:
                raise error_type("Không đọc được thang điểm: " + title) from exc
            if not 0 <= low < high <= 100:
                raise error_type("Thang điểm Microsoft Forms chưa hỗ trợ: " + title)
            field.update(kind="scale", options=[str(i) for i in range(low, high + 1)])
        elif kind == "Question.DateTime":
            if _get(info, "Time"):
                raise error_type("Câu hỏi ngày kèm giờ Microsoft Forms chưa hỗ trợ: " + title)
            field.update(kind="date", has_year=True, has_time=False)
        elif kind in ("Question.MatrixChoiceGroup", "Question.Matrix"):
            rows = children.get(identifier, [])
            if not rows:
                raise error_type("Không đọc được các dòng Likert Microsoft Forms: " + title)
            for row_index, row in enumerate(rows):
                if _get(row, "type") != "Question.MatrixChoice":
                    raise error_type("Kiểu dòng Likert chưa hỗ trợ: " + title)
                row_id = str(_get(row, "id"))
                row_field = {**field, "id": "ms." + row_id, "ms_question_id": row_id,
                             "kind": "grid", "row": _text(_get(row, "title")), "row_index": row_index,
                             "required": field["required"] or bool(_get(row, "required", False))}
                if not row_field["row"] or not options:
                    raise error_type("Dòng hoặc đáp án Likert bị trống: " + title)
                fields.append(row_field)
            question_count += 1
            continue
        elif kind == "Question.FileUpload":
            raise error_type("Microsoft Forms yêu cầu tải tệp/đăng nhập: " + title + ". Cần xử lý thủ công.")
        else:
            raise error_type("Chưa hỗ trợ loại câu hỏi Microsoft Forms " + str(kind) + ": " + title)
        if field["kind"] not in ("text", "paragraph", "date") and not field["options"]:
            raise error_type("Không đọc được đáp án Microsoft Forms: " + title)
        fields.append(field)
        question_count += 1
    orphaned = set(children) - {str(_get(q, "id")) for q in ordered if _get(q, "type") in ("Question.MatrixChoiceGroup", "Question.Matrix")}
    if orphaned or not fields:
        raise error_type("Cấu trúc nhóm câu hỏi Microsoft Forms chưa hỗ trợ hoặc form trống.")
    return {"url": normalize_microsoft_url(url, resolved=True), "provider": "microsoft",
            "title": _text(_get(form, "title")) or "Microsoft Forms", "fields": fields,
            "pages": pages, "question_count": question_count, "branching": bool(branching),
            "confirmation": _text(_get(settings, "ThankYouMessage")), "section_ids": section_ids,
            "ms_form_id": str(_get(form, "id"))}


def create_microsoft_browser(base_class, error_type, normalized, plain, fingerprint, validate_answers):
    class MicrosoftFormsBrowser(base_class):
        def new_session(self):
            super().new_session()
            self._ms_payload = None
            self.page.on("response", self._capture_metadata)

        def _capture_metadata(self, response):
            parts = urlsplit(response.url)
            if (response.request.method != "GET" or parts.hostname not in MICROSOFT_HOSTS
                    or not ("runtimeforms" in parts.path.casefold() or "responsepagestartup.ashx" in parts.path.casefold())):
                return
            try:
                if response.ok:
                    form = find_form_payload(response.json())
                    if form is not None:
                        self._ms_payload = form
            except Exception:
                pass

        def read_schema(self):
            try:
                url = normalize_microsoft_url(self.page.url, resolved=True)
            except ValueError as exc:
                raise error_type("Microsoft Forms yêu cầu đăng nhập hoặc đã chuyển tới trang không hỗ trợ.") from exc
            deadline = time.monotonic() + 25
            fetched = False
            while time.monotonic() < deadline:
                if self._ms_payload is not None:
                    return parse_microsoft_schema(self._ms_payload, url, error_type)
                bootstrap = self.page.evaluate("""() => {
                    const s=window.OfficeFormServerInfo||{};
                    return {url:s.prefetchFormUrl,token:s.antiForgeryToken,
                            payload:s.prefetchFormData||s.form||null};
                }""")
                inline = find_form_payload(bootstrap.get("payload"))
                if inline is not None:
                    self._ms_payload = inline
                    continue
                prefetch = bootstrap.get("url")
                if prefetch and not fetched:
                    parts = urlsplit(prefetch)
                    if (parts.scheme != "https" or parts.netloc.lower() not in MICROSOFT_HOSTS
                            or not parts.path.casefold().startswith("/formapi/api/")
                            or "runtimeforms" not in parts.path.casefold()):
                        raise error_type("Đường dẫn cấu trúc Microsoft Forms không hợp lệ.")
                    fetched = True
                    headers = {"Accept": "application/json"}
                    if bootstrap.get("token"):
                        headers["__RequestVerificationToken"] = bootstrap["token"]
                    response = self.session.request.get(prefetch, headers=headers, timeout=20000)
                    if response.ok:
                        self._ms_payload = find_form_payload(response.json())
                        continue
                    raise error_type("Không đọc được Microsoft Forms công khai. Kiểm tra quyền Anyone can respond.")
                self.page.wait_for_timeout(150)
            raise error_type("Không lấy được toàn bộ câu hỏi Microsoft Forms. Form có thể đóng, cần đăng nhập hoặc cấu trúc chưa hỗ trợ.")

        def _start(self):
            if self.page.locator('[data-automation-id="questionItem"]:visible').count():
                return
            button = self.button(("Bắt đầu", "Bắt đầu ngay", "Start", "Start now"))
            if button is not None:
                button.click()

        def _show_questions(self):
            deadline = time.monotonic() + 20
            started = False
            while time.monotonic() < deadline:
                if self.page.locator('[data-automation-id="questionItem"]:visible').count():
                    return
                if not started:
                    button = self.button(("Bắt đầu", "Bắt đầu ngay", "Start", "Start now"))
                    if button is not None:
                        button.click()
                        started = True
                self.page.wait_for_timeout(150)
            raise error_type("Không mở được câu hỏi Microsoft Forms. Kiểm tra form công khai, đăng nhập hoặc CAPTCHA.")

        def load(self, url):
            self.new_session()
            self.page.goto(normalize_microsoft_url(url), wait_until="domcontentloaded")
            return self.read_schema()

        def question_card(self, field, required=True):
            for identifier in dict.fromkeys((field["ms_question_id"], field["ms_parent_id"])):
                for selector in (f'[id="QuestionId_{identifier}"]', f'[data-question-id="{identifier}"]'):
                    nodes = self.page.locator(selector)
                    for node in nodes.all():
                        if node.is_visible():
                            parents = node.locator("xpath=ancestor-or-self::*[@data-automation-id='questionItem'][1]")
                            return parents.first if parents.count() else node
            matches = []
            for card in self.page.locator('[data-automation-id="questionItem"]:visible').all():
                title = card.locator('[data-automation-id="questionTitle"]')
                if title.count():
                    text = title.first.evaluate("""el => {const c=el.cloneNode(true);c.querySelectorAll('[data-automation-id="questionOrdinal"],[data-automation-id="requiredStar"],[id^="QuestionInfo_"],[role="log"]').forEach(n=>n.remove());return c.textContent;}""")
                    if normalized(text) in (field["question"], field.get("row")):
                        matches.append(card)
            if len(matches) == 1:
                return matches[0]
            if required:
                raise error_type("Không xác định được duy nhất câu hỏi Microsoft Forms: " + field["question"])
            return None

        def select(self, control, checked=True):
            from playwright.sync_api import expect
            if self.checked(control) != checked:
                identifier = control.get_attribute("id")
                label = self.page.locator("label[for=" + json.dumps(identifier) + "]") if identifier else None
                if label is not None and label.count() == 1 and label.is_visible():
                    label.click()
                else:
                    parents = control.locator("xpath=ancestor::label[1]")
                    (parents.first if parents.count() else control).click()
            if control.get_attribute("aria-checked") is not None:
                expect(control).to_have_attribute("aria-checked", "true" if checked else "false")
            elif checked:
                expect(control).to_be_checked()
            else:
                expect(control).not_to_be_checked()

        def choice(self, scope, field, value, role):
            controls = scope.get_by_role(role)
            exact = controls.filter(has_text=re.compile(r"^" + re.escape(str(value)) + r"$"))
            named = scope.get_by_role(role, name=str(value), exact=True)
            if named.count() == 1:
                return named
            for control in controls.all():
                if control.get_attribute("data-value") == str(value):
                    return control
                label = normalized(control.get_attribute("aria-label") or "")
                if field["kind"] == "scale" and re.match(r"^" + re.escape(str(value)) + r"(?:\s|$)", label):
                    return control
            if exact.count() == 1:
                return exact
            if field["kind"] == "scale" and controls.count() == len(field["options"]):
                return controls.nth(field["options"].index(str(value)))
            raise error_type("Không tìm thấy lựa chọn Microsoft Forms: " + str(value))

        def fill_field(self, field, value):
            card = self.question_card(field)
            if field["kind"] == "dropdown":
                native = card.locator("select:visible")
                if native.count() == 1:
                    native.select_option(label=value)
                    return
                controls = card.locator('[role="combobox"]:visible,[aria-haspopup="listbox"]:visible')
                if controls.count() != 1:
                    raise error_type("Không xác định được danh sách Microsoft Forms: " + field["question"])
                controls.click()
                options = [option for option in self.page.get_by_role("option", name=value, exact=True).all() if option.is_visible()]
                if len(options) != 1:
                    raise error_type("Không xác định được đáp án trong danh sách: " + value)
                options[0].click()
                return
            if field["kind"] == "date":
                native = card.locator('input[type="date"]:visible')
                if native.count() == 1:
                    native.fill(f'{value["year"]:04d}-{value["month"]:02d}-{value["day"]:02d}')
                    return
                inputs = card.get_by_role("textbox")
                if inputs.count() != 1:
                    raise error_type("Không xác định được ô ngày Microsoft Forms: " + field["question"])
                hint = (inputs.get_attribute("placeholder") or "") + " " + (inputs.get_attribute("aria-label") or "")
                hint = hint.casefold()
                if re.search(r"m{1,2}[/.-]d{1,2}[/.-]y{2,4}", hint):
                    text = f'{value["month"]:02d}/{value["day"]:02d}/{value["year"]:04d}'
                elif re.search(r"d{1,2}[/.-]m{1,2}[/.-]y{2,4}", hint):
                    text = f'{value["day"]:02d}/{value["month"]:02d}/{value["year"]:04d}'
                elif re.search(r"y{2,4}[/.-]m{1,2}[/.-]d{1,2}", hint):
                    text = f'{value["year"]:04d}-{value["month"]:02d}-{value["day"]:02d}'
                else:
                    raise error_type("Chưa nhận diện được định dạng ngày Microsoft Forms: " + field["question"])
                inputs.fill(text)
                inputs.press("Tab")
                return
            return super().fill_field(field, value)

        def _signature(self):
            return self.page.evaluate("""() => JSON.stringify(Array.from(document.querySelectorAll('[data-automation-id="questionItem"],[data-automation-id="sectionTitle"]')).filter(e=>e.getClientRects().length).map(e=>e.querySelector('[id^="QuestionId_"]')?.id||e.textContent?.trim().slice(0,250)))""")

        def _prepare(self, schema, answers):
            validate_answers(schema, answers)
            self.new_session()
            self.page.goto(schema["url"], wait_until="domcontentloaded")
            current = self.read_schema()
            if fingerprint(current) != fingerprint(schema):
                raise error_type("Microsoft Forms đã thay đổi câu hỏi hoặc quy tắc. Hãy đọc lại form.")
            self._show_questions()
            seen, visited, signatures = set(), set(), set()
            for _ in range(len(schema["pages"]) + len(schema["fields"]) + 3):
                known_ids = {field[key] for field in schema["fields"] for key in ("ms_question_id", "ms_parent_id")}
                for card in self.page.locator('[data-automation-id="questionItem"]:visible').all():
                    rendered_ids = card.locator('[id^="QuestionId_"]').evaluate_all("els=>els.map(e=>e.id.slice(11))")
                    if any(identifier not in known_ids for identifier in rendered_ids):
                        raise error_type("Microsoft Forms xuất hiện câu hỏi ngoài cấu trúc đã đọc. Hãy đọc lại form; chưa bấm Gửi.")
                visible = [field for field in schema["fields"] if self.question_card(field, required=False) is not None]
                if not visible:
                    raise error_type("Không nhận diện được câu hỏi Microsoft Forms trên trang hiện tại; chưa bấm Gửi.")
                visited.update(field["page"] for field in visible)
                pending = [field for field in visible if field["id"] not in seen]
                if pending:
                    for field in pending:
                        if self.question_card(field, required=False) is not None:
                            self.fill_field(field, answers[field["id"]])
                            seen.add(field["id"])
                    self.page.wait_for_timeout(150)
                    continue
                next_button = self.page.locator('[data-automation-id="nextButton"]:visible')
                next_button = next_button.first if next_button.count() else self.button(("Tiếp", "Tiếp theo", "Next"))
                submit_button = self.page.locator('[data-automation-id="submitButton"]:visible')
                submit_button = submit_button.first if submit_button.count() else self.button(("Gửi", "Submit"))
                if next_button is None:
                    if submit_button is None:
                        raise error_type("Không tìm thấy nút Tiếp/Gửi Microsoft Forms. Kiểm tra đăng nhập hoặc CAPTCHA.")
                    expected = {field["id"] for field in schema["fields"]}
                    if not schema["branching"] and not expected <= seen:
                        raise error_type("Chưa điền đủ các câu hỏi qua tất cả các phần Microsoft Forms.")
                    self.report = {"pages": len(visited), "page_indices": sorted(visited),
                                   "questions": len({field["item_id"] for field in schema["fields"] if field["id"] in seen}),
                                   "fields": len(seen), "field_ids": sorted(seen)}
                    return self.report
                before = self._signature()
                if before in signatures:
                    raise error_type("Microsoft Forms tạo vòng lặp trang; chưa bấm Gửi.")
                signatures.add(before)
                next_button.click()
                try:
                    self.page.wait_for_function("""old => JSON.stringify(Array.from(document.querySelectorAll('[data-automation-id="questionItem"],[data-automation-id="sectionTitle"]')).filter(e=>e.getClientRects().length).map(e=>e.querySelector('[id^="QuestionId_"]')?.id||e.textContent?.trim().slice(0,250))) !== old""", arg=before, timeout=15000)
                except Exception as exc:
                    alerts = " ".join(self.page.get_by_role("alert").all_text_contents())
                    raise error_type("Microsoft Forms chưa chuyển sang phần tiếp theo. " + alerts) from exc
            raise error_type("Không xác định được trang cuối Microsoft Forms; chưa bấm Gửi.")

        def submit(self, schema):
            if not self.report:
                raise error_type("Chưa điền đủ Microsoft Forms.")
            buttons = self.page.locator('[data-automation-id="submitButton"]:visible')
            button = buttons.first if buttons.count() else self.button(("Gửi", "Submit"))
            if button is None:
                raise error_type("Không thấy nút Gửi Microsoft Forms.")
            def is_response(response):
                parts = urlsplit(response.url)
                return (response.request.method == "POST" and parts.hostname in MICROSOFT_HOSTS
                        and parts.path.casefold().startswith("/formapi/api/")
                        and bool(re.search(r"/responses/?$", parts.path, re.I)))
            try:
                with self.page.expect_response(is_response, timeout=45000) as pending:
                    button.click()
                response = pending.value
                result = response.json()
                identifier = _get(result, "id") or _get(_get(result, "response", {}), "id")
                if not response.ok or identifier is None or _get(result, "error"):
                    raise error_type("Microsoft chưa xác nhận mã phản hồi đã lưu.")
                self.page.wait_for_function("""() => {
                    const q=Array.from(document.querySelectorAll('[data-automation-id="questionItem"]')).some(e=>e.getClientRects().length);
                    const s=Array.from(document.querySelectorAll('[data-automation-id="submitButton"]')).some(e=>e.getClientRects().length);
                    const msg=document.querySelector('[data-automation-id="thankYouMessage"],[data-automation-id="thankYouMessageContainer"],[data-automation-id="thankYouPage"]');
                    return !q&&!s&&((msg&&msg.textContent.trim())||/submitted|recorded|câu trả lời.*(?:gửi|lưu)|phản hồi.*(?:gửi|lưu)/i.test(document.body.innerText));
                }""", timeout=25000)
                return True
            except Exception as exc:
                raise error_type("Chưa xác định Microsoft Forms đã lưu hay chưa. Không tự gửi lại. " + str(exc)) from exc

    MicrosoftFormsBrowser.__name__ = "MicrosoftFormsBrowser"
    return MicrosoftFormsBrowser

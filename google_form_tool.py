from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, date
import hashlib
import itertools
import json
import math
import os
from pathlib import Path
import random
import re
import secrets
import subprocess
import sys
import tempfile
import time
import unicodedata
from urllib.parse import urlsplit, urlunsplit

FORM_URL = ""  # DÁN LINK GOOGLE FORMS HOẶC MICROSOFT FORMS CỦA BẠN VÀO ĐÂY
APP_VERSION = "9.4"
WEB_SAMPLER_VERSION = "web-2"


# KEY CŨ: chỉ nhập sang SQLite một lần. Tạo key mới bằng --key-generate, không dán key thật vào code.
LEGACY_ACCESS_KEYS = {'khach-01': {'sha256': '79d5a508a85cae8564283f6fb0457d53fa1ffbd0a9cb42ddc73b6f545844ce94',
              'enabled': True},
 'khach-02': {'sha256': '3cbbe5efc2fbf819d8ad5da7e5a67579a7216d509285468de903e81c5b4630cb',
              'enabled': True},
 'khach-03': {'sha256': '538b9ad4bf70ee24058b2edd04249c79f132412f2ddfeaebfa1db4ffb83ec600',
              'enabled': True}}

# KHO KEY: đổi đường dẫn mặc định tại đây, hoặc đặt KD_KEY_DB_PATH / KD_KEY_EXPORT_DIR trong .env.
# data/access_keys.sqlite3 lưu mã băm + trạng thái key; private_keys/*.csv chứa KEY THẬT để bạn cấp cho khách.
KEY_DB_PATH = Path(__file__).resolve().parent / "data" / "access_keys.sqlite3"
KEY_EXPORT_DIR = Path(__file__).resolve().parent / "private_keys"

# HẠN MỨC MIỄN PHÍ: số lần gửi tối đa cho mỗi cookie trình duyệt, lưu trong CSDL.
FREE_LIMIT = 10


class FormError(Exception):
    pass


def normalized(text):
    return " ".join(unicodedata.normalize("NFKC", str(text or "")).split()).strip()


def plain(text):
    text = unicodedata.normalize("NFD", normalized(text).casefold().replace("đ", "d"))
    return "".join(char for char in text if unicodedata.category(char) != "Mn")


def get_at(value, index, default=None):
    return value[index] if isinstance(value, list) and len(value) > index else default


def form_link(value, resolved=False, allow_generic=False):
    value = value.strip()
    from microsoft_forms import is_microsoft_url, normalize_microsoft_url
    if is_microsoft_url(value):
        try:
            return normalize_microsoft_url(value, resolved=resolved)
        except ValueError as exc:
            raise FormError(str(exc)) from exc
    parts = urlsplit(value)
    if parts.scheme != "https" or parts.username or parts.password:
        raise FormError("Cần đường dẫn HTTPS của Google Forms hoặc Microsoft Forms.")
    host = parts.netloc.lower()
    if host in ("forms.gle", "forms.google.com") and not resolved:
        return value
    if host != "docs.google.com":
        if allow_generic:
            from html_forms import normalize_html_url
            try:
                return normalize_html_url(value, resolved=resolved)
            except ValueError as exc:
                raise FormError(str(exc)) from exc
        raise FormError("Chỉ hỗ trợ link Google Forms hoặc Microsoft Forms công khai.")
    match = re.fullmatch(r"/forms/(?:u/\d+/)?d/(?:e/)?[A-Za-z0-9_-]+/(viewform|formResponse|edit)/?", parts.path)
    if not match:
        raise FormError("Link không phải trang điền Google Form công khai.")
    path = re.sub(r"/(?:formResponse|edit)/?$", "/viewform", parts.path)
    return urlunsplit(("https", "docs.google.com", path.rstrip("/"), "", ""))


def parse_schema(data, url):
    if not isinstance(data, list) or not isinstance(get_at(data, 1), list):
        raise FormError("Không đọc được cấu trúc form; form có thể đóng hoặc yêu cầu đăng nhập.")
    form = data[1]
    fields, pages = [], [{"index": 0, "title": "Giới thiệu", "id": -1}]
    question_ids, branching = set(), False
    for item in get_at(form, 1, []) or []:
        item_type = get_at(item, 3)
        title = str(get_at(item, 1, "") or "").strip()
        if item_type == 8:
            action = get_at(item, 5)
            branching |= action not in (None, -2)
            pages.append({"index": len(pages), "title": title, "id": item[0]})
            continue
        if item_type in (6, 11, 12):
            continue
        if item_type == 13:
            raise FormError("Form có yêu cầu tải tệp: " + title + ". Cần xử lý thủ công.")
        if item_type not in (0, 1, 2, 3, 4, 5, 7, 9, 10, 18):
            raise FormError(f"Chưa hỗ trợ loại câu hỏi {item_type}: {title}")
        entries = get_at(item, 4, []) or []
        if not entries:
            raise FormError("Không đọc được mã trả lời của câu: " + title)
        question_ids.add(item[0])
        for row_index, entry in enumerate(entries):
            options, has_other, actions = [], False, {}
            for option in get_at(entry, 1, []) or []:
                if get_at(option, 4) == 1:
                    has_other = True
                    continue
                label = str(option[0])
                options.append(label)
                if get_at(option, 2) not in (None, -2):
                    actions[label] = option[2]
                    branching = True
            is_grid = item_type == 7
            multi_grid = is_grid and bool(get_at(get_at(entry, 11, []), 0, False))
            kind = {0: "text", 1: "paragraph", 2: "single", 3: "dropdown",
                    4: "multiple", 5: "scale", 7: "grid_multiple" if multi_grid else "grid",
                    9: "date", 10: "time", 18: "scale"}[item_type]
            field = {"id": f"entry.{entry[0]}", "item_id": item[0], "question": title,
                     "description": str(get_at(item, 2, "") or ""), "kind": kind,
                     "options": options, "required": bool(get_at(entry, 2, False)),
                     "other": has_other, "page": len(pages) - 1,
                     "row": (get_at(get_at(entry, 3, []), 0, "") if is_grid else None),
                     "row_index": row_index, "actions": actions,
                     "rules": get_at(entry, 4, []) or []}
            if kind in ("multiple", "grid_multiple"):
                field["minimum"] = 1
                field["maximum"] = max(1, len(options))
                for rule in field["rules"]:
                    if get_at(rule, 0) != 7 or get_at(rule, 1) not in (200, 201, 204):
                        raise FormError("Quy tắc chọn nhiều chưa hỗ trợ: " + title)
                    limit = int(rule[2][0])
                    if rule[1] in (200, 204):
                        field["minimum"] = max(1, limit)
                    if rule[1] in (201, 204):
                        field["maximum"] = limit
                if not 1 <= field["minimum"] <= field["maximum"] <= len(options):
                    raise FormError("Số lựa chọn không phù hợp để điền hết câu: " + title)
            grid_rules = get_at(item, 8, []) or []
            field["exclusive_columns"] = bool(is_grid and any(get_at(rule, 1) == 205 for rule in grid_rules))
            if kind == "date":
                flags = get_at(entry, 7, []) or []
                field["has_time"] = bool(get_at(flags, 0, False))
                field["has_year"] = bool(get_at(flags, 1, True))
            if kind == "time":
                field["duration"] = bool(get_at(get_at(entry, 6, []), 0, False))
            if kind not in ("text", "paragraph", "date", "time") and not options and not has_other:
                raise FormError("Câu hỏi không có lựa chọn: " + title)
            fields.append(field)
    ids = [field["id"] for field in fields]
    if not fields or len(set(ids)) != len(ids):
        raise FormError("Cấu trúc câu hỏi trống hoặc trùng mã.")
    return {"url": url, "provider": "google", "title": str(get_at(form, 8, "Google Form")),
            "fields": fields, "pages": pages, "question_count": len(question_ids),
            "branching": branching, "confirmation": str(get_at(get_at(form, 2, []), 0, "") or "")}


def fingerprint(schema):
    items = []
    for field in schema["fields"]:
        item = {key: field.get(key) for key in ("id", "item_id", "question", "kind", "page",
                "row", "required", "rules", "minimum", "maximum", "exclusive_columns")}
        item["options"] = sorted(field["options"])
        items.append(item)
    return hashlib.sha256(json.dumps(sorted(items, key=lambda x: x["id"]),
                                    sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def preference(field, rng):
    options = field["options"]
    if not options:
        return []
    labels = [plain(option) for option in options]
    scores = []
    for label in labels:
        if "khong dong y" in label or "disagree" in label:
            score = 1 if "hoan toan" in label or "strongly" in label else 2
        elif "dong y" in label or re.search(r"\bagree\b", label):
            score = 5 if "hoan toan" in label or "rat dong y" in label or "strongly" in label else 4
        elif any(word in label for word in ("binh thuong", "trung lap", "phan van", "neutral")):
            score = 3
        else:
            scores = []
            break
        scores.append(score)
    if len(options) == 5 and sorted(scores) == [1, 2, 3, 4, 5]:
        baseline = [12, 17, 25, 28, 18]
        return [(baseline[score - 1] + rng.uniform(-2, 2)) for score in scores]
    if len(scores) == len(options) and len(scores) >= 2:
        return [(0.55 + score * 0.17) * rng.uniform(0.95, 1.05) for score in scores]
    if field["kind"] == "scale":
        try:
            numbers = [float(option) for option in options]
        except ValueError:
            numbers = []
        if numbers and max(numbers) > min(numbers):
            low, high = min(numbers), max(numbers)
            return [math.exp(-0.5 * (((number - low) / (high - low) - 0.62) / 0.30) ** 2)
                    * rng.uniform(0.9, 1.1) for number in numbers]
    positive = [bool(re.match(r"^(co|yes)(\b|[,:])", label)) for label in labels]
    negative = [bool(re.match(r"^(khong|no)(\b|[,:])", label)) for label in labels]
    if any(positive) and any(negative):
        return [rng.uniform(1.45, 1.65) if yes else rng.uniform(0.9, 1.1) if no
                else rng.uniform(0.75, 1.0) for yes, no in zip(positive, negative)]
    return [rng.uniform(0.8, 1.2) for _ in options]


def rule_valid(value, rule):
    category, code = rule[:2]
    args = get_at(rule, 2, [])
    args = args if isinstance(args, list) else []
    try:
        if category == 1:
            number = float(value)
            a = float(args[0]) if args else 0
            b = float(args[1]) if len(args) > 1 else 0
            return math.isfinite(number) and {
                1: lambda: number > a, 2: lambda: number >= a,
                3: lambda: number < a, 4: lambda: number <= a,
                5: lambda: number == a, 6: lambda: number != a,
                7: lambda: a <= number <= b, 8: lambda: not a <= number <= b,
                9: lambda: True, 10: lambda: number.is_integer(),
            }[code]()
        if category == 2:
            return {100: lambda: str(args[0]) in value,
                    101: lambda: str(args[0]) not in value,
                    102: lambda: re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value) is not None,
                    103: lambda: value.startswith(("https://", "http://"))}[code]()
        if category == 6:
            return len(value) <= int(args[0]) if code == 202 else len(value) >= int(args[0]) if code == 203 else False
        if category == 4:
            match = re.search(str(args[0]), value) if code in (299, 300) else re.fullmatch(str(args[0]), value)
            return (match is not None) if code in (299, 301) else (match is None) if code in (300, 302) else False
    except (ValueError, KeyError, IndexError, TypeError, re.error):
        return False
    return False


_LIVE_SETTINGS_CACHE = {"stamp": None, "keys": None, "html": None}


def _read_web_template(path):
    try:
        html = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise FormError("Không đọc được giao diện web_ui.html. Kiểm tra file đã có và dùng mã UTF-8.") from exc
    if "<html" not in html.lower() or "</html>" not in html.lower() or "__APP_TOKEN__" not in html:
        raise FormError("Giao diện web_ui.html không hợp lệ hoặc thiếu mã phiên __APP_TOKEN__.")
    return html


def live_settings():
    import ast
    global _LIVE_SETTINGS_CACHE
    path = Path(__file__).resolve()
    template_path = path.with_name("web_ui.html")
    try:
        stat, template_stat = path.stat(), template_path.stat()
    except OSError as exc:
        raise FormError("Thiếu mã nguồn hoặc giao diện web_ui.html. Kiểm tra đầy đủ file trước khi chạy.") from exc
    stamp = (str(path), stat.st_mtime_ns, stat.st_size, template_stat.st_mtime_ns, template_stat.st_size)
    if _LIVE_SETTINGS_CACHE["stamp"] == stamp:
        return _LIVE_SETTINGS_CACHE
    try:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        values = {}
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "LEGACY_ACCESS_KEYS":
                        values[target.id] = ast.literal_eval(node.value)
        registry = values["LEGACY_ACCESS_KEYS"]
        if not isinstance(registry, dict):
            raise ValueError
        for name, item in registry.items():
            if (not isinstance(name, str) or not isinstance(item, dict)
                    or not re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256", "")))
                    or type(item.get("enabled")) is not bool):
                raise ValueError
        html = _read_web_template(template_path)
        _LIVE_SETTINGS_CACHE = {"stamp": stamp, "keys": registry, "html": html}
        return _LIVE_SETTINGS_CACHE
    except (OSError, UnicodeError, ValueError, KeyError, SyntaxError, TypeError) as exc:
        raise FormError("Cấu hình key cũ không hợp lệ. Kiểm tra lại mã trước khi sử dụng.") from exc


def normalize_access_key(value):
    value = unicodedata.normalize("NFKC", str(value))
    value = value.translate(str.maketrans({char: "-" for char in "‐‑‒–—―−﹣－"}))
    value = "".join(char for char in value if not char.isspace() and unicodedata.category(char) != "Cf").upper()
    match = re.search(r"KD-?([0-9A-F]{8})-?([0-9A-F]{8})-?([0-9A-F]{8})-?([0-9A-F]{8})(?![0-9A-F])", value)
    return "KD-" + "-".join(match.groups()) if match else value


def access_key_hash(value):
    return hashlib.sha256(normalize_access_key(value).encode("utf-8")).hexdigest()


def create_access_key():
    raw = secrets.token_hex(16).upper()
    return "KD-" + "-".join(raw[index:index + 8] for index in range(0, 32, 8))


class KeyAlreadyUsed(FormError):
    pass


class QuotaExceeded(FormError):
    pass


def load_local_key_environment():
    path = Path(__file__).resolve().parent / ".env"
    if not path.is_file():
        return
    allowed = {"KD_DATABASE_URL", "KD_KEY_DB_PATH", "KD_KEY_EXPORT_DIR"}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name, value = name.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if name in allowed:
            os.environ.setdefault(name, value)


class KeyStore:
    def __init__(self, path=None, database_url=None, export_dir=None, legacy=None):
        import threading
        self.database_url = database_url or ""
        if self.database_url and urlsplit(self.database_url).scheme not in ("postgres", "postgresql"):
            raise FormError("KD_DATABASE_URL cần là chuỗi kết nối PostgreSQL.")
        self.path = Path(path or os.environ.get("KD_KEY_DB_PATH") or KEY_DB_PATH).expanduser().resolve()
        self.export_dir = Path(export_dir or os.environ.get("KD_KEY_EXPORT_DIR") or KEY_EXPORT_DIR).expanduser().resolve()
        self.cache_lock = threading.RLock()
        self.cache = {}
        self.guest_cache = {}
        self._closed = False
        self._pool = None
        if self.database_url:
            from database_pool import ConnectionPool
            database_url = self.database_url
            def open_postgres():
                try:
                    import psycopg
                    from psycopg.rows import dict_row
                except ImportError as exc:
                    raise FormError("Cài thư viện PostgreSQL: python -m pip install -r requirements.txt") from exc
                return psycopg.connect(database_url, connect_timeout=15,
                                       tcp_user_timeout=15000, prepare_threshold=None, row_factory=dict_row)
            self._pool = ConnectionPool(open_postgres, max_size=4)
        try:
            self.initialize(legacy or {})
        except BaseException:
            self.close()
            raise

    def close(self):
        self._closed = True
        if self._pool is not None:
            self._pool.close()

    def connection(self):
        from contextlib import contextmanager

        @contextmanager
        def connect():
            connection = None
            broken = False
            try:
                if self.database_url:
                    connection = self._pool.acquire()
                else:
                    import sqlite3
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    connection = sqlite3.connect(self.path, timeout=30)
                    connection.row_factory = sqlite3.Row
                    os.chmod(self.path, 0o600)
                yield connection
                try:
                    connection.commit()
                except BaseException:
                    broken = True
                    raise
            except FormError:
                if connection is not None:
                    try:
                        connection.rollback()
                    except BaseException:
                        broken = True
                raise
            except Exception as exc:
                if connection is not None:
                    try:
                        connection.rollback()
                    except BaseException:
                        broken = True
                raise FormError("Không truy cập được kho key. Kiểm tra đường dẫn hoặc kết nối CSDL; thao tác đã dừng.") from exc
            except BaseException:
                if connection is not None:
                    try:
                        connection.rollback()
                    except BaseException:
                        broken = True
                raise
            finally:
                if connection is not None:
                    if self._pool is not None:
                        self._pool.release(connection, broken=broken)
                    else:
                        connection.close()

        return connect()

    def execute(self, connection, sql, parameters=()):
        return connection.execute(sql.replace("?", "%s") if self.database_url else sql, parameters)

    def initialize(self, legacy):
        with self.connection() as connection:
            if not self.database_url:
                self.execute(connection, "BEGIN IMMEDIATE")
            self.execute(connection, """CREATE TABLE IF NOT EXISTS kd_access_keys (
                name TEXT PRIMARY KEY, sha256 TEXT NOT NULL UNIQUE, enabled INTEGER NOT NULL,
                created_at DOUBLE PRECISION NOT NULL, claimed_at DOUBLE PRECISION,
                claimed_job TEXT, finished_at DOUBLE PRECISION, outcome TEXT,
                activated_at DOUBLE PRECISION, activation_id TEXT
            )""")
            if self.database_url:
                self.execute(connection, "ALTER TABLE kd_access_keys ADD COLUMN IF NOT EXISTS activated_at DOUBLE PRECISION")
                self.execute(connection, "ALTER TABLE kd_access_keys ADD COLUMN IF NOT EXISTS activation_id TEXT")
            else:
                columns = {row["name"] for row in self.execute(connection, "PRAGMA table_info(kd_access_keys)").fetchall()}
                if "activated_at" not in columns:
                    self.execute(connection, "ALTER TABLE kd_access_keys ADD COLUMN activated_at DOUBLE PRECISION")
                if "activation_id" not in columns:
                    self.execute(connection, "ALTER TABLE kd_access_keys ADD COLUMN activation_id TEXT")
            if self.database_url:
                self.execute(connection, "ALTER TABLE kd_access_keys ADD COLUMN IF NOT EXISTS unlimited INTEGER NOT NULL DEFAULT 0")
            elif "unlimited" not in columns:
                self.execute(connection, "ALTER TABLE kd_access_keys ADD COLUMN unlimited INTEGER NOT NULL DEFAULT 0")
            self.execute(connection, """CREATE TABLE IF NOT EXISTS kd_guest_usage (
                guest_id TEXT PRIMARY KEY, used INTEGER NOT NULL DEFAULT 0,
                created_at DOUBLE PRECISION NOT NULL
            )""")
            self.execute(connection, "CREATE TABLE IF NOT EXISTS kd_key_meta (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
            inserted = self.execute(connection,
                "INSERT INTO kd_key_meta (name, value) VALUES (?, ?) ON CONFLICT (name) DO NOTHING",
                ("legacy_imported_v1", "1")).rowcount
            if inserted:
                for name, item in legacy.items():
                    self.execute(connection,
                        "INSERT INTO kd_access_keys (name, sha256, enabled, created_at) VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
                        (name, item["sha256"], int(item["enabled"]), time.time()))
            migrated = self.execute(connection,
                "INSERT INTO kd_key_meta (name, value) VALUES (?, ?) ON CONFLICT (name) DO NOTHING",
                ("unlimited_keys_v1", "1")).rowcount
            if migrated:
                self.execute(connection, "UPDATE kd_access_keys SET unlimited = 1 WHERE activated_at IS NULL AND claimed_job IS NULL")

    def description(self):
        return "PostgreSQL (biến KD_DATABASE_URL)" if self.database_url else str(self.path)

    def clear_cache(self):
        with self.cache_lock:
            self.cache.clear()
            self.guest_cache.clear()

    def get(self, name):
        with self.cache_lock:
            cached = self.cache.get(name)
            if cached and cached[0] > time.monotonic():
                return dict(cached[1]) if cached[1] else None
        with self.connection() as connection:
            row = self.execute(connection, "SELECT * FROM kd_access_keys WHERE name = ?", (name,)).fetchone()
        result = dict(row) if row else None
        with self.cache_lock:
            self.cache[name] = (time.monotonic() + 1, result)
        return dict(result) if result else None

    def unused_by_hash(self, digest):
        with self.connection() as connection:
            row = self.execute(connection,
                "SELECT * FROM kd_access_keys WHERE sha256 = ? AND enabled = 1 AND unlimited = 1",
                (digest,)).fetchone()
        return dict(row) if row else None

    def list(self):
        with self.connection() as connection:
            return [dict(row) for row in self.execute(connection, "SELECT * FROM kd_access_keys ORDER BY created_at, name").fetchall()]

    def issue(self, names, rotate=False):
        import csv
        names = list(names)
        if not names or len(set(names)) != len(names):
            raise FormError("Danh sách tên key bị trống hoặc trùng.")
        if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,59}", name) for name in names):
            raise FormError("Tên key dài 1–60 ký tự, gồm chữ không dấu, số, dấu gạch nối, gạch dưới hoặc dấu chấm.")
        records = [{"name": name, "key": create_access_key(), "created_at": time.time()} for name in names]
        self.export_dir.mkdir(parents=True, exist_ok=True)
        export_path = self.export_dir / (datetime.now().strftime("keys_%Y%m%d_%H%M%S_") + secrets.token_hex(4) + ".csv")
        with self.connection() as connection:
            for record in records:
                if rotate:
                    cursor = self.execute(connection, """UPDATE kd_access_keys SET sha256 = ?, enabled = 1,
                        created_at = ?, claimed_at = NULL, claimed_job = NULL, finished_at = NULL, outcome = NULL,
                        activated_at = NULL, activation_id = NULL, unlimited = 1
                        WHERE name = ?""", (access_key_hash(record["key"]), record["created_at"], record["name"]))
                else:
                    cursor = self.execute(connection,
                        "INSERT INTO kd_access_keys (name, sha256, enabled, created_at, unlimited) VALUES (?, ?, 1, ?, 1) ON CONFLICT DO NOTHING",
                        (record["name"], access_key_hash(record["key"]), record["created_at"]))
                if cursor.rowcount != 1:
                    raise FormError("Không tạo/đổi được tên key: " + record["name"] + ". Kiểm tra tên đã tồn tại hoặc đã bị xóa.")
            descriptor = os.open(export_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=("name", "key", "created_at"))
                writer.writeheader()
                for record in records:
                    writer.writerow({**record, "created_at": datetime.fromtimestamp(record["created_at"]).astimezone().isoformat(timespec="seconds")})
        self.clear_cache()
        return records, export_path

    def change(self, name, enabled=None, delete=False):
        with self.connection() as connection:
            if delete:
                cursor = self.execute(connection, "DELETE FROM kd_access_keys WHERE name = ?", (name,))
            else:
                cursor = self.execute(connection, "UPDATE kd_access_keys SET enabled = ? WHERE name = ?", (int(enabled), name))
            if cursor.rowcount != 1:
                raise FormError("Không tìm thấy tên key: " + name)
        self.clear_cache()

    def activate(self, digest, activation_id):
        # KEY UNLIMITED: đăng nhập lại được; khóa/xóa/đổi key sẽ thu hồi quyền sử dụng.
        with self.connection() as connection:
            cursor = self.execute(connection, """UPDATE kd_access_keys
                SET activated_at = COALESCE(activated_at, ?), activation_id = ?, outcome = 'activated'
                WHERE sha256 = ? AND enabled = 1 AND unlimited = 1""", (time.time(), activation_id, digest))
            row = self.execute(connection, "SELECT * FROM kd_access_keys WHERE sha256 = ?", (digest,)).fetchone() if cursor.rowcount == 1 else None
        self.clear_cache()
        return dict(row) if row else None

    def claim(self, name, digest, job_id, activation_id):
        with self.connection() as connection:
            cursor = self.execute(connection, """UPDATE kd_access_keys SET claimed_at = ?, claimed_job = ?, outcome = 'running'
                WHERE name = ? AND sha256 = ? AND enabled = 1 AND unlimited = 1""",
                (time.time(), job_id, name, digest))
            if cursor.rowcount != 1:
                raise KeyAlreadyUsed("Key đã bị khóa, đã thay đổi hoặc là key một lần của phiên bản cũ.")
        self.clear_cache()

    def ensure_guest(self, guest_id):
        with self.connection() as connection:
            self.execute(connection, """INSERT INTO kd_guest_usage (guest_id, used, created_at)
                VALUES (?, 0, ?) ON CONFLICT (guest_id) DO NOTHING""", (guest_id, time.time()))
        with self.cache_lock:
            self.guest_cache.pop(guest_id, None)
        return self.guest_usage(guest_id, fresh=True)

    def guest_usage(self, guest_id, fresh=False):
        started = time.monotonic()
        if not fresh:
            with self.cache_lock:
                cached = self.guest_cache.get(guest_id)
                if cached and cached[0] > started:
                    return dict(cached[1]) if cached[1] is not None else None
        with self.connection() as connection:
            row = self.execute(connection, "SELECT used FROM kd_guest_usage WHERE guest_id = ?", (guest_id,)).fetchone()
        result = None if row is None else {"limit": FREE_LIMIT, "used": row["used"], "remaining": max(0, FREE_LIMIT - row["used"])}
        with self.cache_lock:
            cached = self.guest_cache.get(guest_id)
            # Do not overwrite a quota update that committed while this read was in flight.
            if cached and cached[2] > started:
                return dict(cached[1]) if cached[1] is not None else None
            now = time.monotonic()
            if guest_id not in self.guest_cache and len(self.guest_cache) >= 2048:
                self.guest_cache.pop(next(iter(self.guest_cache)))
            self.guest_cache[guest_id] = (now + 1, result, now)
        return dict(result) if result is not None else None

    def consume_guest(self, guest_id):
        # TRỪ LƯỢT: ghi bền vững NGAY TRƯỚC khi bấm Gửi; lỗi xác nhận vẫn tính một lần gửi.
        with self.connection() as connection:
            row = self.execute(connection, "UPDATE kd_guest_usage SET used = used + 1 WHERE guest_id = ? AND used < ? RETURNING used",
                               (guest_id, FREE_LIMIT)).fetchone()
            if row is None:
                raise QuotaExceeded("Bạn đã dùng hết 10 lượt miễn phí. Nhập key được cấp để tiếp tục.")
        result = {"limit": FREE_LIMIT, "used": row["used"], "remaining": max(0, FREE_LIMIT - row["used"])}
        with self.cache_lock:
            cached = self.guest_cache.get(guest_id)
            if not cached or cached[1] is None or cached[1]["used"] <= result["used"]:
                now = time.monotonic()
                if guest_id not in self.guest_cache and len(self.guest_cache) >= 2048:
                    self.guest_cache.pop(next(iter(self.guest_cache)))
                self.guest_cache[guest_id] = (now + 1, dict(result), now)
        return result

    def release_guest(self, guest_id):
        # Worker-only: return a reserved slot only when no submit attempt began.
        with self.connection() as connection:
            row = self.execute(connection,
                               "UPDATE kd_guest_usage SET used = used - 1 WHERE guest_id = ? AND used > 0 RETURNING used",
                               (guest_id,)).fetchone()
        result = None if row is None else {"limit": FREE_LIMIT, "used": row["used"], "remaining": max(0, FREE_LIMIT - row["used"])}
        with self.cache_lock:
            if result is None:
                self.guest_cache.pop(guest_id, None)
            else:
                now = time.monotonic()
                if guest_id not in self.guest_cache and len(self.guest_cache) >= 2048:
                    self.guest_cache.pop(next(iter(self.guest_cache)))
                self.guest_cache[guest_id] = (now + 1, dict(result), now)
        return result

    def finish(self, name, digest, job_id, outcome):
        with self.connection() as connection:
            self.execute(connection, """UPDATE kd_access_keys SET finished_at = ?, outcome = ?
                WHERE name = ? AND sha256 = ? AND claimed_job = ?""",
                (time.time(), outcome, name, digest, job_id))
        self.clear_cache()


_KEY_STORE_CACHE = {"config": None, "store": None}


def configured_key_store():
    cloud = os.environ.get("KD_CLOUD", "").strip().lower() in ("1", "true", "yes") or os.environ.get("RENDER") == "true"
    database_url = os.environ.get("KD_DATABASE_URL", "").strip()
    if cloud and not database_url:
        raise FormError("Cloud cần KD_DATABASE_URL trỏ tới PostgreSQL để nhớ key đã dùng qua các lần restart/deploy. Xem README.")
    config = (database_url, os.environ.get("KD_KEY_DB_PATH"), os.environ.get("KD_KEY_EXPORT_DIR"))
    if (_KEY_STORE_CACHE["config"] != config or _KEY_STORE_CACHE["store"] is None
            or _KEY_STORE_CACHE["store"]._closed):
        store = KeyStore(database_url=database_url, legacy={} if database_url else live_settings()["keys"])
        _KEY_STORE_CACHE.update(config=config, store=store)
    return _KEY_STORE_CACHE["store"]


def manage_access_keys(args):
    store = configured_key_store()
    print("Kho key: " + store.description())
    if args.key_paths:
        print("Nơi lưu key thật (CSV riêng tư): " + str(store.export_dir))
        return 0
    if args.key_list:
        rows = store.list()
        for item in rows:
            status = "KHÓA" if not item["enabled"] else "UNLIMITED" if item["unlimited"] else "KEY CŨ ĐÃ DÙNG"
            print(f'{item["name"]}: {status}' + (f' · {item["outcome"]}' if item["outcome"] else ""))
        if not rows:
            print("Chưa có key. Tạo bằng --key-generate 1.")
        return 0
    if args.key_generate is not None:
        if args.key_generate <= 0:
            raise FormError("--key-generate cần số lượng lớn hơn 0.")
        prefix = datetime.now().strftime("key-%Y%m%d-%H%M%S-") + secrets.token_hex(3)
        names = [f"{prefix}-{index + 1:03d}" for index in range(args.key_generate)]
        records, export_path = store.issue(names)
    elif args.key_add or args.key_rotate:
        name = (args.key_add or args.key_rotate).strip()
        records, export_path = store.issue([name], rotate=bool(args.key_rotate))
    else:
        name = (args.key_delete or args.key_disable or args.key_enable).strip()
        store.change(name, delete=bool(args.key_delete), enabled=bool(args.key_enable))
        print("Đã cập nhật: " + name + ". Key unlimited dùng lại được khi đang mở khóa.")
        return 0
    for record in records:
        print(f'Tên: {record["name"]}\nKEY: {record["key"]}')
    print("Đã lưu bản key thật tại: " + str(export_path))
    print("CSDL chỉ lưu mã băm và trạng thái dùng. Key mới có hiệu lực ngay trên máy chủ dùng cùng CSDL.")
    return 0


class AccessSessions:
    def __init__(self, key_store=None):
        import threading
        self.key_store = key_store or configured_key_store()
        self.lock = threading.RLock()
        self.sessions = {}
        self.attempts = {}

    def login(self, key, client):
        now = time.time()
        with self.lock:
            attempts = [moment for moment in self.attempts.get(client, []) if now - moment < 60]
            self.attempts[client] = attempts
            if len(attempts) >= 6:
                return None, None, max(1, math.ceil(60 - (now - attempts[0])))
        supplied = access_key_hash(key)
        token = secrets.token_urlsafe(32)
        activation_id = hashlib.sha256(token.encode("utf-8")).hexdigest()
        item = self.key_store.activate(supplied, activation_id)
        if item is None:
            with self.lock:
                self.attempts.setdefault(client, []).append(now)
            return None, None, 0
        record = {"key_id": item["name"], "key_hash": supplied, "activation_id": activation_id, "expires": now + 12 * 3600}
        with self.lock:
            self.attempts.pop(client, None)
            self.sessions[token] = record
            self.sessions = {sid: value for sid, value in self.sessions.items() if value["expires"] > now}
        return token, record, 0

    def current(self, cookie_header):
        from http.cookies import SimpleCookie, CookieError
        try:
            cookie = SimpleCookie()
            cookie.load(cookie_header or "")
            token = cookie["kd_access"].value if "kd_access" in cookie else ""
        except CookieError:
            return None
        with self.lock:
            record = self.sessions.get(token)
        if not record:
            return None
        item = self.key_store.get(record["key_id"])
        valid = (record["expires"] > time.time() and item and item["enabled"] and item["unlimited"]
                 and secrets.compare_digest(item["sha256"], record["key_hash"]))
        if not valid:
            with self.lock:
                self.sessions.pop(token, None)
            return None
        return {**record, "key_used": False, "key_job_id": item["claimed_job"]}

    def logout(self, cookie_header):
        from http.cookies import SimpleCookie, CookieError
        try:
            cookie = SimpleCookie()
            cookie.load(cookie_header or "")
            if "kd_access" in cookie:
                with self.lock:
                    self.sessions.pop(cookie["kd_access"].value, None)
        except CookieError:
            pass


def active_job_key(job, key_store=None):
    store = key_store or configured_key_store()
    name = job.get("access_key_id")
    if not name:
        return bool(job.get("guest_id") and store.guest_usage(job["guest_id"]) is not None)
    item = store.get(name)
    return bool(item and item["enabled"] and item["unlimited"]
                and secrets.compare_digest(item["sha256"], job["access_key_hash"]))



def question_text(field):
    return re.sub(r"[^a-z0-9]+", " ", plain(field["question"])).strip()


def text_intent(field):
    label = question_text(field)
    if any(word in label for word in ("de xuat", "gop y", "giai phap", "kien nghi", "vi sao", "tai sao",
                                      "ly do", "danh gia", "cam nhan", "kho khan", "rao can", "han che",
                                      "tro ngai", "y kien", "mong muon", "nhan xet", "nghi gi", "quan diem",
                                      "suggest", "recommend", "why", "feedback", "opinion", "challenge")):
        return "open"
    if any(word in label for word in ("email", "e mail", "gmail")):
        return "email"
    if any(word in label for word in ("dien thoai", "phone", "mobile", "sdt")):
        return "phone"
    if any(word in label for word in ("ngay sinh", "ngay thang nam sinh", "date of birth",
                                      "birth date", "birthday", "sinh ngay", "when were you born")) or re.search(r"\bdob\b", label):
        return "birth_date"
    if any(word in label for word in ("nam sinh", "sinh nam", "nam ban sinh", "birth year")):
        return "birth_year"
    if any(word in label for word in ("noi sinh", "sinh ra o dau", "place of birth", "birthplace")) and "sinh song" not in label:
        return "birthplace"
    if any(word in label for word in ("que quan", "nguyen quan", "hometown", "home town", "den tu dau")):
        return "hometown"
    if any(word in label for word in ("ho khau", "thuong tru", "permanent address")):
        return "permanent_address"
    if any(word in label for word in ("dia chi", "noi o", "noi cu tru", "sinh song", "address", "dang song", "song o dau", "dang o dau")):
        return "address"
    if any(word in label for word in ("thanh pho", "tinh thanh", "city", "province")):
        return "city"
    if "quoc tich" in label or "nationality" in label:
        return "nationality"
    if "dan toc" in label or "ethnicity" in label:
        return "ethnicity"
    if "ton giao" in label or "religion" in label:
        return "religion"
    if any(word in label for word in ("gioi tinh", "gender", "sex")):
        return "gender"
    if re.search(r"\b(tuoi|age)\b", label):
        return "age"
    if any(word in label for word in ("ma sinh vien", "mssv", "student id", "student code")):
        return "student_id"
    if any(word in label for word in ("nam may", "nam hoc", "nam thu", "year of study", "student year")):
        return "student_year"
    if any(word in label for word in ("ten truong", "truong nao", "dai hoc nao", "hoc vien nao",
                                      "university", "school", "noi hoc", "truong ban")):
        return "school"
    if any(word in label for word in ("chuyen nganh", "nganh hoc", "major", "hoc nganh", "khoa nao", "thuoc khoa", "ten khoa")):
        return "faculty"
    if any(word in label for word in ("nghe nghiep", "cong viec", "occupation", "profession")):
        return "occupation"
    if any(word in label for word in ("ho va ten", "ho ten", "ten day du", "full name", "fullname", "your name", "ten cua ban", "ten la gi", "ten nguoi tra loi", "ten nguoi tham gia")) or label in ("name", "ten cua ban"):
        return "full_name"
    if any(word in label for word in ("ten dem", "ten lot", "middle name")):
        return "middle_name"
    if label in ("ho", "ho cua ban", "surname", "last name", "family name"):
        return "surname"
    if label in ("ten", "ten goi", "first name", "given name"):
        return "given_name"
    return "open"


def profile_template(schema):
    context = plain(schema.get("title", "") + " " + " ".join(field["question"] for field in schema["fields"]))
    age_ranges, choices = [], []
    for field in schema["fields"]:
        intent = text_intent(field)
        if intent == "age":
            numeric = [rule for rule in field.get("rules", []) if rule[0] == 1]
            if numeric:
                age_ranges.append(tuple(candidate for candidate in range(12, 81)
                                        if all(rule_valid(str(candidate), rule) for rule in numeric)))
        if intent in ("school", "faculty") and field["kind"] in ("single", "dropdown") and field["options"]:
            choices.append((intent, field["options"]))
    return context, tuple(age_ranges), tuple(choices)


def make_profile(schema, rng, index, reference_date, *, gender=None, prepared=None):
    context, age_ranges, choices = prepared if prepared is not None else profile_template(schema)
    student = any(word in context for word in ("sinh vien", "hoc tap", "student", "dai hoc", "hoc vien"))
    student_year = rng.randint(1, 4)
    age = 17 + student_year + rng.randrange(2) if student else rng.randint(20, 35)
    for candidates in age_ranges:
        if candidates and age not in candidates:
            age = rng.choice(candidates)
    sampled_gender = rng.choice(["Nam", "Nữ"])
    gender = sampled_gender if gender is None else gender
    surname = rng.choices(
        ["Nguyễn", "Trần", "Lê", "Phạm", "Hoàng", "Huỳnh", "Phan", "Vũ",
         "Võ", "Đặng", "Bùi", "Đỗ", "Hồ", "Ngô", "Dương", "Lý"],
        weights=[28, 13, 10, 8, 6, 5, 5, 4, 4, 4, 3, 3, 2, 2, 2, 1])[0]
    male = ["Minh Anh", "Quang Huy", "Đức Anh", "Tuấn Anh", "Gia Bảo", "Minh Khang",
            "Hoàng Nam", "Quốc Bảo", "Đức Minh", "Văn An", "Hữu Phúc", "Nhật Minh",
            "Thanh Tùng", "Quang Khải", "Xuân Trường", "Trung Kiên", "Tiến Đạt", "Duy Khánh"]
    female = ["Ngọc Anh", "Thu Hà", "Phương Linh", "Bảo Ngọc", "Minh Châu", "Quỳnh Mai",
              "Thảo Nguyên", "Khánh Linh", "Hoài An", "Ngọc Hân", "Thanh Huyền", "Thùy Dương",
              "Hồng Nhung", "Diệu Linh", "Phương Anh", "Mai Chi", "Thị Lan", "Hải Yến"]
    full_name = surname + " " + rng.choice(male if gender == "Nam" else female)
    month, day = rng.randint(1, 12), rng.randint(1, 28)
    birth_year = reference_date.year - age - int((month, day) > (reference_date.month, reference_date.day))
    cities = ["Hà Nội", "TP. Hồ Chí Minh", "Đà Nẵng", "Hải Phòng", "Cần Thơ", "Huế",
              "Nha Trang", "Đà Lạt", "Vinh", "Quy Nhơn", "Buôn Ma Thuột", "Thái Nguyên", "Bắc Ninh"]
    hometown = rng.choice(cities)
    birthplace = hometown if rng.random() < .8 else rng.choice(cities)
    current_city = ("Hà Nội" if "ha noi" in context else "TP. Hồ Chí Minh"
                    if any(word in context for word in ("ho chi minh", "tphcm", "tp hcm"))
                    else "Hà Nội" if student else rng.choice(cities))
    areas = {
        "Hà Nội": ["Cầu Giấy", "Thanh Xuân", "Đống Đa", "Hai Bà Trưng", "Hoàng Mai"],
        "TP. Hồ Chí Minh": ["Thủ Đức", "Bình Thạnh", "Tân Bình", "Gò Vấp", "Bình Tân"],
        "Đà Nẵng": ["Hải Châu", "Thanh Khê", "Sơn Trà", "Liên Chiểu"],
    }
    area = rng.choice(areas.get(current_city, ["trung tâm thành phố"]))
    school = "Đại học Giao thông Vận tải"
    for name in ("Học viện Tài chính", "Đại học Giao thông Vận tải", "Đại học Kinh tế Quốc dân",
                 "Đại học Thương mại", "Đại học Hà Nội", "Đại học Công nghiệp Hà Nội"):
        if plain(name) in context:
            school = name
            break
    profile = {
        "full_name": full_name, "surname": surname, "given_name": full_name.split()[-1],
        "middle_name": " ".join(full_name.split()[1:-1]), "gender": gender, "age": age,
        "birth_date": {"year": birth_year, "month": month, "day": day,
                       "hour": rng.randrange(24), "minute": rng.randrange(60)},
        "birthplace": birthplace, "hometown": hometown, "current_city": current_city,
        "address": f"Khu vực {area}, {current_city}", "permanent_address": hometown,
        "email": re.sub(r"[^a-z0-9]", "", plain(full_name)) + str(birth_year) + "@example.com",
        "phone": "090000" + str(rng.randrange(10000)).zfill(4),
        "nationality": "Việt Nam", "ethnicity": "Kinh", "religion": "Không",
        "student_year": student_year, "school": school,
        "faculty": rng.choice(["Kế toán", "Kinh tế", "Quản trị kinh doanh", "Công nghệ thông tin"]),
        "student_id": str(reference_date.year - student_year + 1)[-2:] + str(rng.randrange(10000000)).zfill(7),
        "occupation": "Sinh viên" if student else rng.choice(["Nhân viên văn phòng", "Kỹ thuật viên", "Kinh doanh tự do"]),
        "frequency": rng.choice(["khoảng 3–4 lần mỗi tuần", "hầu như mỗi ngày", "khoảng 2–3 lần mỗi tuần"]),
        "travel_minutes": rng.choice(["25–35 phút", "30–45 phút", "40–50 phút"]),
        "monthly_budget": rng.choice([150000, 200000, 250000, 300000]),
    }
    for intent, options in choices:
        profile[intent] = rng.choice(options)
    return profile


def coherent_choice(field, profile):
    intent = text_intent(field)
    options = field["options"]
    if intent in ("school", "faculty") and profile[intent] in options:
        return profile[intent]
    for option in options:
        label = re.sub(r"[^a-z0-9]+", " ", plain(option)).strip()
        if intent == "gender" and label in ({"nam", "male", "man"} if profile["gender"] == "Nam" else {"nu", "female", "woman"}):
            return option
        if intent == "student_year":
            year = profile["student_year"]
            words = {1: ("nhat", "mot", "first"), 2: ("hai", "second"),
                     3: ("ba", "third"), 4: ("tu", "bon", "fourth")}[year]
            if label == str(year) or re.search(r"\b" + str(year) + r"\b", label) or any(
                    re.search(r"\b" + word + r"\b", label) for word in words):
                return option
        if intent == "birth_year" and str(profile["birth_date"]["year"]) in label:
            return option
        if intent == "age":
            numbers = [int(number) for number in re.findall(r"\d+", label)]
            age = profile["age"]
            if len(numbers) == 1 and ((any(word in label for word in ("duoi", "under")) and age < numbers[0])
                                     or (any(word in label for word in ("tren", "over", "tu")) and age >= numbers[0])
                                     or label == str(age)):
                return option
            if len(numbers) >= 2 and numbers[0] <= age <= numbers[1]:
                return option
    return None


def natural_answer(field, profile, rng):
    label = question_text(field)
    context = plain(field["question"] + " " + field.get("description", ""))
    transit = any(word in context for word in ("giao thong", "cong cong", "ptgtc", "xe buyt", "tau dien", "bus", "di chuyen"))
    digital = any(word in context for word in ("cong cu so", "ky nang so", "cong nghe", "phan mem", "ung dung", "tri tue", "ai ", "hoc tap", "nghien cuu"))
    proposal = any(word in label for word in ("de xuat", "gop y", "giai phap", "cai thien", "mong muon", "kien nghi", "recommend", "suggest"))
    difficulty = any(word in label for word in ("kho khan", "rao can", "han che", "tro ngai", "bat tien", "problem", "challenge"))
    reason = any(word in label for word in ("vi sao", "tai sao", "ly do", "nguyen nhan", "why", "reason"))
    frequency = any(word in label for word in ("tan suat", "bao nhieu lan", "thuong xuyen", "how often"))
    if frequency:
        return f"Tôi sử dụng {profile['frequency']}, chủ yếu vào những lúc cần thiết."
    if any(word in label for word in ("bao lau", "thoi gian trung binh", "mat bao nhieu phut")):
        return f"Thông thường tôi mất khoảng {profile['travel_minutes']} cho mỗi lần di chuyển."
    if any(word in label for word in ("bao nhieu tien", "chi phi hang thang", "ngan sach", "monthly cost")):
        return f"Khoảng {profile['monthly_budget']:,} đồng mỗi tháng, tùy vào nhu cầu thực tế.".replace(",", ".")
    if proposal:
        choices = (
            ["Tôi mong có thêm tuyến kết nối gần trường, thông tin giờ xe rõ ràng và ít phải chờ vào giờ cao điểm.",
             "Theo tôi, nên cải thiện độ đúng giờ và kết nối giữa xe buýt với tàu điện; giá vé sinh viên hiện khá phù hợp.",
             "Tôi đề xuất cập nhật lịch trình chính xác hơn trên ứng dụng và cải thiện mái che, ghế ngồi ở trạm dừng."] if transit else
            ["Tôi mong nhà trường tổ chức thêm các buổi thực hành công cụ số, hướng dẫn kiểm tra nguồn tin và sử dụng AI có trách nhiệm.",
             "Theo tôi, nên có tài liệu hướng dẫn ngắn, ví dụ dễ làm theo và tài khoản hỗ trợ sinh viên cho những phần mềm cần thiết.",
             "Tôi đề xuất lồng ghép kỹ năng tìm tài liệu, xử lý số liệu và làm việc nhóm trực tuyến vào các học phần thực tế."] if digital else
            ["Tôi mong thông tin hướng dẫn rõ ràng hơn, các bước thực hiện đơn giản và có kênh hỗ trợ khi gặp vướng mắc.",
             "Theo tôi, nên ưu tiên tính thuận tiện, chi phí hợp lý và tiếp nhận góp ý của người dùng thường xuyên.",
             "Tôi đề xuất cải thiện những khâu còn mất thời gian và hướng dẫn bằng ví dụ cụ thể để mọi người dễ áp dụng."]
        )
    elif difficulty:
        choices = (
            ["Tôi thấy bất tiện nhất là thời gian chờ và tình trạng đông người vào giờ cao điểm, nhất là khi cần đến lớp đúng giờ.",
             "Một số trạm dừng còn xa chỗ ở nên tôi phải đi bộ hoặc đổi tuyến; lịch trình trên ứng dụng đôi lúc cũng chưa rõ."] if transit else
            ["Tôi thường mất thời gian kiểm tra độ tin cậy của tài liệu và lựa chọn công cụ phù hợp với bài học.",
             "Khó khăn của tôi là chưa dùng thành thạo một số tính năng; các phần mềm trả phí cũng cần cân nhắc về chi phí."] if digital else
            ["Tôi đôi lúc chưa nắm rõ hướng dẫn nên phải tìm hiểu thêm, và việc xử lý một số bước vẫn mất thời gian.",
             "Điều tôi băn khoăn là chi phí, độ thuận tiện và khả năng nhận hỗ trợ khi có vấn đề phát sinh."]
        )
    elif reason:
        choices = (
            ["Tôi lựa chọn chủ yếu vì muốn tiết kiệm chi phí và bớt áp lực tự lái xe; nếu tuyến đường thuận tiện thì tôi sẽ sử dụng thường xuyên hơn.",
             "Tôi thấy đây là lựa chọn khá phù hợp với sinh viên vì chi phí hợp lý, dù đôi lúc vẫn phải sắp xếp thời gian chờ xe."] if transit else
            ["Tôi sử dụng vì có thể tìm và sắp xếp tài liệu nhanh hơn, dễ phối hợp với bạn cùng nhóm và chủ động hơn khi học.",
             "Tôi thấy các công cụ này giúp tiết kiệm thời gian, nhưng tôi vẫn kiểm tra lại thông tin trước khi dùng vào bài làm."] if digital else
            ["Tôi ưu tiên lựa chọn phù hợp với nhu cầu, dễ sử dụng và có mức chi phí vừa phải; trải nghiệm thực tế cũng ảnh hưởng khá nhiều đến quyết định của tôi.",
             "Tôi lựa chọn vì thấy khá thuận tiện và có thể đáp ứng nhu cầu hằng ngày, dù vẫn muốn tìm hiểu thêm trước khi dùng lâu dài."]
        )
    elif digital and any(word in label for word in ("cong cu nao", "phan mem nao", "ung dung nao", "ke ten", "liet ke")):
        choices = ["Tôi thường dùng Google Docs, Google Drive và Canva; khi cần gợi ý hoặc giải thích thêm thì tôi tham khảo ChatGPT.",
                   "Tôi dùng Google Drive để lưu tài liệu, Excel để xử lý số liệu và Canva để chuẩn bị phần trình bày."]
    elif transit:
        choices = ["Nhìn chung tôi thấy phương tiện công cộng khá phù hợp với sinh viên, nhất là về chi phí; tôi mong các chuyến xe đúng giờ và kết nối thuận tiện hơn.",
                   "Trải nghiệm của tôi tương đối tốt. Nếu giảm thời gian chờ và cải thiện thông tin tuyến đường thì tôi sẽ sử dụng thường xuyên hơn."]
    elif digital:
        choices = ["Tôi thấy công cụ số khá hữu ích cho việc học và làm việc nhóm; tôi vẫn tự chỉnh sửa nội dung và kiểm tra nguồn trước khi nộp bài.",
                   "Tôi sử dụng khi cần tìm tài liệu hoặc chuẩn bị bài. Nhìn chung khá thuận tiện, nhưng tôi không muốn phụ thuộc hoàn toàn vào kết quả từ công cụ."]
    elif label.startswith(("ban co", "anh chi co", "do you", "have you")):
        choices = ["Có, tôi đã có trải nghiệm và nhìn chung thấy khá phù hợp, nhưng vẫn cân nhắc tùy nhu cầu và điều kiện thực tế.",
                   "Có, tôi thường cân nhắc lựa chọn này khi thấy thuận tiện và phù hợp với nhu cầu của mình."]
    else:
        choices = ["Theo trải nghiệm của tôi, điều quan trọng là sự thuận tiện, thông tin rõ ràng và mức chi phí phù hợp. Nhìn chung tôi có đánh giá khá tích cực, nhưng vẫn muốn một số khâu được cải thiện.",
                   "Tôi đã có một số trải nghiệm và thấy khá phù hợp với nhu cầu hiện tại. Tôi mong được hướng dẫn cụ thể hơn và có thêm lựa chọn linh hoạt khi sử dụng.",
                   "Hiện tại tôi chưa có quá nhiều trải nghiệm để đánh giá chi tiết. Tôi thường tìm hiểu thông tin, hỏi thêm bạn bè rồi mới đưa ra lựa chọn phù hợp với mình."]
    return rng.choice(choices)


def natural_candidates(field, profile, rng):
    intent = text_intent(field)
    label = question_text(field)
    if intent == "birth_date":
        birth = profile["birth_date"]
        values = [f'{birth["day"]:02d}/{birth["month"]:02d}/{birth["year"]}',
                  f'{birth["day"]:02d}-{birth["month"]:02d}-{birth["year"]}',
                  f'{birth["year"]}-{birth["month"]:02d}-{birth["day"]:02d}',
                  f'Ngày {birth["day"]} tháng {birth["month"]} năm {birth["year"]}']
    elif intent == "birth_year":
        values = [str(profile["birth_date"]["year"])]
    elif intent == "student_year":
        values = [f'Năm {profile["student_year"]}', str(profile["student_year"])]
    elif intent in profile:
        values = [str(profile[intent])]
    else:
        values = [natural_answer(field, profile, rng)]
    if intent in ("full_name", "given_name", "middle_name", "surname"):
        if "viet hoa" in label or "in hoa" in label:
            values.insert(0, values[0].upper())
        if "khong dau" in label:
            values.insert(0, " ".join(part.capitalize() for part in plain(values[0]).split()))
    originals = list(values)
    for value in originals:
        ascii_value = " ".join(part.capitalize() for part in plain(value).split())
        if ascii_value != value:
            values.append(ascii_value)
    if intent == "open":
        minimum = max([int(rule[2][0]) for rule in field.get("rules", [])
                       if rule[0] == 6 and rule[1] == 203] or [1])
        expanded = values[0]
        for _ in range(12):
            if len(expanded) >= minimum:
                break
            expanded += " " + natural_answer(field, profile, rng)
        values.insert(0, expanded)
        for rule in field.get("rules", []):
            if rule[0] == 6 and rule[1] == 202:
                maximum = int(rule[2][0])
                values = [value[:maximum].rsplit(" ", 1)[0] if len(value) > maximum
                          else value for value in values]
    return values


def sample_text(field, rng, index, profile=None):
    label = plain(field["question"] + " " + field["description"])
    if profile is None:
        profile = make_profile({"title": "", "fields": [field]}, rng, index, datetime.now().astimezone().date())
    for value in natural_candidates(field, profile, rng):
        if value and all(rule_valid(value, rule) for rule in field["rules"]):
            return value
    token = f"{index:06d}{rng.randrange(10000):04d}"
    candidates = [f"[TEST] Du lieu gia lap {token}", f"Test {token}", "Test", "Demo",
                  f"test.{token}@example.com", "https://example.com/test", "0900000000",
                  "Nguyen Van Test", "CQ64TEST", str(rng.randint(1, 100))]
    if any(word in label for word in ("email", "e-mail", "gmail")):
        candidates.insert(0, f"test.{token}@example.com")
    elif any(word in label for word in ("dien thoai", "phone", "mobile", "sdt")):
        candidates.insert(0, "090000" + str(rng.randrange(10000)).zfill(4))
    elif any(word in label for word in ("ho va ten", "full name", "fullname")):
        candidates.insert(0, "Nguyen Van Test")
    elif any(word in label for word in ("tuoi", "age")):
        candidates.insert(0, str(rng.randint(18, 25)))
    for rule in field["rules"]:
        category, code = rule[:2]
        args = get_at(rule, 2, [])
        args = args if isinstance(args, list) else []
        if category == 1:
            a = float(args[0]) if args else 20
            b = float(args[1]) if len(args) > 1 else a + 100
            numbers = [a, a + 1, a - 1, b, b + 1, b - 1, (a + b) / 2]
            candidates = [str(int(n)) if n.is_integer() else str(n) for n in map(float, numbers)] + candidates
        elif category == 2 and args:
            candidates.insert(0, str(args[0]) + " TEST" if code == 100 else "TEST")
        elif category == 2 and code == 102:
            candidates.insert(0, f"test.{token}@example.com")
        elif category == 2 and code == 103:
            candidates.insert(0, "https://example.com/test")
        elif category == 6 and args:
            length = int(args[0])
            if length < 0 or length > 100000:
                raise FormError("Giới hạn độ dài văn bản không phù hợp: " + field["question"])
            candidates.insert(0, ("TEST" * ((length + 3) // 4))[:length])
        elif category == 4 and args:
            pattern = str(args[0])
            literal = re.match(r"\^?([A-Za-z0-9 _-]+)", pattern)
            if literal:
                candidates.insert(0, literal.group(1) + "TEST")
            for size in re.findall(r"\{(\d+)(?:,\d*)?\}", pattern):
                size = int(size)
                if 0 < size <= 10000:
                    candidates.extend(["1" * size, "A" * size, "a" * size, "0" * size])
    numeric = [rule for rule in field["rules"] if rule[0] == 1]
    if numeric:
        low, high = -1000000.0, 1000000.0
        integer = any(rule[1] == 10 for rule in numeric)
        for rule in numeric:
            code, args = rule[1], get_at(rule, 2, [])
            args = args if isinstance(args, list) else []
            if code in (1, 2):
                low = max(low, math.nextafter(float(args[0]), math.inf) if code == 1 else float(args[0]))
            elif code in (3, 4):
                high = min(high, math.nextafter(float(args[0]), -math.inf) if code == 3 else float(args[0]))
            elif code == 5:
                low, high = max(low, float(args[0])), min(high, float(args[0]))
            elif code == 7:
                low, high = max(low, float(args[0])), min(high, float(args[1]))
        if integer:
            low, high = math.ceil(low), math.floor(high)
        if low <= high:
            for _ in range(20):
                number = rng.randint(int(low), int(high)) if integer else rng.uniform(low, high)
                candidates.insert(0, str(number))
    minimum, maximum = 1, 100000
    for rule in field["rules"]:
        if rule[0] == 6 and isinstance(get_at(rule, 2), list):
            if rule[1] == 202:
                maximum = min(maximum, int(rule[2][0]))
            elif rule[1] == 203:
                minimum = max(minimum, int(rule[2][0]))
    if minimum > maximum:
        raise FormError("Quy tắc độ dài mâu thuẫn: " + field["question"])
    candidates = [(candidate + "TEST" * ((minimum - len(candidate) + 3) // 4))
                  if len(candidate) < minimum else candidate for candidate in candidates]
    candidates = [candidate[:maximum] for candidate in candidates]
    for _ in range(50):
        candidates.append(str(rng.randint(0, 9999999999)))
    for candidate in candidates:
        if candidate and all(rule_valid(candidate, rule) for rule in field["rules"]):
            return candidate
    raise FormError("Không tạo được dữ liệu phù hợp quy tắc của câu: " + field["question"])


class Sampler:
    def __init__(self, schema, seed, reference_date=None):
        self.schema, self.seed = schema, seed
        self.reference_date = (date.fromisoformat(reference_date) if isinstance(reference_date, str)
                               else reference_date or datetime.now().astimezone().date())
        self.last_profile = None
        self.groups = Counter(field["item_id"] for field in schema["fields"])
        self.profile_template = profile_template(schema)
        self._weights = None

    @property
    def weights(self):
        if self._weights is None:
            rng = random.Random(self.seed)
            self._weights = {field["id"]: preference(field, rng) for field in self.schema["fields"]}
        return self._weights

    def new_profile(self, index, *, gender=None):
        rng = random.Random(f"{self.seed}:{index}")
        profile = make_profile(self.schema, rng, index, self.reference_date, gender=gender,
                               prepared=self.profile_template)
        return rng, profile

    def sample_value(self, field, rng, index, profile):
        kind = field["kind"]
        if kind in ("text", "paragraph"):
            return sample_text(field, rng, index, profile)
        if kind == "date":
            return (dict(profile["birth_date"]) if text_intent(field) == "birth_date" else
                    {"year": self.reference_date.year, "month": rng.randint(1, 12), "day": rng.randint(1, 28),
                     "hour": rng.randint(8, 17), "minute": rng.randrange(60)})
        if kind == "time":
            return {"hour": rng.randint(0, 5) if field.get("duration") else rng.randint(0, 23),
                    "minute": rng.randrange(60), "second": rng.randrange(60)}
        raise FormError("Câu hỏi không phải ô văn bản hoặc ngày/giờ: " + field["question"])

    def sample(self, index):
        weights_by_id = self.weights
        rng, profile = self.new_profile(index)
        result, used_columns = {}, defaultdict(set)
        groups = self.groups.copy()
        for field in self.schema["fields"]:
            kind = field["kind"]
            if kind in ("text", "paragraph", "date", "time"):
                value = self.sample_value(field, rng, index, profile)
            else:
                options = field["options"]
                weights = weights_by_id[field["id"]]
                available = [i for i, option in enumerate(options)
                             if not field["exclusive_columns"] or option not in used_columns[field["item_id"]]]
                if not available:
                    raise FormError("Không còn cột hợp lệ trong bảng: " + field["question"])
                if kind in ("multiple", "grid_multiple"):
                    minimum, maximum = field["minimum"], field["maximum"]
                    if field["exclusive_columns"]:
                        maximum = min(maximum, len(available) - groups[field["item_id"]] + 1)
                    else:
                        maximum = min(maximum, len(available))
                    if maximum < minimum:
                        raise FormError("Không thể thỏa quy tắc của bảng: " + field["question"])
                    amount = rng.randint(minimum, maximum)
                    selected = []
                    for _ in range(amount):
                        position = rng.choices(available, weights=[weights[i] for i in available])[0]
                        selected.append(options[position])
                        available.remove(position)
                    value = selected
                else:
                    preferred = coherent_choice(field, profile) if kind in ("single", "dropdown") else None
                    if preferred is not None and preferred in [options[i] for i in available]:
                        value = preferred
                    else:
                        position = rng.choices(available, weights=[weights[i] for i in available])[0]
                        value = options[position]
                if field["exclusive_columns"]:
                    used_columns[field["item_id"]].update(value if isinstance(value, list) else [value])
            result[field["id"]] = value
            groups[field["item_id"]] -= 1
        self.last_profile = profile
        return result


def validate_answers(schema, answers):
    for field in schema["fields"]:
        value = answers.get(field["id"])
        if field["kind"] in ("text", "paragraph"):
            valid = isinstance(value, str) and bool(value) and all(rule_valid(value, rule) for rule in field["rules"])
        elif field["kind"] in ("date", "time"):
            valid = isinstance(value, dict)
        elif field["kind"] in ("multiple", "grid_multiple"):
            valid = (isinstance(value, list) and field["minimum"] <= len(value) <= field["maximum"]
                     and len(value) == len(set(value)) and set(value) <= set(field["options"]))
        else:
            valid = value in field["options"]
        if not valid:
            raise FormError("Dữ liệu không hợp lệ: " + field["question"])


class FormBrowser:
    def __init__(self, headless=False):
        self.headless = headless
        self.runtime = self.browser = self.session = self.page = None
        self.report = None
        self.engine = "fast"
        self.metrics = {}

    def __enter__(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise FormError("Chạy lệnh cài đặt trước: python google_form_tool.py --setup") from exc
        try:
            self.runtime = sync_playwright().start()
            self.browser = self.runtime.chromium.launch(headless=self.headless)
            return self
        except Exception as exc:
            self.__exit__(None, None, None)
            raise FormError("Không mở được Chromium. Chạy python google_form_tool.py --setup. " + str(exc)) from exc

    def __exit__(self, *args):
        for resource in (self.session, self.browser):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    pass
        if self.runtime is not None:
            self.runtime.stop()

    def new_session(self):
        self.report = None
        self._page_history = None
        if self.session is not None:
            self.session.close()
        self.session = self.browser.new_context(locale="vi-VN")
        self.page = self.session.new_page()
        self.page.set_default_timeout(15000)
        self.page.set_default_navigation_timeout(45000)

    def read_schema(self):
        url = form_link(self.page.url, resolved=True)
        snapshot_script = """() => ({
            data: Array.isArray(window.FB_PUBLIC_LOAD_DATA_) ? window.FB_PUBLIC_LOAD_DATA_ : null,
            history: document.querySelector('input[name="pageHistory"]')?.value ?? null
        })"""
        try:
            snapshot = self.page.evaluate(snapshot_script)
            if snapshot["data"] is None:
                self.page.wait_for_function("() => Array.isArray(window.FB_PUBLIC_LOAD_DATA_)", timeout=10000)
                snapshot = self.page.evaluate(snapshot_script)
        except Exception as exc:
            raise FormError("Không truy cập được câu hỏi. Form có thể đã đóng hoặc cần đăng nhập.") from exc
        self._page_history = snapshot["history"]
        return parse_schema(snapshot["data"], url)

    def load(self, url):
        self.new_session()
        self.page.goto(form_link(url), wait_until="domcontentloaded")
        return self.read_schema()

    def button(self, names):
        names = {normalized(name).casefold() for name in names}
        for control in self.page.get_by_role("button").all():
            if control.is_visible() and normalized(control.inner_text()).casefold() in names:
                return control
        return None

    def question_card(self, field):
        named = self.page.locator(f'[name="{field["id"]}"]')
        for control in named.all():
            parents = control.locator("xpath=ancestor::*[@role='listitem'][1]")
            if parents.count() and parents.first.is_visible():
                return parents.first
        title = normalized(field["question"])
        candidates = [card for card in self.page.get_by_role("listitem").all()
                      if normalized(card.inner_text()).startswith(title)]
        with_heading = [card for card in candidates
                        if card.locator('[role="heading"],h1,h2,h3,h4').count()]
        candidates = with_heading or candidates
        if len(candidates) == 1:
            return candidates[0]
        for card in candidates:
            params = card.locator("[data-params]").evaluate_all(
                "els => els.map(el => el.getAttribute('data-params') || '').join(' ')")
            if re.search(r"(?<!\d)" + re.escape(field["id"].split(".")[-1]) + r"(?!\d)", params):
                return card
        raise FormError("Không xác định được duy nhất câu hỏi: " + field["question"])

    @staticmethod
    def checked(control):
        state = control.get_attribute("aria-checked")
        return state == "true" if state is not None else control.is_checked()

    def select(self, control, checked=True):
        from playwright.sync_api import expect
        if self.checked(control) != checked:
            control.click()
        if control.get_attribute("aria-checked") is not None:
            expect(control).to_have_attribute("aria-checked", "true" if checked else "false", timeout=8000)
        elif checked:
            expect(control).to_be_checked()
        else:
            expect(control).not_to_be_checked()

    def choice(self, scope, field, value, role):
        controls = scope.get_by_role(role)
        for control in controls.all():
            if control.get_attribute("data-value") == value or control.get_attribute("aria-label") == value:
                return control
        exact = scope.get_by_role(role, name=value, exact=True)
        if exact.count() == 1:
            return exact
        if field["kind"] == "scale" and controls.count() == len(field["options"]):
            return controls.nth(field["options"].index(value))
        raise FormError(f"Không tìm thấy lựa chọn {value!r}: {field['question']}")

    def grid_scope(self, card, field):
        row_name = normalized(field["row"])
        for role in ("radiogroup", "group", "row"):
            exact = card.get_by_role(role, name=field["row"], exact=True)
            if exact.count():
                return exact.first
            for candidate in card.get_by_role(role).all():
                label = candidate.evaluate("""el => {
                    const ids=(el.getAttribute('aria-labelledby') || '').split(/\\s+/);
                    return el.getAttribute('aria-label') ||
                        ids.map(id => document.getElementById(id)?.textContent || '').join(' ') ||
                        el.textContent;
                }""")
                if row_name in normalized(label) and candidate.get_by_role(
                        "checkbox" if field["kind"] == "grid_multiple" else "radio").count() == len(field["options"]):
                    return candidate
        labels = card.get_by_text(field["row"], exact=True)
        for label in labels.all():
            candidate = label
            for _ in range(6):
                candidate = candidate.locator("xpath=..")
                if candidate.get_by_role("checkbox" if field["kind"] == "grid_multiple" else "radio").count() == len(field["options"]):
                    return candidate
        raise FormError("Không tìm thấy dòng bảng: " + field["row"])

    def fill_temporal(self, card, field, value):
        controls = card.locator("input:not([type=hidden]):visible")
        handled = 0
        parts = {
            "year": ("year", "nam"), "month": ("month", "thang"),
            "day": ("day", "ngay"), "hour": ("hour", "gio"),
            "minute": ("minute", "phut"), "second": ("second", "giay"),
        }
        for control in controls.all():
            kind = control.get_attribute("type") or "text"
            if kind == "date":
                control.fill(f'{value["year"]:04d}-{value["month"]:02d}-{value["day"]:02d}')
            elif kind == "datetime-local":
                control.fill(f'{value["year"]:04d}-{value["month"]:02d}-{value["day"]:02d}T{value["hour"]:02d}:{value["minute"]:02d}')
            elif kind == "time":
                control.fill(f'{value["hour"]:02d}:{value["minute"]:02d}')
            else:
                label = plain(" ".join(control.get_attribute(name) or "" for name in
                                       ("name", "aria-label", "placeholder", "title")))
                part = next((part for part, aliases in parts.items()
                             if any(re.search(r"(^|[^\w])" + alias + r"($|[^\w])", label)
                                    or label.endswith("_" + alias) for alias in aliases)), None)
                if part is None:
                    raise FormError("Không xác định được ô ngày/giờ: " + field["question"])
                control.fill(str(value[part]))
            handled += 1
        if not handled:
            raise FormError("Không tìm thấy ô ngày/giờ: " + field["question"])

    def fill_field(self, field, value):
        card = self.question_card(field)
        kind = field["kind"]
        if kind in ("text", "paragraph"):
            textboxes = card.get_by_role("textbox")
            if textboxes.count() != 1:
                raise FormError("Không xác định được ô nhập: " + field["question"])
            textboxes.fill(value)
            if textboxes.input_value() != value:
                raise FormError("Ô nhập không nhận đủ giá trị: " + field["question"])
            return
        if kind in ("date", "time"):
            self.fill_temporal(card, field, value)
            return
        scope = self.grid_scope(card, field) if kind in ("grid", "grid_multiple") else card
        if kind in ("multiple", "grid_multiple"):
            for control in scope.get_by_role("checkbox").all():
                if self.checked(control):
                    self.select(control, False)
            for option in value:
                self.select(self.choice(scope, field, option, "checkbox"))
            return
        if kind == "dropdown":
            listbox = scope.get_by_role("listbox")
            listbox.click()
            option = self.page.get_by_role("option", name=value, exact=True)
            visible = [control for control in option.all() if control.is_visible()]
            if len(visible) != 1:
                raise FormError("Không xác định được lựa chọn trong danh sách: " + value)
            visible[0].click()
            return
        self.select(self.choice(scope, field, value, "radio"))

    def prepare(self, schema, answers):
        validate_answers(schema, answers)
        try:
            return self._prepare(schema, answers)
        except FormError:
            raise
        except Exception as exc:
            raise FormError("Chưa điền xong form, chưa bấm Gửi. " + str(exc)) from exc

    def before_field(self, field):
        pass

    def before_navigation(self):
        pass

    def fill_page(self, fields, answers):
        started = time.perf_counter()
        handled = set()
        navigation = None
        if getattr(self, "engine", "fast") == "fast":
            from google_fast import fill_google_page, FastFillError
            for field in fields:
                self.before_field(field)
            try:
                email = (getattr(self, "profile", None) or {}).get("email", "test.user@example.com")
                result = fill_google_page(self.page, fields, answers, email=email)
                handled.update(result["handled"])
                if not result["fallback"] and result.get("email_handled"):
                    navigation = result.get("navigation")
            except FastFillError:
                pass
        for field in fields:
            if field["id"] not in handled:
                self.fill_field(field, answers[field["id"]])
        self.metrics["batch_fields"] = self.metrics.get("batch_fields", 0) + len(handled)
        self.metrics["fallback_fields"] = self.metrics.get("fallback_fields", 0) + len(fields) - len(handled)
        self.metrics["fill_ms"] = self.metrics.get("fill_ms", 0) + (time.perf_counter() - started) * 1000
        return navigation

    def fill_email(self):
        for control in self.page.locator('input[name="emailAddress"]:visible').all():
            if not control.input_value():
                control.fill((getattr(self, "profile", None) or {}).get("email", "test.user@example.com"))

    def navigation_state(self):
        return self.page.evaluate("""() => {
            const clean=v=>String(v||'').normalize('NFKC').replace(/\\s+/g,' ').trim().toLowerCase();
            const visible=el=>!!el.getClientRects().length&&getComputedStyle(el).visibility!=='hidden';
            const buttons=Array.from(document.querySelectorAll('button,[role="button"],input[type="submit"],input[type="button"]'))
                .filter(el=>visible(el)&&!el.disabled&&el.getAttribute('aria-disabled')!=='true');
            const name=el=>clean(el.innerText||el.value||el.getAttribute('aria-label'));
            return {history:document.querySelector('input[name="pageHistory"]')?.value??null,
                next:buttons.some(el=>['tiếp','tiếp theo','next'].includes(name(el))),
                submit:buttons.some(el=>['gửi','submit'].includes(name(el)))};
        }""")

    def click_navigation(self, action):
        if action != "submit":
            self.before_navigation()
        result = self.page.evaluate("""action => {
            const clean=v=>String(v||'').normalize('NFKC').replace(/\\s+/g,' ').trim().toLowerCase();
            const names=action==='next'?['tiếp','tiếp theo','next']:['gửi','submit'];
            const matches=Array.from(document.querySelectorAll('button,[role="button"],input[type="submit"],input[type="button"]'))
                .filter(el=>el.getClientRects().length&&getComputedStyle(el).visibility!=='hidden'&&!el.disabled&&el.getAttribute('aria-disabled')!=='true')
                .filter(el=>names.includes(clean(el.innerText||el.value||el.getAttribute('aria-label'))));
            if(matches.length!==1)return false;
            const form=matches[0].closest('form');
            if(form&&!form.checkValidity())return false;
            matches[0].click();return true;
        }""", action)
        if not result:
            raise FormError("Không xác định được duy nhất nút Tiếp/Gửi hoặc trang chưa hợp lệ. Chọn chế độ Tương thích để kiểm tra.")

    def _prepare(self, schema, answers):
        started = time.perf_counter()
        self.metrics = {"engine": getattr(self, "engine", "fast"), "batch_fields": 0, "fallback_fields": 0,
                        "fill_ms": 0, "navigation_ms": 0, "submit_ms": 0}
        self.new_session()
        self.page.goto(schema["url"], wait_until="domcontentloaded")
        current = self.read_schema()
        if fingerprint(current) != fingerprint(schema):
            raise FormError("Form đã thay đổi câu hỏi hoặc quy tắc. Chạy lại để đọc cấu trúc mới.")
        self.metrics["load_ms"] = (time.perf_counter() - started) * 1000
        fields_by_page = defaultdict(list)
        for field in schema["fields"]:
            fields_by_page[field["page"]].append(field)
        fast = getattr(self, "engine", "fast") == "fast"
        history = self._page_history
        if history is None:
            node = self.page.locator('input[name="pageHistory"]')
            node.wait_for(state="attached")
            history = node.input_value()
        visited, seen, seen_items = [], set(), set()
        visited_pages = set()
        for _ in range(len(schema["pages"]) + 1):
            page_index = int(history.split(",")[-1])
            if page_index in visited_pages or not 0 <= page_index < len(schema["pages"]):
                raise FormError("Phân trang tạo vòng lặp hoặc không xác định.")
            visited.append(page_index)
            visited_pages.add(page_index)
            fields = fields_by_page[page_index]
            navigation = self.fill_page(fields, answers)
            seen.update(field["id"] for field in fields)
            seen_items.update(field["item_id"] for field in fields)
            if navigation is None:
                self.fill_email()
            if fast:
                navigation = navigation or self.navigation_state()
                if not navigation["next"] and not navigation["submit"]:
                    navigation = self.navigation_state()
                has_next, has_submit = navigation["next"], navigation["submit"]
            else:
                next_button = self.button(("Tiếp", "Next"))
                has_next = next_button is not None
                has_submit = self.button(("Gửi", "Submit")) is not None if not has_next else False
            if not has_next:
                if not has_submit:
                    raise FormError("Không tìm thấy nút Tiếp/Gửi. Kiểm tra đăng nhập hoặc CAPTCHA.")
                required_pages = visited_pages if schema["branching"] else fields_by_page
                expected = {field["id"] for page in required_pages for field in fields_by_page[page]}
                if not expected <= seen:
                    raise FormError("Chưa điền đủ câu hỏi trên các trang cần đi qua.")
                if not schema["branching"] and len(visited) != len(schema["pages"]):
                    raise FormError("Chưa đi qua đủ toàn bộ trang của form.")
                self.report = {"pages": len(visited), "page_indices": visited,
                               "questions": len(seen_items), "fields": len(seen)}
                self.metrics["prepare_ms"] = (time.perf_counter() - started) * 1000
                return self.report
            moved = time.perf_counter()
            if fast:
                self.click_navigation("next")
            else:
                self.before_navigation()
                next_button.click()
            try:
                history = self.page.wait_for_function("""old => {
                    const input=document.querySelector('input[name="pageHistory"]');
                    return input && input.value !== old && input.value;
                }""", arg=history, timeout=25000).json_value()
                self.metrics["navigation_ms"] += (time.perf_counter() - moved) * 1000
            except Exception as exc:
                alerts = " ".join(self.page.locator('[role="alert"]').all_text_contents())
                raise FormError(f"Không qua được trang {page_index + 1}. {alerts}") from exc
        raise FormError("Không xác định được trang cuối, chưa bấm Gửi.")

    def submit(self, schema):
        if not self.report:
            raise FormError("Chưa điền đủ các trang.")
        fast = getattr(self, "engine", "fast") == "fast"
        button = None if fast else self.button(("Gửi", "Submit"))
        if (fast and not self.navigation_state()["submit"]) or (not fast and button is None):
            raise FormError("Không thấy nút Gửi ở trang cuối.")
        started = time.perf_counter()
        try:
            with self.page.expect_response(lambda response:
                response.request.method == "POST"
                and urlsplit(response.url).hostname == "docs.google.com"
                and urlsplit(response.url).path.endswith("/formResponse"), timeout=45000
            ) as pending:
                if fast:
                    self.click_navigation("submit")
                else:
                    button.click()
            response = pending.value
            self.page.wait_for_load_state("domcontentloaded")
            self.page.wait_for_function("""() => {
                const body=document.body;
                return body && !document.querySelector('form') && body.innerText.trim();
            }""", timeout=25000)
            text = normalized(self.page.locator("body").inner_text())
            markers = self.page.locator(".vHW8K,.freebirdFormviewerViewResponseConfirmationMessage")
            expected = normalized(schema["confirmation"])
            if response.status != 200 or self.page.locator("form").count():
                raise FormError("Google chưa xác nhận lưu phản hồi.")
            if not (markers.count() and text) and not (expected and expected in text):
                raise FormError("Trang trả về chưa có thông báo xác nhận.")
            self.metrics["submit_ms"] = (time.perf_counter() - started) * 1000
            return True
        except Exception as exc:
            raise FormError("Chưa xác định phản hồi đã lưu hay chưa. Không tự gửi lại. " + str(exc)) from exc

def form_browser(url):
    from microsoft_forms import is_microsoft_url, create_microsoft_browser
    if is_microsoft_url(url):
        return create_microsoft_browser(FormBrowser, FormError, normalized, plain, fingerprint, validate_answers)
    if urlsplit(url).hostname not in ("docs.google.com", "forms.gle", "forms.google.com"):
        from html_forms import create_html_browser
        return create_html_browser(FormBrowser, FormError, normalized, plain, fingerprint, validate_answers)
    return FormBrowser


def spss_available():
    from importlib.util import find_spec
    return find_spec("pyreadstat") is not None and find_spec("pandas") is not None


class RunLog:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event):
        event = dict(event, time=datetime.now().astimezone().isoformat())
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def read(self):
        try:
            events = [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]
        except (OSError, ValueError) as exc:
            raise FormError("Không đọc được nhật ký; kiểm tra trước khi chạy tiếp để tránh gửi trùng.") from exc
        if not events or events[0].get("event") != "run":
            raise FormError("Nhật ký không có cấu hình phiên chạy.")
        states = {}
        for event in events[1:]:
            if "index" in event and event.get("event") in ("started", "confirmed"):
                states[event["index"]] = event["event"]
        if any(status == "started" for status in states.values()):
            raise FormError("Có lượt đã bấm Gửi nhưng chưa rõ kết quả. Kiểm tra biểu mẫu và nhật ký trước.")
        completed = set(states)
        if completed != set(range(1, len(completed) + 1)):
            raise FormError("Nhật ký không liên tục; cần kiểm tra trước khi gửi tiếp.")
        return events[0], len(completed)


def inspect_form(schema, sampler):
    print(schema["title"])
    print(f'{len(schema["pages"])} trang; {schema["question_count"]} câu hỏi; '
          f'{len(schema["fields"])} mục trả lời kể cả từng dòng bảng.')
    for field in schema["fields"]:
        row = " / " + field["row"] if field["row"] else ""
        print(f'\nTrang {field["page"] + 1} | {field["kind"]} | {field["question"]}{row}')
        weights = sampler.weights[field["id"]]
        if weights:
            total = sum(weights)
            for option, weight in zip(field["options"], weights):
                print(f"  {100 * weight / total:5.1f}%  {option}")


def setup():
    requirements = Path(__file__).resolve().with_name("requirements.txt")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", str(requirements)])
    subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"])


WEB_HTML = _read_web_template(Path(__file__).resolve().with_name("web_ui.html"))

def apportion_web(weights, total):
    factor = total / sum(weights) if sum(weights) else 0
    ideal = [value * factor for value in weights]
    result = [math.floor(value) for value in ideal]
    order = sorted(range(len(weights)), key=lambda i: ideal[i] - result[i], reverse=True)
    for index in order[:total - sum(result)]:
        result[index] += 1
    return result


def default_web_config(schema, seed):
    rng = random.Random(seed)
    config = {}
    for field in schema["fields"]:
        if not field["options"]:
            config[field["id"]] = {"mode": "auto", "value": ""}
            continue
        weights = preference(field, rng)
        if field["kind"] in ("multiple", "grid_multiple"):
            target = min(field["maximum"] * 100, max(field["minimum"] * 100, 180))
            values = [0] * len(weights)
            for _ in range(target):
                available = [i for i in range(len(weights)) if values[i] < 100]
                index = rng.choices(available, weights=[weights[i] for i in available])[0]
                values[index] += 1
        else:
            values = apportion_web(weights, 100)
        config[field["id"]] = {"rates": dict(zip(field["options"], values))}
    return config


def validate_web_config(schema, supplied):
    if not isinstance(supplied, dict):
        raise FormError("Cấu hình tỷ lệ không hợp lệ.")
    expected = {field["id"] for field in schema["fields"]}
    if set(supplied) != expected:
        raise FormError("Cấu hình không khớp các câu hỏi của form. Hãy đọc lại biểu mẫu.")
    result = {}
    for field in schema["fields"]:
        entry = supplied[field["id"]]
        if not isinstance(entry, dict):
            raise FormError("Thiếu cấu hình câu hỏi: " + field["question"])
        if field["options"]:
            rates = entry.get("rates")
            if not isinstance(rates, dict) or set(rates) != set(field["options"]):
                raise FormError("Thiếu tỷ lệ cho đáp án: " + field["question"])
            values = list(rates.values())
            if any(type(value) not in (int, float) or not math.isfinite(value)
                   or not 0 <= value <= 100 for value in values):
                raise FormError("Tỷ lệ phải nằm từ 0% đến 100%.")
            total = sum(values)
            if field["kind"] in ("multiple", "grid_multiple"):
                minimum, maximum = field["minimum"] * 100, field["maximum"] * 100
                if not minimum - .0001 <= total <= maximum + .0001:
                    raise FormError(f'{field["question"]}: tổng tỷ lệ phải từ {minimum}% đến {maximum}%.')
            elif not math.isclose(total, 100, abs_tol=.0001):
                raise FormError(f'{field["question"]}: tổng tỷ lệ cần bằng 100%, hiện là {total:g}%.')
            result[field["id"]] = {"rates": {key: float(value) for key, value in rates.items()}}
        else:
            mode = entry.get("mode", "auto")
            value = str(entry.get("value", "")).strip()
            if mode not in ("auto", "fixed"):
                raise FormError("Chế độ trả lời không hợp lệ.")
            if mode == "fixed" and field["kind"] not in ("text", "paragraph"):
                raise FormError("Câu ngày/giờ dùng chế độ tự động.")
            if mode == "fixed" and (not value or not all(rule_valid(value, rule) for rule in field["rules"])):
                raise FormError("Nội dung tự nhập chưa phù hợp quy tắc: " + field["question"])
            result[field["id"]] = {"mode": mode, "value": value}
    return result


class WebRatioSampler:
    def __init__(self, schema, config, total, seed, reference_date=None):
        self.schema, self.config, self.total, self.seed = schema, validate_web_config(schema, config), total, seed
        self.base = Sampler(schema, seed, reference_date)
        self.last_profile = None
        self.choice_fields = tuple(field for field in schema["fields"] if field["options"])
        self.value_fields = tuple(field for field in schema["fields"] if not field["options"])
        self.rates = {field["id"]: tuple(self.config[field["id"]]["rates"][option]
                                       for option in field["options"]) for field in self.choice_fields}
        self.option_indices = {field["id"]: tuple(range(len(field["options"]))) for field in self.choice_fields}
        self.cumulative_rates = {field["id"]: tuple(itertools.accumulate(self.rates[field["id"]]))
                                 for field in self.choice_fields if not field["exclusive_columns"]}
        self.profile_fields = tuple((field, text_intent(field)) for field in self.choice_fields
                                    if field["kind"] in ("single", "dropdown"))
        self.gender_fields = tuple(field for field, intent in self.profile_fields if intent == "gender")
        self.exact = total > 0 and not schema["branching"] and not any(
            field["exclusive_columns"] for field in schema["fields"])
        self.remaining = {}
        if self.exact:
            for field in self.choice_fields:
                percents = self.rates[field["id"]]
                if field["kind"] in ("multiple", "grid_multiple"):
                    ideal = [percent * total / 100 for percent in percents]
                    values = [math.floor(value) for value in ideal]
                    desired = math.floor(sum(ideal) + .5)
                    order = sorted(range(len(values)), key=lambda i: ideal[i] - values[i], reverse=True)
                    for position in order[:desired - sum(values)]:
                        values[position] += 1
                else:
                    values = apportion_web(percents, total)
                self.remaining[field["id"]] = values

    def reconcile_profile(self, answers, profile):
        for field, intent in self.profile_fields:
            value = answers[field["id"]]
            if intent in ("school", "faculty", "occupation"):
                profile[intent] = value
            elif intent == "student_year":
                for year in range(1, 5):
                    test_profile = dict(profile, student_year=year)
                    if coherent_choice(field, test_profile) == value:
                        profile["student_year"] = year
                        break

    def sample(self, index):
        answers = {}
        rng = random.Random(f"web:{self.seed}:{index}")
        used_columns = defaultdict(set)
        groups = self.base.groups.copy()
        for field in self.choice_fields:
            options = field["options"]
            available = ([i for i, value in enumerate(options) if value not in used_columns[field["item_id"]]]
                         if field["exclusive_columns"] else self.option_indices[field["id"]])
            if not available:
                raise FormError("Không còn cột hợp lệ trong bảng: " + field["question"])
            rates = self.rates[field["id"]]
            multi = field["kind"] in ("multiple", "grid_multiple")
            if self.exact:
                remaining = self.remaining[field["id"]]
                rows = self.total - index + 1
                if multi:
                    forced = [i for i, amount in enumerate(remaining) if amount == rows]
                    budget = sum(remaining)
                    lower = max(field["minimum"], len(forced), budget - field["maximum"] * (rows - 1))
                    upper = min(field["maximum"], sum(value > 0 for value in remaining),
                                budget - field["minimum"] * (rows - 1))
                    amount = rng.randint(lower, upper)
                    selected = list(forced)
                    selected_indices = set(forced)
                    while len(selected) < amount:
                        candidates = [i for i, value in enumerate(remaining) if value > 0 and i not in selected_indices]
                        chosen = rng.choices(candidates, weights=[remaining[i] for i in candidates])[0]
                        selected.append(chosen)
                        selected_indices.add(chosen)
                    for position in selected:
                        remaining[position] -= 1
                else:
                    position = rng.choices(range(len(options)), weights=remaining)[0]
                    remaining[position] -= 1
                    selected = [position]
            elif multi:
                maximum = min(field["maximum"], len(available))
                if field["exclusive_columns"]:
                    maximum = min(maximum, len(available) - groups[field["item_id"]] + 1)
                amount = max(field["minimum"], min(maximum, math.floor(sum(rates) / 100 + rng.random())))
                selected = []
                selected_indices = set()
                for _ in range(amount):
                    candidates = [i for i in available if i not in selected_indices and rates[i] > 0]
                    if not candidates:
                        raise FormError("Tỷ lệ không đủ đáp án hợp lệ trong bảng: " + field["question"])
                    chosen = rng.choices(candidates, weights=[rates[i] for i in candidates])[0]
                    selected.append(chosen)
                    selected_indices.add(chosen)
            else:
                if field["exclusive_columns"]:
                    selected = [rng.choices(available, weights=[rates[i] for i in available])[0]]
                else:
                    selected = [rng.choices(available, cum_weights=self.cumulative_rates[field["id"]])[0]]
            value = [options[i] for i in selected] if multi else options[selected[0]]
            answers[field["id"]] = value
            if field["exclusive_columns"]:
                used_columns[field["item_id"]].update(value if isinstance(value, list) else [value])
            groups[field["item_id"]] -= 1
        gender = None
        for field in self.gender_fields:
            label = plain(answers[field["id"]])
            if label in ("nam", "nu", "male", "female", "man", "woman"):
                gender = "Nam" if label in ("nam", "male", "man") else "Nữ"
        _, profile = self.base.new_profile(index, gender=gender)
        self.reconcile_profile(answers, profile)
        for field in self.value_fields:
            entry = self.config[field["id"]]
            if field["kind"] in ("text", "paragraph"):
                answers[field["id"]] = (entry["value"] if entry.get("mode") == "fixed" else
                                         self.base.sample_value(field, rng, index, profile))
            else:
                answers[field["id"]] = self.base.sample_value(field, rng, index, profile)
        validate_answers(self.schema, answers)
        self.last_profile = profile
        return answers


class WebStopRequested(Exception):
    pass


class WebPauseRequested(Exception):
    pass


class WebApplication:
    def __init__(self, force_check=False, headless_only=False, key_store=None, *, browser_workers=None, scan_workers=None):
        import threading
        from work_queue import TaskDispatcher
        def workers(value, variable, default):
            raw = os.environ.get(variable, default) if value is None else value
            try:
                if type(raw) is not int and not (isinstance(raw, str) and re.fullmatch(r"[0-9]+", raw.strip())):
                    raise ValueError()
                result = int(raw)
            except (TypeError, ValueError) as exc:
                raise FormError(variable + " cần là số nguyên từ 1 đến 10.") from exc
            if not 1 <= result <= 10:
                raise FormError(variable + " cần là số nguyên từ 1 đến 10.")
            return result
        browser_capacity = workers(browser_workers, "KD_BROWSER_WORKERS", 1)
        scan_capacity = workers(scan_workers, "KD_SCAN_WORKERS", 2)
        self.key_store = key_store or configured_key_store()
        self.headless_only = headless_only
        self.lock = threading.RLock()
        self.export_lock = threading.Lock()
        self.scans, self.jobs = {}, {}
        self.token = secrets.token_urlsafe(32)
        self.force_check = force_check
        self._closing = False
        self._last_cleanup = 0
        self._browser_local = threading.local()
        self.browser_queue = TaskDispatcher(browser_capacity,
                                            name="kd-browser", on_worker_exit=self._close_worker_browser)
        self.scan_queue = TaskDispatcher(scan_capacity, name="kd-metadata")

    def _close_worker_browser(self):
        launcher = getattr(self._browser_local, "launcher", None)
        if launcher is not None:
            try:
                launcher.__exit__(None, None, None)
            except Exception:
                pass
            finally:
                self._browser_local.launcher = None

    def _browser(self, browser_type, headless=True):
        from contextlib import contextmanager
        @contextmanager
        def lease():
            # Real Playwright objects stay on their owning worker thread. Every
            # respondent still gets a fresh context; only Chromium is reused.
            if not hasattr(FormBrowser, "new_session"):
                with browser_type(headless=headless) as browser:
                    yield browser
                return
            launcher = getattr(self._browser_local, "launcher", None)
            if launcher is not None and (launcher.headless != headless or not launcher.browser.is_connected()):
                self._close_worker_browser()
                launcher = None
            if launcher is None:
                launcher = FormBrowser(headless=headless).__enter__()
                self._browser_local.launcher = launcher
            browser = browser_type(headless=headless)
            browser.browser = launcher.browser
            try:
                yield browser
            finally:
                if browser.session is not None:
                    try:
                        browser.session.close()
                    except Exception:
                        # Cleanup cannot invalidate a confirmed response. Retire
                        # this launcher before the next respondent uses it.
                        self._close_worker_browser()
        return lease()

    def close(self, wait=True):
        with self.lock:
            self._closing = True
            jobs = list(self.jobs.values())
        for job in jobs:
            if job["status"] in ("queued", "running", "stopping"):
                self.stop_job(job["id"])
        self.scan_queue.shutdown(wait=wait, cancel_pending=True)
        self.browser_queue.shutdown(wait=wait, cancel_pending=True)
        self.key_store.close()

    def cleanup(self):
        now = time.time()
        with self.lock:
            if now - self._last_cleanup < 60:
                return
            self._last_cleanup = now
            active_scans = {job.get("scan_id") for job in self.jobs.values()
                            if job["status"] in ("queued", "running", "stopping")}
            self.scans = {key: value for key, value in self.scans.items()
                          if value["status"] == "scanning" or key in active_scans
                          or now - value.get("updated_at", value.get("created_at", now)) < 3600}
            expired = [key for key, job in self.jobs.items()
                       if job["status"] in ("completed", "stopped", "error")
                       and now - job.get("updated_at", now) > 86400]
            for key in expired:
                job = self.jobs.pop(key)
                path = Path(job.get("log_path", ""))
                folder = Path(tempfile.gettempdir()) / "google_form_tool"
                if path.parent == folder and path.name == "web_" + key + ".jsonl":
                    path.unlink(missing_ok=True)

    def read_form_schema(self, url):
        if urlsplit(url).hostname in ("docs.google.com", "forms.gle", "forms.google.com"):
            from form_scanning import scan_google
            data, resolved = scan_google(url)
            return parse_schema(data, resolved)
        with self._browser(form_browser(url), headless=True) as browser:
            return browser.load(url)

    def scan(self, url, provider="auto", owner_id=None):
        if provider not in ("auto", "google", "microsoft", "html"):
            raise FormError("Loại biểu mẫu không hợp lệ.")
        url = form_link(str(url), allow_generic=provider == "html")
        self.cleanup()
        scan_id = secrets.token_hex(8)
        state = {"id": scan_id, "status": "scanning", "url": url, "owner_id": owner_id,
                 "created_at": time.time(), "updated_at": time.time()}
        with self.lock:
            if self._closing:
                raise FormError("Máy chủ đang khởi động lại. Vui lòng thử lại sau.")
            if owner_id and any(item.get("owner_id") == owner_id and item["status"] == "scanning"
                                for item in self.scans.values()):
                raise FormError("Biểu mẫu của bạn đang được đọc. Vui lòng chờ đọc xong.")
            self.scans[scan_id] = state
        queue = self.scan_queue if urlsplit(url).hostname in ("docs.google.com", "forms.gle", "forms.google.com") else self.browser_queue
        def worker():
            try:
                schema = self.read_form_schema(url)
                config = default_web_config(schema, secrets.randbits(32))
                with self.lock:
                    state.update(status="ready", schema=schema, config=config, updated_at=time.time())
            except Exception as exc:
                with self.lock:
                    state.update(status="error", error=str(exc), updated_at=time.time())
        try:
            queue.submit("scan-" + scan_id, worker)
        except RuntimeError:
            with self.lock:
                self.scans.pop(scan_id, None)
            raise FormError("Máy chủ đang khởi động lại. Vui lòng thử lại sau.")
        return {"scan_id": scan_id}

    def scan_state(self, scan_id):
        with self.lock:
            state = self.scans.get(scan_id)
            if state is None:
                raise FormError("Phiên đọc form không còn tồn tại. Hãy đọc lại.")
            result = dict(state)
            if state["status"] == "scanning":
                queue = self.scan_queue if urlsplit(state.get("url", "")).hostname in ("docs.google.com", "forms.gle", "forms.google.com") else self.browser_queue
                queued = queue.snapshot("scan-" + scan_id)
                result.update(queue_position=queued["queue_position"], queue_waiting=queued["queued"])
            return result

    def job_state(self, job_id, compact=False):
        import copy
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise FormError("Không tìm thấy lượt chạy.")
            excluded = {"stop_event", "control", "schema", "config", "access_key_hash", "access_activation_id", "owner_id", "guest_id"}
            if compact:
                excluded.update(("counts", "answered_counts", "last_profile"))
            snapshot = {key: value for key, value in job.items() if key not in excluded and not key.startswith("_")}
            if compact:
                snapshot["events"] = job.get("events", [])[-12:]
            snapshot["elapsed_seconds"] = round(max(0, time.time() - job.get("created_at", time.time())), 1)
            if job["status"] in ("queued", "running", "stopping"):
                queue = self.browser_queue.snapshot(job_id)
                remaining = max(0, job.get("_next_due", 0) - time.monotonic())
                snapshot.update(worker_capacity=queue["capacity"], queue_position=queue["queue_position"],
                                queue_waiting=queue["queued"] and not job["control"].paused and remaining <= 0)
                if not job["control"].paused:
                    snapshot.update(waiting=remaining > 0, remaining_seconds=remaining,
                                    next_run_at=time.time() + remaining if remaining else None)
            return copy.deepcopy(snapshot)

    def report_job(self, job_id):
        import copy
        from teaching_tools import summary_report, TeachingToolsError
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise FormError("Không tìm thấy lượt chạy.")
            snapshot = copy.deepcopy({key: job.get(key) for key in ("mode", "confirmed", "status", "counts", "answered_counts")})
            schema = job["schema"]
        try:
            return summary_report(schema, snapshot)
        except TeachingToolsError as exc:
            raise FormError(str(exc)) from exc

    def preset(self, scan_id, payload):
        from teaching_tools import ratio_preset, TeachingToolsError
        state = self.scan_state(scan_id)
        if state["status"] != "ready":
            raise FormError("Biểu mẫu chưa sẵn sàng.")
        config = validate_web_config(state["schema"], payload.get("config"))
        try:
            result = ratio_preset(state["schema"], config, payload.get("preset"))
            return {"config": validate_web_config(state["schema"], result)}
        except TeachingToolsError as exc:
            raise FormError(str(exc)) from exc

    def export_job(self, job_id, kind="sav"):
        from spss_export import export_spss, SpssExportError
        from teaching_tools import export_csv_bundle, TeachingToolsError
        if kind not in ("sav", "zip"):
            raise FormError("Định dạng xuất không hợp lệ.")
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None or not job.get("confirmed") or not job.get("log_path"):
                raise FormError("Chưa có phản hồi được xác nhận để xuất dữ liệu. Điền thử không tạo dữ liệu đã gửi.")
            schema, log_path = job["schema"], job["log_path"]
        self.export_lock.acquire()
        target = None
        try:
            descriptor, filename = tempfile.mkstemp(prefix="KDautoform_", suffix="." + kind)
            os.close(descriptor)
            target = Path(filename)
            return (export_spss if kind == "sav" else export_csv_bundle)(schema, log_path, target)
        except (SpssExportError, TeachingToolsError) as exc:
            if target:
                target.unlink(missing_ok=True)
            raise FormError(str(exc)) from exc
        except Exception:
            if target:
                target.unlink(missing_ok=True)
            raise
        finally:
            self.export_lock.release()

    def event(self, job, message, level="info"):
        with self.lock:
            job["events"].append({"time": datetime.now().strftime("%H:%M:%S"), "message": message, "level": level})
            job["events"] = job["events"][-120:]
            job["updated_at"] = time.time()

    def start_job(self, payload):
        import threading
        from run_scheduling import parse_schedule, RunControl
        state = self.scan_state(str(payload.get("scan_id", "")))
        if state["status"] != "ready":
            raise FormError("Biểu mẫu chưa sẵn sàng.")
        count = payload.get("count", 100)
        if type(count) is not int or count < 0:
            raise FormError("Số lượng phải là số nguyên từ 0 trở lên.")
        delay = payload.get("delay", 2)
        if type(delay) not in (int, float) or not math.isfinite(delay) or delay < 0:
            raise FormError("Khoảng nghỉ không hợp lệ.")
        schedule = parse_schedule(payload.get("schedule"), legacy_delay=delay)
        engine = payload.get("engine", "fast")
        if engine not in ("fast", "compatible"):
            raise FormError("Chế độ xử lý phải là Nhanh hoặc Tương thích.")
        if state["schema"].get("provider", "google") != "google":
            engine = "compatible"
        seed = payload.get("seed")
        if seed is None:
            seed = secrets.randbits(48)
        elif type(seed) is not int or not 0 <= seed <= 9007199254740991:
            raise FormError("Seed phải là số nguyên từ 0 đến 9007199254740991 hoặc để trống.")
        reference = payload.get("reference_date")
        if reference is None:
            reference = datetime.now().astimezone().date().isoformat()
        try:
            if not isinstance(reference, str):
                raise ValueError()
            reference = date.fromisoformat(reference).isoformat()
        except ValueError as exc:
            raise FormError("Ngày tham chiếu cần có dạng YYYY-MM-DD hợp lệ.") from exc
        config = validate_web_config(state["schema"], payload.get("config"))
        mode = "check" if self.force_check else payload.get("mode", "send")
        if mode not in ("check", "send"):
            raise FormError("Chế độ chạy không hợp lệ.")
        job_id = secrets.token_hex(8)
        job = {"id": job_id, "status": "queued", "mode": mode, "requested": count,
               "total": 1 if mode == "check" else count, "processed": 0, "confirmed": 0,
               "current_page": 0, "current_question": "", "events": [], "counts": {}, "answered_counts": {},
               "schema": state["schema"], "config": config, "scan_id": state["id"], "delay": float(delay),
               "schedule": schedule, "paused": False, "pause_requested": False, "waiting": False,
               "engine": engine, "last_response_seconds": None, "last_performance": {},
               "queue_waiting": True, "queue_position": 0, "worker_capacity": self.browser_queue.capacity,
               "next_run_at": None, "remaining_seconds": 0,
               "headless": self.headless_only or bool(payload.get("headless", True)), "stop_event": threading.Event(),
               "created_at": time.time(), "updated_at": time.time(), "title": state["schema"]["title"],
               "seed": seed, "reference_date": reference, "access_key_id": payload.get("_key_id"),
               "owner_id": payload.get("_owner_id"), "guest_id": payload.get("_guest_id"),
               "access_activation_id": payload.get("_activation_id", ""),
               "access_key_hash": payload.get("_key_hash", ""), "exact": count > 0 and not state["schema"]["branching"]
               and not any(field["exclusive_columns"] for field in state["schema"]["fields"])}
        job["control"] = RunControl(job["stop_event"])
        with self.lock:
            if self._closing:
                raise FormError("Máy chủ đang khởi động lại. Vui lòng thử lại sau.")
            if any(item.get("owner_id") == job["owner_id"] and item["status"] in ("queued", "running", "stopping")
                   for item in self.jobs.values()):
                raise FormError("Bạn đã có một đợt đang hoạt động. Hãy dừng hoặc chờ đợt đó hoàn tất.")
            if not job["owner_id"] or not active_job_key(job, self.key_store):
                raise FormError("Phiên sử dụng không hợp lệ. Vui lòng tải lại trang.")
            if not job["access_key_id"]:
                usage = self.key_store.guest_usage(job["guest_id"], fresh=True)
                if not usage["remaining"]:
                    raise QuotaExceeded("Bạn đã dùng hết 10 lượt miễn phí. Nhập key được cấp để tiếp tục.")
                if mode == "send" and (count == 0 or count > usage["remaining"]):
                    raise QuotaExceeded(f'Bạn còn {usage["remaining"]} lượt miễn phí. Giảm số lượng hoặc nhập key unlimited.')
            elif mode == "send":
                self.key_store.claim(job["access_key_id"], job["access_key_hash"], job_id, job["access_activation_id"])
            self.jobs[job_id] = job
            self.scans[state["id"]]["config"] = config
        try:
            self._enqueue_job(job)
        except RuntimeError as exc:
            job.update(status="error", error="Không khởi động được đợt chạy.")
            if mode == "send" and job["access_key_id"]:
                self.key_store.finish(job["access_key_id"], job["access_key_hash"], job_id, "error")
            raise FormError("Không khởi động được đợt chạy. Chưa gửi phản hồi; bạn có thể thử lại.") from exc
        return {"job_id": job_id, "mode": mode, "key_used": False}

    def _enqueue_job(self, job, due=None):
        due = time.monotonic() if due is None else due
        with self.lock:
            if job.get("_finished") or self._closing:
                return
            job["_next_due"] = due
            job.update(status="queued", queue_waiting=due <= time.monotonic(),
                       waiting=due > time.monotonic(), next_run_at=time.time() + max(0, due - time.monotonic()),
                       remaining_seconds=max(0, due - time.monotonic()))
        self.browser_queue.submit(job["id"], lambda: self.run_job(job), due_monotonic=due,
                                  ready=lambda: not job["control"].paused and not job["control"].stopped)

    def _finish_job(self, job, status, error=None):
        with self.lock:
            if job.get("_finished"):
                return
            job["_finished"] = True
            job.update(status=status, current_question="", paused=False, pause_requested=False, waiting=False,
                       queue_waiting=False, queue_position=0, next_run_at=None, remaining_seconds=0)
            if error:
                job["error"] = error
            job.pop("_execution", None)
        self.browser_queue.cancel(job["id"])
        if job["mode"] == "send" and job["access_key_id"]:
            try:
                self.key_store.finish(job["access_key_id"], job["access_key_hash"], job["id"], status)
            except FormError:
                self.event(job, "Chưa cập nhật được kết quả vào kho key. Kiểm tra kết nối CSDL.", "warning")
        self.event(job, error or ("Hoàn tất lượt chạy." if status == "completed" else "Đã dừng đợt chạy."),
                   "error" if error else "success" if status == "completed" else "info")

    def run_job(self, job):
        with self.lock:
            if job.get("_finished"):
                return
            job["_executing"] = True
            job["_turn_started"] = time.monotonic()
            job.update(status="running", queue_waiting=False, waiting=False, next_run_at=None, remaining_seconds=0)
        try:
            if job["control"].stopped or not active_job_key(job, self.key_store):
                raise WebStopRequested()
            if job["control"].paused:
                raise WebPauseRequested()
            self._run_job(job)
        except WebPauseRequested:
            with self.lock:
                paused = job["control"].paused
                job.update(paused=paused, pause_requested=paused, status="queued", current_question="")
            self.event(job, "Phản hồi chưa gửi sẽ được điền lại với cùng đáp án khi tiếp tục.")
        except WebStopRequested:
            self._finish_job(job, "stopped")
        except Exception as exc:
            self._finish_job(job, "error", str(exc))
        finally:
            with self.lock:
                job["_executing"] = False
        with self.lock:
            terminal = job["status"] in ("completed", "stopped", "error")
            finished = job.get("_finished")
        if terminal:
            if not finished:
                self._finish_job(job, job["status"])
            return
        if job["control"].stopped:
            self._finish_job(job, "stopped")
            return
        if job["total"] and job["processed"] >= job["total"]:
            self._finish_job(job, "completed")
            return
        context = job.get("_execution", {})
        due = context.get("next_due", time.monotonic())
        self._enqueue_job(job, due)

    def _run_job(self, job):
        from run_scheduling import sample_interval, next_start_deadline
        schema, control, app = job["schema"], job["control"], self
        context = job.get("_execution")
        if context is None:
            path = Path(tempfile.gettempdir()) / "google_form_tool" / ("web_" + job["id"] + ".jsonl")
            log = RunLog(path)
            context = {"sampler": WebRatioSampler(schema, job["config"], job["requested"], job["seed"], job["reference_date"]),
                       "timing_rng": random.Random(job["seed"] ^ 0x4B4454494D45), "log": log}
            with self.lock:
                job["_execution"] = context
                job["log_path"] = str(path)
            log.write({"event": "run", "synthetic": True, "url": schema["url"], "count": job["requested"],
                       "seed": job["seed"], "config": job["config"], "mode": job["mode"],
                       "sampler_version": WEB_SAMPLER_VERSION,
                       "provider": schema.get("provider", "google"), "schedule": job["schedule"],
                       "reference_date": job["reference_date"], "engine": job["engine"]})
        sampler, log = context["sampler"], context["log"]
        index = job["processed"] + 1
        if "pending" not in context:
            context["pending"] = {"answers": sampler.sample(index), "profile": sampler.last_profile}
        pending = context["pending"]
        answers, profile = pending["answers"], pending["profile"]
        def checkpoint(check_access=True):
            if control.stopped:
                raise WebStopRequested()
            if control.paused:
                raise WebPauseRequested()
            if check_access and not active_job_key(job, app.key_store):
                raise WebStopRequested()
        class ProgressBrowser(form_browser(schema["url"])):
            def before_field(self, field):
                checkpoint(check_access=bool(job["access_key_id"]))
                with app.lock:
                    changed = job["current_page"] != field["page"] + 1
                    job.update(current_page=field["page"] + 1, current_question=field["question"])
                if changed:
                    app.event(job, f'Đang điền trang {field["page"] + 1}/{len(schema["pages"])}.')
            def before_navigation(self):
                checkpoint(check_access=bool(job["access_key_id"]))
            def fill_field(self, field, value):
                self.before_field(field)
                return super().fill_field(field, value)
            def prepare(self, schema, answers):
                validate_answers(schema, answers)
                return self._prepare(schema, answers)
        checkpoint()
        started = job.get("_turn_started", time.monotonic())
        confirmed_at = None
        with self.lock:
            previous_start = context.get("previous_start")
            start_interval = round(started - previous_start, 3) if previous_start is not None else None
            schedule_overrun = round(max(0, started - context.get("next_due", started)), 3) if previous_start is not None else 0
            job.update(current_page=0, current_question="", current_index=index, paused=False)
        self.event(job, f'Bắt đầu lượt {index}.')
        with self._browser(ProgressBrowser, headless=job["headless"]) as browser:
            browser.engine = job["engine"]
            browser.profile = profile
            report = browser.prepare(schema, answers)
            checkpoint()
            log.write({"event": "filled", "index": index, "report": report, "answers": answers, "profile": profile})
            if job["mode"] == "send":
                reserved = attempted = False
                try:
                    if not job["access_key_id"]:
                        self.key_store.consume_guest(job["guest_id"])
                        reserved = True
                    checkpoint()
                    log.write({"event": "started", "index": index})
                    checkpoint()
                    attempted = True
                    browser.submit(schema)
                    confirmed_at = time.monotonic()
                except (WebStopRequested, WebPauseRequested):
                    raise
                except Exception as exc:
                    if attempted:
                        log.write({"event": "unconfirmed", "index": index, "error": str(exc)})
                    raise FormError(str(exc)) from exc
                finally:
                    if reserved and not attempted:
                        try:
                            self.key_store.release_guest(job["guest_id"])
                        except FormError:
                            self.event(job, "Chưa hoàn được lượt giữ chỗ chưa gửi. Kiểm tra kết nối kho lượt.", "warning")
                log.write({"event": "confirmed", "index": index, "report": report})
            measured = getattr(browser, "metrics", {})
        completed = time.monotonic()
        elapsed = max(0, completed - started)
        with self.lock:
            job["last_start_interval_seconds"] = start_interval
            job["last_schedule_overrun_seconds"] = schedule_overrun
            context["previous_start"] = started
            if confirmed_at is not None:
                previous_confirmation = context.get("previous_confirmation")
                job["last_confirmation_interval_seconds"] = round(confirmed_at - previous_confirmation, 3) if previous_confirmation is not None else None
                context["previous_confirmation"] = confirmed_at
            job["processed"] += 1
            if job["mode"] == "send":
                job["confirmed"] += 1
            job["response_duration_total"] = job.get("response_duration_total", 0) + elapsed
            job["average_response_seconds"] = round(job["response_duration_total"] / job["processed"], 2)
            job["last_response_seconds"] = round(elapsed, 3)
            job["last_performance"] = {key: round(value, 3) if isinstance(value, float) else value for key, value in measured.items()}
            for field in schema["fields"]:
                visited = (field["id"] in report["field_ids"] if "field_ids" in report else field["page"] in report["page_indices"])
                if not visited or job["mode"] != "send":
                    continue
                job["answered_counts"][field["id"]] = job["answered_counts"].get(field["id"], 0) + 1
                if field["options"]:
                    value = answers[field["id"]]
                    selected = value if isinstance(value, list) else [value]
                    counts = job["counts"].setdefault(field["id"], {})
                    for option in selected:
                        counts[option] = counts.get(option, 0) + 1
            job["last_profile"] = profile
            context.pop("pending", None)
            interval = sample_interval(job["schedule"], context["timing_rng"])
            job["target_interval_seconds"] = interval
            job["processing_overrun_seconds"] = round(max(0, elapsed - interval), 3) if job["schedule"].get("basis", "start") == "start" else 0
            context["next_due"] = next_start_deadline(job["schedule"], started=started, completed=completed, interval=interval)
        log.write({"event": "performance", "index": index, "seconds": round(elapsed, 3),
                   "metrics": job["last_performance"], "engine": job["engine"],
                   "start_interval_seconds": job.get("last_start_interval_seconds"),
                   "confirmation_interval_seconds": job.get("last_confirmation_interval_seconds"),
                   "target_interval_seconds": job["target_interval_seconds"],
                   "processing_overrun_seconds": job["processing_overrun_seconds"],
                   "schedule_overrun_seconds": job.get("last_schedule_overrun_seconds", 0)})
        self.event(job, f'Lượt {index}: đã điền đủ {report["questions"]} câu qua {report["pages"]} trang. '
                   + ("Biểu mẫu đã xác nhận lưu." if job["mode"] == "send" else "Dừng trước Gửi, chưa gửi phản hồi."), "success")
        with self.lock:
            if control.paused:
                job.update(paused=True, pause_requested=True)
                job["_paused_remaining"] = max(0, context["next_due"] - time.monotonic())

    def stop_job(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise FormError("Không tìm thấy lượt chạy.")
            active = job["status"] in ("queued", "running", "stopping")
            if active:
                job["control"].stop()
                job["status"] = "stopping"
                executing = job.get("_executing", False)
            else:
                executing = False
        if active:
            self.browser_queue.cancel(job_id)
            if not executing:
                self._finish_job(job, "stopped")
        return self.job_state(job_id)

    def pause_job(self, job_id, paused=True):
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None or job["status"] not in ("queued", "running") or not job.get("control"):
                raise FormError("Chỉ tạm dừng hoặc tiếp tục đợt đang hoạt động.")
            if paused != job["control"].paused:
                if paused:
                    job["_paused_remaining"] = max(0, job.get("_next_due", time.monotonic()) - time.monotonic())
                    job["control"].pause()
                else:
                    job["control"].resume()
                    due = time.monotonic() + job.pop("_paused_remaining", 0)
                    job["_next_due"] = due
                    if "_execution" in job:
                        job["_execution"]["next_due"] = due
                    self.browser_queue.reschedule(job_id, due_monotonic=due)
            job.update(pause_requested=paused, paused=paused and not job.get("_executing", False),
                       waiting=False if paused else job.get("_next_due", 0) > time.monotonic())
        self.browser_queue.wake()
        return self.job_state(job_id, compact=True)


def web_settings(port=None, host=None):
    cloud = os.environ.get("KD_CLOUD", "").strip().lower() in ("1", "true", "yes") or os.environ.get("RENDER") == "true"
    try:
        port = int(port if port is not None else os.environ.get("PORT", "10000" if cloud else "8765"))
    except (TypeError, ValueError) as exc:
        raise FormError("PORT phải là số nguyên từ 0 đến 65535.") from exc
    if not 0 <= port <= 65535:
        raise FormError("PORT phải nằm từ 0 đến 65535.")
    host = host or os.environ.get("KD_HOST") or ("0.0.0.0" if cloud else "127.0.0.1")
    public_url = (os.environ.get("KD_PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL", "")).strip()
    values = [public_url] + os.environ.get("KD_ALLOWED_ORIGINS", "").split(",")
    origins = set()
    for value in values:
        value = value.strip().rstrip("/")
        if not value:
            continue
        try:
            parts = urlsplit(value)
            valid = (parts.scheme in ("http", "https") and parts.hostname and not parts.username
                     and not parts.password and not parts.path and not parts.query and not parts.fragment)
            _ = parts.port
        except ValueError:
            valid = False
        if not valid:
            raise FormError("KD_PUBLIC_URL và KD_ALLOWED_ORIGINS cần có dạng https://ten-web.example, không có đường dẫn.")
        origins.add(value)
    if cloud and not origins:
        raise FormError("Thiếu URL công khai. Trên Render dùng RENDER_EXTERNAL_URL tự cấp; nơi khác đặt KD_PUBLIC_URL.")
    return {"cloud": cloud, "host": host, "port": port, "public_url": public_url,
            "origins": origins, "secure_cookie": bool(origins) and all(value.startswith("https://") for value in origins)}


def create_web_server(port=None, force_check=False, host=None, key_store=None):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    settings = web_settings(port, host)
    key_store = key_store or configured_key_store()
    app = WebApplication(force_check=force_check, headless_only=settings["cloud"], key_store=key_store)
    access = AccessSessions(key_store)
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(30)
        def log_message(self, *args):
            pass

        def reply(self, status, data, content_type="application/json; charset=utf-8", headers=None):
            body = data.encode("utf-8") if isinstance(data, str) else json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def access_cookie(self, token, max_age):
            suffix = "; Secure" if settings["secure_cookie"] else ""
            return f"kd_access={token}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Strict{suffix}"

        def guest_identity(self):
            from http.cookies import SimpleCookie, CookieError
            try:
                cookie = SimpleCookie()
                cookie.load(self.headers.get("Cookie") or "")
                raw = cookie["kd_guest"].value if "kd_guest" in cookie else ""
            except CookieError:
                return None
            return hashlib.sha256(raw.encode()).hexdigest() if re.fullmatch(r"[A-Za-z0-9_-]{43}", raw) else None

        def access_state(self, session, fresh=False):
            usage = key_store.guest_usage(session["guest_id"], fresh=fresh)
            paid = bool(session.get("key_id"))
            return {"authenticated": paid, "plan": "unlimited" if paid else "free",
                    "key_name": session.get("key_id"), "key_used": False,
                    "free_limit": FREE_LIMIT, "used": usage["used"], "remaining": usage["remaining"],
                    "locked": not paid and usage["remaining"] == 0}

        def require_access(self):
            guest_id = self.guest_identity()
            if not guest_id or key_store.guest_usage(guest_id) is None:
                self.reply(401, {"error": "Vui lòng mở lại trang chủ để bắt đầu sử dụng."})
                return None
            paid = access.current(self.headers.get("Cookie"))
            return {"key_id": None, "key_hash": "", "activation_id": "", "key_used": False,
                    **(paid or {}), "guest_id": guest_id, "owner_id": guest_id}

        def require_owner(self, kind, identifier, session):
            with app.lock:
                collection = app.scans if kind == "scan" else app.jobs
                value = collection.get(identifier)
                if value is None or value.get("owner_id") != session["owner_id"]:
                    raise FormError("Không tìm thấy dữ liệu thuộc trình duyệt đang sử dụng.")

        def do_GET(self):
            path = urlsplit(self.path).path
            try:
                if path == "/healthz":
                    self.reply(200, {"ok": True, "name": "KDautoform"})
                    return
                if path == "/":
                    guest_id = self.guest_identity()
                    headers = {}
                    if not guest_id or key_store.guest_usage(guest_id) is None:
                        raw = secrets.token_urlsafe(32)
                        guest_id = hashlib.sha256(raw.encode()).hexdigest()
                        key_store.ensure_guest(guest_id)
                        suffix = "; Secure" if settings["secure_cookie"] else ""
                        headers["Set-Cookie"] = f"kd_guest={raw}; Path=/; Max-Age=31536000; HttpOnly; SameSite=Strict{suffix}"
                    self.reply(200, live_settings()["html"].replace("__APP_TOKEN__", app.token), "text/html; charset=utf-8",
                               headers=headers)
                    return
                if path == "/api/auth/status":
                    session = self.require_access()
                    if session is not None:
                        self.reply(200, self.access_state(session))
                    return
                session = self.require_access()
                if session is None:
                    return
                if path == "/api/state":
                    self.reply(200, {"ok": True, "force_check": app.force_check, "headless_only": app.headless_only,
                                     "version": APP_VERSION, "max_browser_workers": 10, "name": "KDautoform", "providers": ["google", "microsoft", "html"],
                                     "spss_export": spss_available(), "csv_export": True, "scheduling": True,
                                     "teaching_tools": True, "multiuser_queue": True, "browser_workers": app.browser_queue.capacity, "fast_google": True, "compact_poll": True, **self.access_state(session)})
                elif path.startswith("/api/scans/"):
                    scan_id = path.rsplit("/", 1)[-1]
                    self.require_owner("scan", scan_id, session)
                    self.reply(200, app.scan_state(scan_id))
                elif path.startswith("/api/jobs/") and path.endswith("/download"):
                    job_id = path.split("/")[3]
                    self.require_owner("job", job_id, session)
                    job = app.job_state(job_id)
                    target = Path(job.get("log_path", ""))
                    if not target.is_file():
                        raise FormError("Nhật ký chưa sẵn sàng.")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
                    self.send_header("Content-Disposition", 'attachment; filename="KDautoform_' + job_id + '.jsonl"')
                    self.send_header("Content-Length", str(target.stat().st_size))
                    self.end_headers()
                    with target.open("rb") as handle:
                        while chunk := handle.read(65536):
                            self.wfile.write(chunk)
                elif path.startswith("/api/jobs/") and path.endswith(("/export.sav", "/export.zip")):
                    job_id = path.split("/")[3]
                    self.require_owner("job", job_id, session)
                    kind = "zip" if path.endswith(".zip") else "sav"
                    target = app.export_job(job_id, kind=kind)
                    try:
                        self.send_response(200)
                        self.send_header("Content-Type", "application/octet-stream")
                        self.send_header("Content-Disposition", 'attachment; filename="KDautoform_' + job_id + '.' + kind + '"')
                        self.send_header("Content-Length", str(target.stat().st_size))
                        self.send_header("Cache-Control", "no-store")
                        self.send_header("X-Content-Type-Options", "nosniff")
                        self.end_headers()
                        with target.open("rb") as handle:
                            while chunk := handle.read(65536):
                                self.wfile.write(chunk)
                    finally:
                        target.unlink(missing_ok=True)
                elif path.startswith("/api/jobs/") and path.endswith("/report"):
                    job_id = path.split("/")[3]
                    self.require_owner("job", job_id, session)
                    self.reply(200, app.report_job(job_id))
                elif path.startswith("/api/jobs/"):
                    job_id = path.rsplit("/", 1)[-1]
                    self.require_owner("job", job_id, session)
                    self.reply(200, {**app.job_state(job_id, compact=True), **self.access_state(session)})
                else:
                    self.reply(404, {"error": "Không tìm thấy địa chỉ."})
            except FormError as exc:
                self.reply(400, {"error": str(exc)})
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_POST(self):
            origin = self.headers.get("Origin")
            expected = set(settings["origins"])
            if not settings["cloud"]:
                expected.update((f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"))
            if self.headers.get("X-Local-Token") != app.token or (origin and origin not in expected):
                # Drain small rejected bodies before closing, so Windows clients
                # receive the 403 instead of an unread-body TCP reset.
                try:
                    rejected_length = int(self.headers.get("Content-Length", "0"))
                    if 0 < rejected_length <= 65536:
                        self.rfile.read(rejected_length)
                except (ValueError, OSError):
                    pass
                self.reply(403, {"error": "Phiên truy cập không hợp lệ. Tải lại giao diện."})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 2000000:
                    raise FormError("Yêu cầu quá lớn hoặc thiếu dữ liệu.")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(payload, dict):
                    raise FormError("Dữ liệu yêu cầu không hợp lệ.")
                path = urlsplit(self.path).path
                if path == "/api/auth/login":
                    session = self.require_access()
                    if session is None:
                        return
                    key = payload.get("key", "")
                    if not isinstance(key, str) or len(key) > 256:
                        raise FormError("Định dạng key không hợp lệ.")
                    token, record, retry = access.login(key, session["guest_id"])
                    if retry:
                        self.reply(429, {"error": f"Bạn đã nhập sai nhiều lần. Thử lại sau {retry} giây."},
                                   headers={"Retry-After": str(retry)})
                    elif token:
                        self.reply(200, self.access_state({**session, **record}),
                                   headers={"Set-Cookie": self.access_cookie(token, 43200)})
                    else:
                        self.reply(401, {"error": "Key không đúng, đã bị khóa hoặc là key một lần của phiên bản cũ."})
                    return
                if path == "/api/auth/logout":
                    session = self.require_access()
                    if session is None:
                        return
                    access.logout(self.headers.get("Cookie"))
                    session["key_id"] = None
                    self.reply(200, self.access_state(session), headers={
                        "Set-Cookie": self.access_cookie("", 0)})
                    return
                session = self.require_access()
                if session is None:
                    return
                if path == "/api/scan":
                    if self.access_state(session)["locked"]:
                        raise QuotaExceeded("Bạn đã dùng hết 10 lượt miễn phí. Nhập key được cấp để tiếp tục.")
                    result = app.scan(str(payload.get("url", "")), provider=payload.get("provider", "auto"),
                                      owner_id=session["owner_id"])
                    self.reply(202, result)
                elif path.startswith("/api/scans/") and path.endswith("/preset"):
                    scan_id = path.split("/")[3]
                    self.require_owner("scan", scan_id, session)
                    self.reply(200, app.preset(scan_id, payload))
                elif path == "/api/jobs":
                    self.require_owner("scan", str(payload.get("scan_id", "")), session)
                    payload["_key_id"] = session["key_id"]
                    payload["_key_hash"] = session["key_hash"]
                    payload["_activation_id"] = session["activation_id"]
                    payload["_guest_id"] = session["guest_id"]
                    payload["_owner_id"] = session["owner_id"]
                    self.reply(202, {**app.start_job(payload), **self.access_state(session)})
                elif path.startswith("/api/jobs/") and path.endswith("/stop"):
                    job_id = path.split("/")[3]
                    self.require_owner("job", job_id, session)
                    self.reply(200, app.stop_job(job_id))
                elif path.startswith("/api/jobs/") and path.endswith(("/pause", "/resume")):
                    job_id = path.split("/")[3]
                    self.require_owner("job", job_id, session)
                    self.reply(200, {**app.pause_job(job_id, paused=path.endswith("/pause")), **self.access_state(session)})
                else:
                    self.reply(404, {"error": "Không tìm thấy thao tác."})
            except QuotaExceeded as exc:
                self.reply(402, {"error": str(exc), **self.access_state(session, fresh=True)})
            except KeyAlreadyUsed as exc:
                self.reply(403, {"error": str(exc)})
            except (FormError, ValueError, TypeError, KeyError) as exc:
                self.reply(400, {"error": str(exc)})
            except (BrokenPipeError, ConnectionResetError):
                pass
    class WebServer(ThreadingHTTPServer):
        daemon_threads = True
        request_queue_size = 128
        def server_close(self):
            app.close()
            super().server_close()
    server = WebServer((settings["host"], settings["port"]), Handler)
    server.application = app
    server.settings = settings
    return server


def start_web(port=None, open_browser=True, force_check=False, host=None):
    import signal
    import threading
    import webbrowser
    server = create_web_server(port, force_check, host)
    app = server.application
    url = server.settings["public_url"] or f"http://127.0.0.1:{server.server_port}"
    print(f"KDautoform đang chạy: {url}", flush=True)
    print("Nhấn Ctrl+C trong cửa sổ này để dừng máy chủ.", flush=True)
    if force_check:
        print("Chế độ kiểm tra: mọi lượt chạy sẽ dừng trước nút Gửi.", flush=True)
    print(f"Đang lắng nghe tại {server.settings['host']}:{server.server_port}", flush=True)
    if open_browser and not server.settings["cloud"]:
        webbrowser.open(url)
    previous_handler = None
    if threading.current_thread() is threading.main_thread():
        def stop_server(signum, frame):
            raise KeyboardInterrupt
        previous_handler = signal.signal(signal.SIGTERM, stop_server)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for job in app.jobs.values():
            if job.get("control"):
                job["control"].stop()
            else:
                job["stop_event"].set()
        server.server_close()
        if previous_handler is not None:
            signal.signal(signal.SIGTERM, previous_handler)


UX_PRINCIPLES = [['Phân cấp thị giác', 'Tiêu đề, số lượng, tỷ lệ và nút bắt đầu có cấp độ rõ ràng.'], ['Tính nhất quán', 'Dùng chung bảng màu đỏ, kiểu nút và cách trình bày câu hỏi.'], ['Căn chỉnh', 'Các trường nhập, nhãn và thẻ câu hỏi cùng một trục.'], ['Nhịp điệu khoảng cách', 'Khoảng cách được lặp lại giữa các nhóm thông tin.'], ['Nguyên lý gần nhau', 'Đáp án, thanh kéo và ô phần trăm nằm cùng một hàng.'], ['Khoảng trắng', 'Tách rõ thiết lập, câu hỏi, tóm tắt và liên hệ.'], ['Tương phản dễ đọc', 'Làm đậm chữ chức năng để giữ tương phản trên nền sáng.'], ['Thiết kế đáp ứng', 'Bố cục đổi thành một cột trên điện thoại.'], ['Định luật Fitts', 'Nút chính lớn, vùng chạm trên điện thoại được mở rộng.'], ['Định luật Hick', 'Chỉ giữ một hành động chính theo trạng thái hiện tại.'], ['Ngăn ngừa lỗi', 'Chặn chạy khi thiếu link, sai số lượng hoặc tỷ lệ chưa hợp lệ.'], ['Báo lỗi tại chỗ', 'Hiển thị tổng phần trăm và đánh dấu ngay câu chưa hợp lệ.'], ['Hiển thị trạng thái hệ thống', 'Có trạng thái đọc form, đếm ngược, trang đang điền và kết quả.'], ['Phản hồi ngay', 'Đổi tỷ lệ là cập nhật thanh phân bổ và tóm tắt tức thì.'], ['Quyền kiểm soát của người dùng', 'Cho tạm dừng tự chạy, dừng lượt đang chạy và chọn điền thử.'], ['Khả năng hoàn tác', 'Hoàn tác được thao tác ngẫu nhiên, cân bằng và sửa tỷ lệ.'], ['Nhận biết thay vì ghi nhớ', 'Hiển thị câu hỏi và đáp án thật, không buộc nhớ mã trường.'], ['Ngôn ngữ quen thuộc', 'Dùng nhãn tiếng Việt, phần trăm và số lượt dễ hiểu.'], ['Tiết lộ dần', 'Chỉ mở ô nội dung tự nhập khi chọn chế độ tương ứng.'], ['Thao tác trực tiếp', 'Có cả thanh kéo và ô số cho từng tỷ lệ.'], ['Khả năng tiếp cận', 'Có nhãn, đường dẫn bỏ qua, trạng thái live và focus bàn phím.'], ['Tôn trọng giảm chuyển động', 'Tắt hiệu ứng khi hệ thống yêu cầu giảm chuyển động.'], ['Giữ ngữ cảnh làm việc', 'Lưu link, số lượng, cấu hình đang sửa và lượt chạy hiện tại.'], ['Tìm kiếm và lọc', 'Tìm câu hỏi và lọc theo trang khi biểu mẫu dài.'], ['Xác nhận theo kết quả thật', 'Chỉ ghi nhận lượt gửi sau khi Google xác nhận lưu phản hồi.']]

def cli(argv=None):
    load_local_key_environment()
    parser = argparse.ArgumentParser(
        description="Tự điền Google Forms / Microsoft Forms công khai bằng dữ liệu giả để kiểm thử. Chỉ sửa FORM_URL.",
        epilog="--count 0 chạy đến Ctrl+C. --check điền đến trang cuối nhưng không gửi. "
               "Form yêu cầu đăng nhập, tải tệp, CAPTCHA hoặc quy tắc chưa hỗ trợ sẽ báo dừng. "
               "Form phân nhánh được điền đầy đủ theo nhánh đã chọn.")
    parser.add_argument("--web", action="store_true", help="Mở giao diện web KDautoform")
    parser.add_argument("--port", type=int, default=None, help="Cổng giao diện web; mặc định PORT hoặc 8765")
    parser.add_argument("--host", help="Địa chỉ lắng nghe; mặc định localhost, hoặc 0.0.0.0 trên cloud")
    parser.add_argument("--no-open", action="store_true", help="Không tự mở giao diện")
    parser.add_argument("--ux-report", action="store_true", help="Xem các nguyên lý UI/UX đã áp dụng")
    key_commands = parser.add_mutually_exclusive_group()
    key_commands.add_argument("--key-list", action="store_true", help="Xem danh sách key unlimited, key cũ và trạng thái khóa")
    key_commands.add_argument("--key-generate", type=int, nargs="?", const=1, metavar="SO_LUONG", help="Tạo key unlimited ngẫu nhiên và lưu CSV; mặc định 1")
    key_commands.add_argument("--key-paths", action="store_true", help="Xem vị trí kho key và thư mục CSV riêng tư")
    key_commands.add_argument("--key-add", metavar="TEN", help="Tạo và lưu key mới")
    key_commands.add_argument("--key-rotate", metavar="TEN", help="Đổi mã của một key")
    key_commands.add_argument("--key-delete", metavar="TEN", help="Xóa một key")
    key_commands.add_argument("--key-disable", metavar="TEN", help="Khóa một key")
    key_commands.add_argument("--key-enable", metavar="TEN", help="Mở lại một key")
    parser.add_argument("--url", help="Link Google Forms hoặc Microsoft Forms, thay cho FORM_URL trong mã")
    parser.add_argument("--provider", choices=("auto", "google", "microsoft", "html"), default="auto",
                        help="Mặc định tự nhận Google/Microsoft; html cho form HTML POST chuẩn thử nghiệm")
    parser.add_argument("--count", type=int, help="Số lượt; 0 = chạy liên tục")
    parser.add_argument("--engine", choices=("fast", "compatible"), default="fast", help="Cách điền Google: nhanh theo trang hoặc tương thích từng câu")
    parser.add_argument("--delay", type=float, default=2.0, help="Giây nghỉ giữa các lượt, mặc định 2")
    parser.add_argument("--seed", type=int, help="Tái tạo cùng bộ dữ liệu ngẫu nhiên")
    parser.add_argument("--headless", action="store_true", help="Chạy trình duyệt ẩn")
    parser.add_argument("--check", action="store_true", help="Điền thử qua mọi trang, dừng trước Gửi")
    parser.add_argument("--inspect", action="store_true", help="Xem cấu trúc và tỷ lệ dự kiến")
    parser.add_argument("--resume", type=Path, help="Tiếp tục nhật ký cũ, bỏ qua lượt đã xác nhận")
    parser.add_argument("--log", type=Path, help="Đường dẫn nhật ký; mặc định lưu ngoài thư mục mã nguồn")
    parser.add_argument("--spss-out", type=Path, help="Xuất phản hồi đã xác nhận sang .sav sau khi đợt gửi hoàn tất")
    parser.add_argument("--setup", action="store_true", help="Cài các thư viện trong requirements.txt và Chromium, rồi thoát")
    args = parser.parse_args(argv)
    if args.key_generate is not None or args.key_paths or any(getattr(args, name) for name in ("key_list", "key_add", "key_rotate", "key_delete", "key_disable", "key_enable")):
        try:
            return manage_access_keys(args)
        except (FormError, OSError, ValueError) as exc:
            print("Không cập nhật được key: " + str(exc), file=sys.stderr)
            return 2
    if args.setup:
        try:
            setup()
            print("Đã cài xong. Dán link vào FORM_URL rồi chạy python google_form_tool.py")
            return 0
        except subprocess.CalledProcessError as exc:
            print("Cài đặt chưa hoàn tất: " + str(exc), file=sys.stderr)
            return 2
    if not math.isfinite(args.delay) or args.delay < 0:
        parser.error("--delay phải là số không âm.")
    if args.count is not None and args.count < 0:
        parser.error("--count phải từ 0 trở lên.")
    if args.check and args.inspect:
        parser.error("Chọn --check hoặc --inspect.")
    if args.spss_out and (args.check or args.inspect or args.web):
        parser.error("--spss-out dùng cho đợt gửi CLI; giao diện web có nút Tải SPSS riêng.")
    if args.resume and (args.url or args.count is not None or args.seed is not None or args.log or args.check or args.inspect):
        parser.error("--resume chỉ dùng cùng --delay và --headless.")
    if args.ux_report:
        for index, (principle, implementation) in enumerate(UX_PRINCIPLES, 1):
            print(f"{index:02d}. {principle}: {implementation}")
        return 0
    if args.web:
        if args.port is not None and not 0 <= args.port <= 65535:
            parser.error("--port phải nằm từ 0 đến 65535.")
        try:
            start_web(args.port, open_browser=not args.no_open, force_check=args.check, host=args.host)
            return 0
        except (OSError, FormError) as exc:
            print("Không mở được giao diện: " + str(exc), file=sys.stderr)
            return 2
    log = None
    try:
        completed = 0
        if args.resume:
            log = RunLog(args.resume)
            header, completed = log.read()
            url, count, seed = header["url"], header["count"], header["seed"]
        else:
            url = args.url or FORM_URL
            if not url.strip():
                raise FormError("Dán link vào dòng FORM_URL đầu file, hoặc dùng --url LINK.")
            seed = args.seed if args.seed is not None else secrets.randbits(64)
            count = args.count
            if count is None and (args.check or args.inspect):
                count = 1
            if count is None:
                count = int(input("Số lượt cần điền (0 = chạy liên tục, Ctrl+C để dừng): ").strip())
            if count < 0:
                raise FormError("Số lượt phải từ 0 trở lên.")
        url = form_link(url, allow_generic=args.provider == "html" or (args.resume and header.get("provider") == "html"))
        with form_browser(url)(headless=args.headless) as bot:
            schema = bot.load(url)
            bot.engine = args.engine if schema.get("provider", "google") == "google" else "compatible"
            sampler = Sampler(schema, seed, header.get("reference_date") if args.resume else None)
            print(f'\n{schema["title"]}\nĐã nhận diện {len(schema["pages"])} trang, '
                  f'{schema["question_count"]} câu hỏi, {len(schema["fields"])} mục trả lời.')
            print(f"Seed: {seed}")
            if args.inspect:
                inspect_form(schema, sampler)
                return 0
            if args.resume:
                if header.get("schema_hash") != fingerprint(schema):
                    raise FormError("Form đã thay đổi so với nhật ký, không thể tiếp tục phiên cũ.")
            elif not args.check:
                if args.log and args.log.exists():
                    raise FormError("Nhật ký đã tồn tại. Dùng --resume hoặc chọn tên mới.")
                path = args.log or Path(tempfile.gettempdir()) / "google_form_tool" / (
                    datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + secrets.token_hex(4) + ".jsonl")
                log = RunLog(path)
                log.write({"event": "run", "synthetic": True, "url": schema["url"],
                           "seed": seed, "count": count, "schema_hash": fingerprint(schema),
                           "reference_date": sampler.reference_date.isoformat(), "provider": schema.get("provider", "google"), "engine": bot.engine})
            if log:
                print(f"Nhật ký: {log.path.resolve()}")
            for index in itertools.count(completed + 1):
                if count and index > count:
                    break
                if index > completed + 1 and args.delay:
                    time.sleep(args.delay)
                answers = sampler.sample(index)
                bot.profile = sampler.last_profile
                response_started = time.perf_counter()
                report = bot.prepare(schema, answers)
                print(f'Lượt {index}: đã điền {report["questions"]} câu / '
                      f'{report["fields"]} mục qua {report["pages"]} trang.', flush=True)
                if args.check:
                    print(f"Thời gian điền thử: {time.perf_counter() - response_started:.2f} giây. Đã đến nút Gửi cuối cùng; chưa gửi phản hồi.", flush=True)
                    if count == 0:
                        break
                    continue
                log.write({"event": "filled", "index": index, "report": report, "answers": answers,
                           "profile": sampler.last_profile})
                log.write({"event": "started", "index": index})
                try:
                    bot.submit(schema)
                except Exception as exc:
                    log.write({"event": "unconfirmed", "index": index, "error": str(exc)})
                    raise
                log.write({"event": "confirmed", "index": index, "report": report})
                log.write({"event": "performance", "index": index, "seconds": round(time.perf_counter() - response_started, 3), "metrics": bot.metrics, "engine": bot.engine})
                print(f"[{index}/{count if count else 'liên tục'}] Biểu mẫu xác nhận đã lưu.", flush=True)
            print("Hoàn tất.")
            if args.spss_out:
                from spss_export import export_spss
                target = export_spss(schema, log.path, args.spss_out)
                print(f"Dữ liệu SPSS: {target.resolve()}")
        return 0
    except (KeyboardInterrupt, EOFError):
        print("\nĐã dừng. Các lượt đã được xác nhận nằm trong nhật ký.", file=sys.stderr)
        if log:
            print(f'Chạy tiếp: python google_form_tool.py --resume "{log.path.resolve()}"', file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Lỗi: {exc}", file=sys.stderr)
        if log:
            print(f"Kiểm tra nhật ký: {log.path.resolve()}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    raise SystemExit(cli())





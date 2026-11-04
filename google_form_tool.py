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


def live_settings():
    import ast
    global _LIVE_SETTINGS_CACHE
    path = Path(__file__).resolve()
    stat = path.stat()
    stamp = (stat.st_mtime_ns, stat.st_size)
    if _LIVE_SETTINGS_CACHE["stamp"] == stamp:
        return _LIVE_SETTINGS_CACHE
    try:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        values = {}
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id in ("LEGACY_ACCESS_KEYS", "WEB_HTML"):
                        values[target.id] = ast.literal_eval(node.value)
        registry = values["LEGACY_ACCESS_KEYS"]
        if not isinstance(registry, dict):
            raise ValueError
        for name, item in registry.items():
            if (not isinstance(name, str) or not isinstance(item, dict)
                    or not re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256", "")))
                    or type(item.get("enabled")) is not bool):
                raise ValueError
        if not isinstance(values["WEB_HTML"], str):
            raise ValueError
        _LIVE_SETTINGS_CACHE = {"stamp": stamp, "keys": registry, "html": values["WEB_HTML"]}
        return _LIVE_SETTINGS_CACHE
    except (ValueError, KeyError, SyntaxError, TypeError) as exc:
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


def make_profile(schema, rng, index, reference_date):
    context = plain(schema.get("title", "") + " " + " ".join(field["question"] for field in schema["fields"]))
    student = any(word in context for word in ("sinh vien", "hoc tap", "student", "dai hoc", "hoc vien"))
    student_year = rng.randint(1, 4)
    age = 17 + student_year + rng.randrange(2) if student else rng.randint(20, 35)
    for field in schema["fields"]:
        if text_intent(field) == "age":
            numeric = [rule for rule in field.get("rules", []) if rule[0] == 1]
            if numeric:
                candidates = [candidate for candidate in range(12, 81)
                              if all(rule_valid(str(candidate), rule) for rule in numeric)]
                if candidates and age not in candidates:
                    age = rng.choice(candidates)
    gender = rng.choice(["Nam", "Nữ"])
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
    for field in schema["fields"]:
        intent = text_intent(field)
        if intent in ("school", "faculty") and field["kind"] in ("single", "dropdown") and field["options"]:
            profile[intent] = rng.choice(field["options"])
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
        rng = random.Random(seed)
        self.weights = {field["id"]: preference(field, rng) for field in schema["fields"]}

    def sample(self, index):
        rng = random.Random(f"{self.seed}:{index}")
        profile = make_profile(self.schema, rng, index, self.reference_date)
        result, used_columns = {}, defaultdict(set)
        groups = Counter(field["item_id"] for field in self.schema["fields"])
        for field in self.schema["fields"]:
            kind = field["kind"]
            if kind in ("text", "paragraph"):
                value = sample_text(field, rng, index, profile)
            elif kind == "date":
                value = (dict(profile["birth_date"]) if text_intent(field) == "birth_date" else
                         {"year": self.reference_date.year, "month": rng.randint(1, 12), "day": rng.randint(1, 28),
                          "hour": rng.randint(8, 17), "minute": rng.randrange(60)})
            elif kind == "time":
                value = {"hour": rng.randint(0, 5) if field.get("duration") else rng.randint(0, 23),
                         "minute": rng.randrange(60), "second": rng.randrange(60)}
            else:
                options = field["options"]
                weights = self.weights[field["id"]]
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
        if self.session is not None:
            self.session.close()
        self.session = self.browser.new_context(locale="vi-VN")
        self.page = self.session.new_page()
        self.page.set_default_timeout(15000)
        self.page.set_default_navigation_timeout(45000)

    def read_schema(self):
        url = form_link(self.page.url, resolved=True)
        try:
            data = self.page.evaluate("Array.isArray(window.FB_PUBLIC_LOAD_DATA_) ? window.FB_PUBLIC_LOAD_DATA_ : null")
            if data is None:
                self.page.wait_for_function("() => Array.isArray(window.FB_PUBLIC_LOAD_DATA_)", timeout=10000)
                data = self.page.evaluate("window.FB_PUBLIC_LOAD_DATA_")
        except Exception as exc:
            raise FormError("Không truy cập được câu hỏi. Form có thể đã đóng hoặc cần đăng nhập.") from exc
        return parse_schema(data, url)

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
        if getattr(self, "engine", "fast") == "fast":
            from google_fast import fill_google_page, FastFillError
            for field in fields:
                self.before_field(field)
            try:
                result = fill_google_page(self.page, fields, answers)
                handled.update(result["handled"])
            except FastFillError:
                pass
        for field in fields:
            if field["id"] not in handled:
                self.fill_field(field, answers[field["id"]])
        self.metrics["batch_fields"] = self.metrics.get("batch_fields", 0) + len(handled)
        self.metrics["fallback_fields"] = self.metrics.get("fallback_fields", 0) + len(fields) - len(handled)
        self.metrics["fill_ms"] = self.metrics.get("fill_ms", 0) + (time.perf_counter() - started) * 1000

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
        visited, seen = [], set()
        for _ in range(len(schema["pages"]) + 1):
            fast = getattr(self, "engine", "fast") == "fast"
            if fast:
                navigation = self.navigation_state()
                history = navigation["history"]
                if history is None:
                    self.page.wait_for_function("() => !!document.querySelector('input[name=pageHistory]')", timeout=10000)
                    history = self.navigation_state()["history"]
            else:
                history_node = self.page.locator('input[name="pageHistory"]')
                history_node.wait_for(state="attached")
                history = history_node.input_value()
            page_index = int(history.split(",")[-1])
            if page_index in visited or not 0 <= page_index < len(schema["pages"]):
                raise FormError("Phân trang tạo vòng lặp hoặc không xác định.")
            visited.append(page_index)
            fields = [field for field in schema["fields"] if field["page"] == page_index]
            self.fill_page(fields, answers)
            seen.update(field["id"] for field in fields)
            for control in self.page.locator('input[name="emailAddress"]:visible').all():
                if not control.input_value():
                    control.fill((getattr(self, "profile", None) or {}).get("email", "test.user@example.com"))
            if fast:
                navigation = self.navigation_state()
                has_next, has_submit = navigation["next"], navigation["submit"]
            else:
                next_button = self.button(("Tiếp", "Next"))
                has_next = next_button is not None
                has_submit = self.button(("Gửi", "Submit")) is not None if not has_next else False
            if not has_next:
                if not has_submit:
                    raise FormError("Không tìm thấy nút Tiếp/Gửi. Kiểm tra đăng nhập hoặc CAPTCHA.")
                expected = {field["id"] for field in schema["fields"]
                            if not schema["branching"] or field["page"] in visited}
                if not expected <= seen:
                    raise FormError("Chưa điền đủ câu hỏi trên các trang cần đi qua.")
                if not schema["branching"] and len(visited) != len(schema["pages"]):
                    raise FormError("Chưa đi qua đủ toàn bộ trang của form.")
                question_count = len({field["item_id"] for field in schema["fields"] if field["id"] in seen})
                self.report = {"pages": len(visited), "page_indices": visited,
                               "questions": question_count, "fields": len(seen)}
                self.metrics["prepare_ms"] = (time.perf_counter() - started) * 1000
                return self.report
            moved = time.perf_counter()
            if fast:
                self.click_navigation("next")
            else:
                self.before_navigation()
                next_button.click()
            try:
                self.page.wait_for_function("""old => {
                    const input=document.querySelector('input[name="pageHistory"]');
                    return input && input.value !== old;
                }""", arg=history, timeout=25000)
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


WEB_HTML = r"""<!doctype html>
<html lang="vi">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light">
<meta name="description" content="KDautoform: Google Forms và Microsoft Forms, tùy chỉnh tỷ lệ, xuất SPSS .sav, 10 lượt miễn phí. Nhập key để tiếp tục sau khi hết lượt.">
<title>KDautoform — Google Forms, Microsoft Forms &amp; SPSS</title>
<style>
:root{--red:#c52c38;--red-deep:#a71e2a;--red-soft:#fff0ef;--ink:#282128;--muted:#776870;--line:#eee6e6;--paper:#fff;--canvas:#fbf8f7;--green:#236f58;--radius:20px}
*{box-sizing:border-box}body{margin:0;background:var(--canvas);font:14px/1.55 "Segoe UI",Arial,sans-serif;color:var(--ink);-webkit-font-smoothing:antialiased}button,input,select,textarea{font:inherit}button,a,input,select,textarea{-webkit-tap-highlight-color:transparent}button,a{touch-action:manipulation}button{cursor:pointer}button:disabled{cursor:not-allowed;opacity:.45}button:focus-visible,a:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible{outline:3px solid #efa0a4;outline-offset:3px}a{color:inherit;text-decoration:none}button{border:0}button svg{flex-shrink:0}svg.icon{width:18px;height:18px;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round;vertical-align:middle}.defs{position:absolute;width:0;height:0;overflow:hidden}
.header{height:82px;background:#fffdfc;border-bottom:1px solid var(--line);display:flex;align-items:center}.header-inner{max-width:1260px;margin:auto;width:100%;padding:0 34px;display:flex;align-items:center;justify-content:space-between;gap:20px}.brand{display:flex;align-items:center;gap:11px;letter-spacing:-1px;font-size:25px;font-weight:750}.brand-mark{width:38px;height:40px;border-radius:11px;background:var(--red);display:grid;place-items:center;color:white;box-shadow:0 5px 14px #c52c3825}.brand-mark svg{width:23px;height:23px}.brand span:last-child{color:var(--red)}.contact-nav{display:flex;gap:10px;align-items:center}.contact-note{color:var(--muted);font-size:12px;margin-right:4px}.contact-link{height:37px;display:flex;align-items:center;justify-content:center;gap:7px;padding:0 13px;border:1px solid var(--line);border-radius:9px;background:white;font-size:12px;font-weight:600;transition:.18s}.contact-link:hover{border-color:#d9969b;color:var(--red);background:#fff8f7}.zalo-mark{font-size:11px;letter-spacing:-.5px;font-weight:800;border:1.5px solid currentColor;border-radius:5px;padding:0 2px;line-height:17px}.fb-mark{font-family:Arial;font-size:19px;font-weight:bold}
main{max-width:1260px;padding:42px 34px 48px;margin:auto}.intro{display:flex;justify-content:space-between;align-items:flex-end;margin-bottom:30px;gap:20px}.eyebrow{display:flex;gap:7px;align-items:center;color:var(--red);font-size:11px;font-weight:750;letter-spacing:1.8px;margin-bottom:13px}.eyebrow:before{content:"";width:6px;height:6px;background:var(--red);border-radius:50%}h1{font-size:38px;line-height:1.23;letter-spacing:-1.45px;margin:0;font-weight:730}h1 span{color:var(--red)}.intro p{margin:13px 0 0;color:var(--muted);font-size:14px;max-width:600px}.local-badge{display:flex;align-items:center;gap:7px;color:#756970;white-space:nowrap;font-size:11px;background:#f2ecea;padding:8px 12px;border-radius:30px;margin-bottom:3px}.local-badge i{height:6px;width:6px;background:#40967c;border-radius:50%;box-shadow:0 0 0 3px #40967c13}
.panel{border:1px solid var(--line);border-radius:var(--radius);background:white;box-shadow:0 4px 25px #43272103}.setup{padding:25px 27px 19px;margin-bottom:29px}.panel-head{display:flex;align-items:center;gap:11px;margin-bottom:20px}.step{background:var(--red-soft);color:var(--red);font-size:11px;font-weight:700;height:29px;width:29px;display:grid;place-items:center;border-radius:8px}.panel-head h2{font-size:16px;margin:0;letter-spacing:-.25px}.panel-head .tiny{margin-left:auto;color:var(--muted);font-size:11px}.input-grid{display:grid;grid-template-columns:minmax(0,1fr) 165px;gap:18px}label.label{font-size:12px;font-weight:650;display:block;margin-bottom:8px}.url-wrap{height:51px;display:flex;align-items:center;border:1px solid #dfd6d7;border-radius:10px;padding:0 12px;gap:10px;transition:.2s}.url-wrap:focus-within{border-color:var(--red);box-shadow:0 0 0 3px #c52c380c}.url-wrap .icon{color:#76636c}.url-wrap input{border:0;outline:none!important;min-width:0;flex:1;color:var(--ink);background:transparent;font-size:13px;height:100%}.url-wrap input::placeholder{color:#76636c}.scan-again{background:#f7f2f1;color:#77686c;font-size:11px;white-space:nowrap;border-radius:6px;padding:5px 9px}.scan-again:hover{background:#fce5e4;color:var(--red)}.quantity{display:flex;height:51px;border:1px solid #dfd6d7;border-radius:10px;padding:4px;gap:1px;align-items:center}.quantity button{height:35px;width:30px;background:#faf5f4;color:#984a59;border-radius:6px;font-size:18px}.quantity input{border:0;outline:none!important;text-align:center;min-width:0;width:100%;font-size:18px;font-weight:650;color:var(--ink);-moz-appearance:textfield}.quantity input::-webkit-inner-spin-button{display:none}.setup-bottom{display:flex;align-items:center;justify-content:space-between;margin-top:12px;gap:12px}.scan-status{font-size:11px;display:flex;align-items:center;gap:7px;color:var(--muted)}.scan-status .icon{width:14px;height:14px}.scan-status.success{color:var(--green)}.scan-status.error{color:var(--red)}.scan-status.loading{color:var(--red)}.setup-tip{font-size:11px;color:#76636c}.spinner{height:14px;width:14px;display:inline-block;border:2px solid #edc2c5;border-top-color:var(--red);border-radius:50%;animation:spin .75s linear infinite}@keyframes spin{to{transform:rotate(360deg)}}
.workspace{display:grid;grid-template-columns:minmax(0,1fr) 310px;gap:24px;align-items:start}.questions{min-width:0}.section-title{display:flex;align-items:center;gap:9px;margin:0 0 14px}.section-title h2{font-size:18px;letter-spacing:-.4px;margin:0}.count-badge{font-size:11px;color:#984a59;background:#eee7e6;padding:3px 8px;border-radius:20px;font-weight:600}.section-title .ghost-button{margin-left:auto}.ghost-button,.small-button{display:inline-flex;align-items:center;gap:6px;background:transparent;color:#984a59;font-size:11px;padding:6px 8px;border-radius:7px;white-space:nowrap}.ghost-button:hover,.small-button:hover{background:#f7ebe9;color:var(--red)}.ghost-button .icon,.small-button .icon{width:14px;height:14px}.red-outline{border:1px solid #e4c5c8;color:var(--red);background:#fffafa;padding:7px 10px}.form-meta{margin-bottom:15px;padding:13px 16px;background:#f2ebea;border-radius:11px;border:1px solid #ede3e1}.form-meta strong{font-size:12px;font-weight:650;display:block;line-height:1.55}.form-meta small{display:block;margin-top:4px;color:#76636c;font-size:11px}.toolbar{display:flex;gap:8px;margin-bottom:16px}.search-wrap{display:flex;align-items:center;gap:7px;flex:1;min-width:0;background:white;border:1px solid var(--line);border-radius:9px;padding:8px 10px;color:#984a59}.search-wrap .icon{width:15px;height:15px}.search-wrap input{width:100%;min-width:0;border:0;outline:none!important;font-size:11px;background:transparent}.page-select{max-width:118px;border:1px solid var(--line);background:white;color:#7f6d75;border-radius:9px;padding:0 8px;font-size:11px}.toolbar[hidden],.form-meta[hidden]{display:none}
.empty{padding:58px 30px 52px;min-height:330px;text-align:center;display:flex;flex-direction:column;align-items:center;justify-content:center}.empty-art{width:164px;height:112px;margin-bottom:20px}.empty h3{font-size:16px;margin:0 0 8px;letter-spacing:-.3px}.empty p{font-size:12px;max-width:310px;color:#76636c;margin:0;line-height:1.8}.empty-foot{display:flex;gap:18px;margin-top:24px;font-size:11px;color:#984a59}.empty-foot span{display:flex;align-items:center;gap:5px}.empty-foot .icon{height:12px;width:12px}
.question-card{padding:21px 23px 15px;margin-bottom:14px;transition:border-color .15s}.question-card.invalid{border-color:#eaa8ad}.q-top{display:flex;align-items:center;gap:7px;flex-wrap:wrap}.q-label{font-size:11px;font-weight:700;letter-spacing:.2px;color:#b6505b;background:#fbedea;border-radius:5px;padding:3px 7px}.q-kind{color:#984a59;font-size:11px}.required{margin-left:2px;color:#984a59;font-size:11px}.q-top .small-button{margin-left:auto;padding-right:0}.question-card h3{font-size:13px;font-weight:650;line-height:1.65;margin:12px 0 0;letter-spacing:-.08px}.row-name{font-size:12px;color:#984a59;line-height:1.6;margin:5px 0 0}.answers{margin-top:19px}.answer-row{display:grid;grid-template-columns:minmax(0,1fr) 114px 66px;gap:14px;align-items:center;margin:13px 0}.answer-label{display:flex;align-items:flex-start;gap:8px;font-size:11px;color:#76656d;min-width:0;line-height:1.6}.option-dot{height:7px;width:7px;min-width:7px;border-radius:3px;margin-top:5px}.rate-range{width:100%;accent-color:var(--red);height:3px;cursor:pointer;margin:0}.rate-box{display:flex;align-items:center;gap:3px;background:#fcf9f8;border:1px solid #e8dedf;border-radius:7px;padding:4px 6px;height:33px}.rate-box input{min-width:0;width:100%;border:0;background:transparent;outline:none!important;text-align:right;font-weight:650;font-size:11px;color:#594750;-moz-appearance:textfield}.rate-box input::-webkit-inner-spin-button{display:none}.rate-box span{font-size:11px;color:#984a59}.rate-stack{height:5px;display:flex;border-radius:5px;overflow:hidden;gap:2px;background:#f3edec;margin:17px 0 10px}.rate-stack i{height:100%;min-width:0;transition:width .2s}.q-bottom{display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;font-size:11px;color:#984a59}.sum{color:var(--green);font-weight:650;display:flex;gap:4px;align-items:center}.sum .icon{width:11px;height:11px}.invalid .sum{color:var(--red)}.q-bottom .small-button{font-size:11px;padding:2px 0}.text-mode{margin-top:16px;display:flex;align-items:center;gap:8px;padding:12px;background:#fcf8f7;border:1px dashed #e8dcde;border-radius:9px;color:#984a59;font-size:11px}.text-mode .icon{height:17px;width:17px;color:#984a59}.text-mode select{margin-left:auto;max-width:145px;font-size:11px;background:white;border:1px solid var(--line);border-radius:6px;padding:5px;color:#7e6973}.fixed-text{margin-top:10px;width:100%;min-height:76px;resize:vertical;border:1px solid #e3d6d9;border-radius:8px;padding:10px;font-size:12px;line-height:1.65;color:#5c4650;outline:none}.no-results{text-align:center;padding:35px;color:#984a59;font-size:12px}
.summary{position:sticky;top:22px;overflow:hidden}.summary-top{padding:24px 23px 19px;background:linear-gradient(135deg,#fff9f7,#fff0ef);border-bottom:1px solid #f2dbdd}.summary-kicker{font-size:11px;letter-spacing:.4px;color:#984a59;font-weight:600;display:flex;gap:6px;align-items:center}.summary-kicker .icon{height:14px;width:14px}.big-count{font-size:48px;line-height:1.2;font-weight:720;letter-spacing:-2px;color:#8f2230;margin-top:14px;font-variant-numeric:tabular-nums}.big-count small{font-size:12px;font-weight:450;letter-spacing:0;color:#984a59;margin-left:8px}.summary-top p{margin:9px 0 0;color:#984a59;font-size:11px;line-height:1.8}.summary-body{padding:20px 23px}.summary-row{display:flex;justify-content:space-between;align-items:center;font-size:11px;color:#984a59;margin-bottom:13px}.summary-row strong{font-size:11px;color:#715661;font-weight:650}.summary-row strong.good{color:var(--green)}.summary-row strong.bad{color:var(--red)}.summary-separator{height:1px;background:var(--line);margin:17px 0}.toggle-line{display:flex;align-items:center;justify-content:space-between;margin:13px 0;color:#984a59;font-size:11px;gap:10px}.switch{position:relative;width:29px;height:17px;min-width:29px}.switch input{opacity:0;width:0;height:0}.switch span{position:absolute;inset:0;background:#dfd4d5;border-radius:20px;transition:.2s;cursor:pointer}.switch span:before{content:"";position:absolute;width:11px;height:11px;background:white;border-radius:50%;left:3px;top:3px;transition:.2s;box-shadow:0 1px 3px #0001}.switch input:checked+span{background:var(--red)}.switch input:checked+span:before{transform:translateX(12px)}.switch input:focus-visible+span{outline:3px solid #efa0a4;outline-offset:3px}.countdown{background:#fcf2f0;border:1px solid #f2dcd8;color:#984a59;padding:10px 11px;border-radius:9px;font-size:11px;line-height:1.7;margin:15px 0 12px}.countdown button{background:transparent;padding:0;margin-left:3px;color:var(--red);font-size:11px;text-decoration:underline}.primary{width:100%;height:46px;display:flex;gap:8px;align-items:center;justify-content:center;border-radius:10px;background:var(--red);color:white;font-size:12px;font-weight:650;box-shadow:0 4px 11px #c52c3814;transition:.18s}.primary:hover:not(:disabled){background:var(--red-deep);transform:translateY(-1px)}.primary.stop{background:#6a3540;box-shadow:none}.primary .icon{height:15px;width:15px}.test-run{display:block;margin:11px auto 0;padding:4px 8px;background:transparent;color:#984a59;font-size:11px}.test-run:hover{color:var(--red)}.test-run .icon{height:12px;width:12px;margin-right:3px}.test-note{font-size:11px;text-align:center;margin-top:14px;color:#984a59}.job-panel{padding:18px 23px 21px;border-top:1px solid var(--line);background:#fffdfc}.job-head{display:flex;justify-content:space-between;align-items:center;font-size:11px;font-weight:650;gap:8px}.status-chip{font-size:11px;padding:3px 6px;letter-spacing:.3px;border-radius:4px;background:#f7e9e7;color:#a05a65}.status-chip.completed{background:#e9f5ee;color:#76636c}.status-chip.error{background:#fde6e7;color:var(--red)}.job-numbers{margin-top:15px;display:flex;align-items:baseline;gap:5px;color:#984a59;font-size:11px}.job-numbers strong{font-size:27px;font-weight:650;color:#73434f;letter-spacing:-.9px}.job-bar{height:5px;background:#f0e5e5;border-radius:8px;margin:10px 0;overflow:hidden}.job-bar i{display:block;height:100%;background:var(--red);width:0;transition:width .25s}.job-bar.indeterminate i{width:30%;animation:slide 1.5s ease-in-out infinite}@keyframes slide{50%{transform:translateX(220%)}}.job-detail{font-size:11px;color:#984a59;line-height:1.65;min-height:25px}.events{border-top:1px solid #f0e7e7;margin-top:13px;padding-top:5px;max-height:145px;overflow:auto}.event{font-size:11px;color:#984a59;padding:6px 0;line-height:1.6;display:flex;gap:6px}.event:before{content:"";width:4px;height:4px;min-width:4px;border-radius:50%;background:#d8c4c8;margin-top:6px}.event.success:before{background:#59a38a}.event.error{color:#984a59}.event.error:before{background:var(--red)}.download{font-size:11px;color:var(--red);display:inline-flex;align-items:center;gap:5px;margin-top:8px}.download .icon{width:12px;height:12px}
.contact-footer{margin-top:44px;border-top:1px solid #e8dede;padding-top:23px;display:flex;justify-content:space-between;align-items:center;gap:15px}.footer-copy strong{display:block;font-size:12px;color:#984a59;font-weight:600}.footer-copy p{margin:4px 0 0;font-size:11px;color:#984a59}.footer-links{display:flex;gap:9px}.footer-links a{font-size:11px;padding:7px 10px;display:flex;gap:6px;align-items:center;color:#984a59;border:1px solid #ead9dc;border-radius:7px;background:#fffcfa}.footer-links a:hover{background:#fcebe9}.copyright{text-align:center;margin-top:25px;font-size:11px;color:#984a59;letter-spacing:.25px}.toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);padding:12px 19px;background:#50343e;color:white;box-shadow:0 6px 28px #4d1b3820;border-radius:11px;font-size:12px;z-index:20;max-width:90vw;transition:.2s}.toast[hidden],[hidden]{display:none!important}.loading-skeleton{height:220px;background:linear-gradient(110deg,#fff 30%,#fcf4f2 45%,#fff 60%);background-size:200% 100%;animation:skeleton 1.3s infinite;border:1px solid var(--line);border-radius:20px}@keyframes skeleton{to{background-position:-200% 0}}
@media(min-width:1450px){main{padding-top:49px}.intro{margin-bottom:35px}h1{font-size:42px}.header-inner{padding:0 34px}}
@media(max-width:1050px){.workspace{grid-template-columns:minmax(0,1fr) 280px;gap:18px}main{padding:32px 22px}.header-inner{padding:0 22px}.summary-top,.summary-body,.job-panel{padding-left:19px;padding-right:19px}.question-card{padding:18px}.answer-row{grid-template-columns:minmax(0,1fr) 85px 60px;gap:9px}.contact-note{display:none}h1{font-size:34px}.local-badge{font-size:11px}.intro p{font-size:12px}}
@media(max-width:790px){.header{height:69px}.brand{font-size:22px}.brand-mark{width:33px;height:35px;border-radius:9px}.header-inner{padding:0 18px}.contact-link{height:33px;padding:0 10px;font-size:11px}.contact-link .phone-label{display:none}.contact-nav{gap:7px}main{padding:28px 17px}.intro{display:block;margin-bottom:23px}h1{font-size:32px;letter-spacing:-1px}.intro p{max-width:400px;font-size:12px}.local-badge{display:inline-flex;margin-top:15px}.eyebrow{font-size:11px;margin-bottom:11px}.setup{padding:20px 18px 16px;margin-bottom:22px}.panel-head{margin-bottom:15px}.panel-head .tiny{display:none}.input-grid{grid-template-columns:minmax(0,1fr) 110px;gap:12px}.scan-again{display:none}.url-wrap{padding:0 9px;gap:6px}.url-wrap input{font-size:11px}.setup-tip{display:none}.quantity button{width:21px;height:31px}.quantity input{font-size:16px}.workspace{display:flex;flex-direction:column;gap:19px}.summary{position:static;width:100%;order:0}.questions{width:100%;order:1}.summary-top{padding:17px 19px;display:grid;grid-template-columns:1fr auto;align-items:center}.summary-kicker{grid-column:1}.big-count{grid-column:2;grid-row:1/3;font-size:34px;margin:0}.big-count small{font-size:11px}.summary-top p{font-size:11px;margin:5px 0 0;grid-column:1;max-width:235px}.summary-body{padding:16px 19px}.summary-rows{display:flex;gap:20px;justify-content:space-between}.summary-row{flex-direction:column;align-items:flex-start;gap:4px;margin-bottom:5px;font-size:11px}.summary-row strong{font-size:11px}.summary-separator{margin:13px 0}.toggle-line{margin:9px 0;font-size:11px}.summary-controls{display:flex;gap:20px}.summary-controls .toggle-line{flex:1}.countdown{margin:12px 0 10px}.primary{height:42px}.test-note{margin-top:9px}.job-panel{padding:15px 19px}.events{max-height:95px}.section-title h2{font-size:17px}.section-title .ghost-button{font-size:11px}.toolbar{gap:7px}.page-select{max-width:100px;font-size:11px}.question-card{padding:18px 17px 13px}.answer-row{grid-template-columns:minmax(0,1fr) 66px;gap:5px 10px;margin:14px 0}.answer-label{font-size:11px}.rate-box{grid-column:2;grid-row:auto}.rate-range{grid-row:auto;grid-column:1/-1;margin:7px 0 3px}.answers{margin-top:14px}.q-top .small-button{font-size:11px}.q-kind,.required{font-size:11px}.question-card h3{font-size:13px}.row-name{font-size:11px}.contact-footer{margin-top:28px;flex-direction:column;align-items:flex-start;gap:14px}.footer-links{width:100%}.footer-links a{flex:1;justify-content:center;font-size:11px}.text-mode{font-size:11px}.text-mode select{max-width:130px}.q-bottom{font-size:11px}.q-bottom>span:first-child{max-width:63%}}
.skip-link{position:fixed;left:15px;top:-70px;z-index:50;background:var(--red);color:white;padding:12px 18px;border-radius:8px}.skip-link:focus{top:12px}.ghost-button,.small-button{min-height:32px}.contact-link{min-height:37px}.rate-range{min-height:20px}.summary-row strong{font-variant-numeric:tabular-nums}@media(max-width:790px){.ghost-button,.small-button,.contact-link,.test-run{min-height:44px}.q-top .small-button{padding:8px}.input-grid{grid-template-columns:minmax(0,1fr) 132px}.quantity button{width:34px;min-width:34px;height:40px}.primary{min-height:46px}.rate-box{height:40px}.summary-controls{gap:12px}.page-select{min-height:40px}.text-mode{flex-wrap:wrap}.toolbar{flex-wrap:wrap}.search-wrap{min-height:40px}.q-bottom>span:first-child{max-width:100%}}
@media(max-width:790px){.setup-bottom{flex-wrap:wrap}.setup-tip{display:block}}
@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important;scroll-behavior:auto!important}}

.access-locked .skip-link{display:none}.access-gate{min-height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:35px 20px;background:radial-gradient(ellipse at 50% 0%,#fce9e5 0%,transparent 55%),var(--canvas)}.access-card{width:100%;max-width:460px;border:1px solid #ecdfe0;border-radius:24px;background:#fff;padding:36px 36px 29px;box-shadow:0 18px 70px #70414b0b}.access-brand{display:flex;align-items:center;justify-content:center;gap:11px;color:var(--red);font-size:26px;letter-spacing:-1px;margin-bottom:30px}.access-badge{display:flex;justify-content:center;align-items:center;gap:6px;color:#966572;font-size:10px;font-weight:700;letter-spacing:1.1px}.access-badge .icon{height:14px;width:14px}.access-card h1{text-align:center;margin:13px 0 12px;font-size:28px;letter-spacing:-.8px}.access-description{text-align:center;color:#806571;font-size:13px;line-height:1.8;margin:0 0 26px}.access-input{display:flex;align-items:center;border:1px solid #dccdd1;border-radius:10px;height:50px;padding:0 11px;gap:7px}.access-input:focus-within{border-color:var(--red);box-shadow:0 0 0 3px #c52c380c}.access-input input{width:100%;min-width:0;outline:none!important;border:0;height:100%;font-size:12px;letter-spacing:.3px;background:transparent;color:#493540}.access-input input::placeholder{font-size:10px;color:#9d8690;letter-spacing:0}.access-input button{background:#f8efed;border-radius:6px;color:#97616d;padding:6px 8px;min-width:40px;font-size:11px;min-height:32px}.access-error{font-size:11px;color:#b4293a;line-height:1.7;min-height:18px;margin:8px 0 12px}.access-input:has(input[aria-invalid=true]){border-color:var(--red)}.access-help{text-align:center;margin-top:27px;font-size:11px;color:#826973}.access-contacts{display:flex;justify-content:center;gap:9px;margin-top:11px}.access-contacts .contact-link{height:40px;color:#8f5664}.access-foot{margin-top:25px;color:#916f7d;font-size:10px}.key-session{min-width:35px}.key-session .icon{width:14px;height:14px}@media(max-width:550px){.access-card{padding:29px 24px 25px}.access-card h1{font-size:25px}.access-brand{font-size:24px;margin-bottom:24px}.access-input{height:52px}.access-input button{min-height:40px}.contact-nav>a:last-of-type>span:last-child{display:none}.brand{font-size:20px;gap:8px}.contact-nav{gap:6px}.contact-nav .contact-link{padding:0 9px}.key-session span{display:none}.key-session{padding:0 11px!important}}

.header-links{display:flex;gap:27px;margin-left:auto;margin-right:25px;color:#74606a;font-size:13px;font-weight:600}.header-links a:hover{color:var(--red)}.intro{align-items:center;gap:55px;margin-bottom:32px}.hero-copy{flex:1;min-width:0}.hero-copy h1{font-size:51px;letter-spacing:-2px;line-height:1.18}.hero-copy p{font-size:15px;line-height:1.85;max-width:560px}.hero-actions{display:flex;align-items:center;gap:20px;margin-top:24px}.hero-start{display:flex;align-items:center;gap:16px;border-radius:10px;background:var(--red);color:white;padding:13px 19px;font-size:13px;font-weight:650}.hero-start:hover{background:var(--red-deep)}.hero-actions>span{color:#74606a;font-size:12px}.hero-offer{width:340px;flex-shrink:0;background:linear-gradient(135deg,#ae1d2c,#d53c49);border:1px solid #af2633;border-radius:22px;padding:28px 30px;color:white;box-shadow:0 15px 35px #b9212c16;position:relative;overflow:hidden}.hero-offer:after{content:"";position:absolute;width:180px;height:180px;right:-70px;top:-70px;border:32px solid #ffffff0c;border-radius:50%;pointer-events:none}.offer-label{display:flex;align-items:center;gap:8px;font-size:9px;letter-spacing:1px;color:#ffe1e3;font-weight:650}.offer-label .icon{height:15px;width:15px}.hero-offer h2{font-size:36px;letter-spacing:-1.3px;margin:15px 0 2px;line-height:1.2}.hero-offer p{font-size:12px;margin:6px 0;color:#ffe1e3}.hero-offer button{display:flex;justify-content:space-between;align-items:center;width:100%;padding:12px 15px;border-radius:9px;background:white;color:#ab2030;font-size:12px;font-weight:700;min-height:44px}.hero-offer small{display:block;margin-top:12px;font-size:10px;color:#ffe1e3}.usage-strip{display:flex;gap:25px;align-items:center;background:#fff;border:1px solid #ecdadd;border-radius:15px;padding:19px 23px;margin-bottom:25px}.usage-heading{display:flex;gap:13px;align-items:center;flex:1;min-width:0}.usage-icon{display:grid;place-items:center;width:40px;height:42px;flex-shrink:0;border-radius:12px;background:#fff0ef;color:var(--red)}.usage-heading strong{font-size:13px}.usage-heading p{font-size:11px;color:#74606a;line-height:1.65;margin:4px 0 0}.usage-meter{min-width:155px;font-size:11px;color:#68525c}.usage-meter span{display:block;text-align:right;margin-bottom:7px;font-weight:650;font-variant-numeric:tabular-nums}.usage-meter progress{appearance:none;-webkit-appearance:none;width:100%;height:5px;border:0;display:block;border-radius:8px;overflow:hidden;background:#f5e5e6}.usage-meter progress::-webkit-progress-bar{background:#f5e5e6}.usage-meter progress::-webkit-progress-value{background:var(--red)}.usage-meter progress::-moz-progress-bar{background:var(--red)}.usage-strip>button{display:flex;align-items:center;gap:8px;color:var(--red);background:#fff5f3;border:1px solid #f0d1d4;padding:10px 13px;border-radius:9px;font-size:11px;font-weight:650;min-height:42px}.usage-strip.is-locked{border-color:#c52c38;background:#fff9f8}.usage-strip.is-unlimited .usage-icon{background:#ebf5ef;color:#236f58}.how-it-works{list-style:none;display:grid;grid-template-columns:repeat(3,1fr);margin:0 0 28px;padding:0;gap:20px}.how-it-works li{display:flex;align-items:center;gap:13px;padding:0 12px}.how-it-works li+li{border-left:1px solid #e7dadd;padding-left:30px}.how-it-works li>span{color:#b35a63;font-size:24px;font-weight:300;letter-spacing:-1px}.how-it-works strong{display:block;font-size:12px;font-weight:650}.how-it-works small{display:block;color:#7d6771;font-size:10px;margin-top:3px}.quantity-error{color:var(--red);font-size:12px;margin:12px 0 0}.access-gate{position:fixed;inset:0;z-index:70;min-height:0;overflow:auto;background:#27121b88;backdrop-filter:blur(5px);padding:26px 18px}.access-card{position:relative;flex-shrink:0;max-width:465px;box-shadow:0 22px 90px #220c2038}.close-key{position:absolute;top:13px;right:13px;height:36px;width:36px;border-radius:50%;background:#faf0ef;color:#8b5a65;font-size:23px;line-height:1}.access-card h2{text-align:center;font-size:27px;line-height:1.3;letter-spacing:-.9px;margin:14px 0 12px}.access-foot{color:white}.access-brand{margin-bottom:24px}.access-description strong{color:var(--red);white-space:nowrap}.modal-open{overflow:hidden}.hero-start,.hero-offer button,.usage-strip>button{transition:background .18s,transform .18s}.hero-start:focus-visible,.hero-offer button:focus-visible{outline:3px solid #eea5ac;outline-offset:4px}html{scroll-behavior:smooth;scroll-padding-top:24px}
@media(max-width:1050px){.header-links{gap:18px;margin-right:10px}.hero-copy h1{font-size:43px}.intro{gap:30px}.hero-offer{width:300px;padding:25px}.usage-strip{gap:15px}.usage-meter{min-width:115px}.hero-copy p{font-size:13px}}
@media(max-width:790px){.header-links{display:none}.intro{display:grid;grid-template-columns:minmax(0,1fr) 235px;gap:24px;margin-bottom:24px}.hero-copy h1{font-size:35px;letter-spacing:-1.2px}.hero-offer{width:100%;padding:21px}.hero-offer h2{font-size:29px}.offer-label{font-size:8px}.hero-actions{flex-wrap:wrap;gap:12px;margin-top:18px}.hero-actions>span{font-size:10px}.hero-start{font-size:11px;padding:12px 14px}.usage-strip{flex-wrap:wrap;padding:16px 18px;gap:12px}.usage-heading{flex-basis:calc(100% - 150px)}.usage-meter{min-width:105px}.usage-strip>button{margin-left:53px;min-height:44px}.how-it-works{gap:9px}.how-it-works li{padding:0;gap:8px}.how-it-works li+li{padding-left:12px}.how-it-works strong{font-size:10px}.how-it-works small{font-size:9px}.how-it-works li>span{font-size:21px}.access-card h2{font-size:25px}}
@media(max-width:550px){.intro{grid-template-columns:1fr;gap:23px}.hero-copy h1{font-size:40px;line-height:1.2}.hero-copy p{font-size:13px}.hero-offer{display:grid;grid-template-columns:1fr auto;column-gap:10px;padding:21px}.offer-label{grid-column:1/-1;font-size:8px}.hero-offer h2{margin-top:13px;font-size:29px}.hero-offer p{grid-column:1;font-size:10px}.hero-offer button{grid-column:1/-1;margin-top:14px}.hero-offer small{grid-column:1/-1;font-size:9px}.usage-heading{flex-basis:100%}.usage-meter{flex:1;margin-left:53px;min-width:0}.usage-meter span{text-align:left}.usage-strip>button{margin-left:0;padding:10px;font-size:10px}.how-it-works{margin-bottom:22px;grid-template-columns:1fr;gap:13px}.how-it-works li,.how-it-works li+li{border:0;padding:0 3px;gap:12px}.how-it-works strong{font-size:12px}.how-it-works small{font-size:10px}.how-it-works li>span{font-size:20px}.hero-actions>span{font-size:11px}.access-gate{justify-content:flex-start;padding-top:25px}.access-brand{margin-bottom:19px}.access-description{font-size:12px}.access-help{margin-top:20px}.contact-footer .footer-copy strong{color:#553740;font-size:13px}.usage-heading strong{font-size:12px}.usage-heading p{font-size:10px}.summary-top p,.summary-row,.summary-row strong,.toggle-line,.test-note,.test-run,.job-detail,.event{color:#755560}}
@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}}
.provider-chips{display:flex;gap:7px;flex-wrap:wrap;margin:-4px 0 18px}.provider-chips span{font-size:10px;border-radius:20px;padding:4px 9px;border:1px solid #ecdadd;background:#fffbfa;color:#825a64}.provider-chips span:last-child{color:#236f58;background:#f0f8f3;border-color:#d9eade}.spss-download{display:flex;margin-top:11px;font-weight:650}.spss-download[aria-busy=true]{opacity:.6;pointer-events:none}.spss-note{font-size:10px;line-height:1.7;color:#78606b;margin:7px 0 0}@media(max-width:550px){.input-grid{grid-template-columns:1fr}.quantity{max-width:200px}.provider-chips{gap:5px}.provider-chips span{font-size:9px}}
.provider-section{margin:0 0 22px}.provider-tabs{display:grid;grid-template-columns:1.25fr 1fr 1fr;gap:9px}.provider-tabs button{text-align:left;padding:15px 18px;border:1px solid #e7d7da;background:white;border-radius:12px;color:#775360;min-height:66px}.provider-tabs strong{display:block;font-size:13px;letter-spacing:-.15px}.provider-tabs small{display:block;font-size:10px;margin-top:4px;color:#88717b}.provider-tabs button[aria-selected=true]{background:#fff0ef;border-color:#ca6972;color:#a81f2e;box-shadow:inset 0 2px 0 #c52c38}.provider-panel{padding:13px 17px 0}.provider-panel strong{font-size:12px;color:#71545e}.provider-panel p{font-size:11px;line-height:1.75;margin:5px 0 0;color:#78606a;max-width:900px}.provider-tabs button:hover:not(:disabled){border-color:#c66370}@media(max-width:550px){.provider-tabs{gap:5px;grid-template-columns:1.1fr 1fr 1fr}.provider-tabs button{padding:12px 9px;min-height:72px}.provider-tabs strong{font-size:11px}.provider-tabs small{font-size:9px;line-height:1.5}.provider-panel{padding:12px 5px 0}.provider-panel strong{font-size:11px}.provider-panel p{font-size:10px}}
.run-settings{border-top:1px solid #efe1e2;margin-top:18px;padding-top:15px}.run-settings summary{font-size:12px;color:#795361;font-weight:650;cursor:pointer;min-height:32px}.run-options{display:grid;grid-template-columns:1.65fr .8fr .8fr 1.2fr;gap:12px;margin-top:12px}.run-options input,.run-options select{width:100%;min-width:0;background:#fff;border:1px solid #dfd0d5;border-radius:9px;padding:9px 10px;height:42px;font-size:12px;color:#583c48}.run-options input:disabled{background:#f7f1f1;color:#99848b}.run-hint{font-size:10px;line-height:1.7;color:#806570;margin:10px 0 0}.run-hint+.run-hint{margin-top:3px}.duration-estimate{font-size:11px;font-weight:600;color:#754854;line-height:1.7;margin:10px 0 0}.academic-tools{display:flex;align-items:center;gap:12px;background:#f7f4ee;border:1px solid #ebe2d1;border-radius:12px;padding:13px 15px;margin-bottom:15px}.academic-tools>div{flex:1}.academic-tools strong{font-size:11px;color:#695541}.academic-tools p{font-size:10px;color:#81705e;margin:4px 0 0;line-height:1.6}.academic-tools select{max-width:185px;min-width:0;border:1px solid #dfd4c4;border-radius:8px;background:white;font-size:11px;padding:8px;color:#6c5443;min-height:37px}.pause-run{display:block;border:1px solid #d9a6ae;background:#fff8f6;color:#a32839;font-size:12px;font-weight:650;min-height:40px;border-radius:9px;width:100%;margin-top:11px}.run-meta{font-size:10px;line-height:1.7;color:#806470;margin:10px 0 0;overflow-wrap:anywhere}.stats-panel{margin-top:25px;padding:21px 24px}.stats-panel>summary{font-size:15px;font-weight:650;cursor:pointer;color:#573644;min-height:32px}.stats-note{font-size:11px;line-height:1.8;color:#7c626e;max-width:900px;margin:12px 0}.report-content{margin-top:15px;font-size:12px;color:#735361;overflow-x:auto}.stats-table{width:100%;border-collapse:collapse;font-size:11px;min-width:570px}.stats-table th,.stats-table td{padding:10px;border-bottom:1px solid #eee3e6;text-align:right;vertical-align:top}.stats-table th:first-child,.stats-table td:first-child{text-align:left;max-width:450px}.stats-table thead{background:#fcf3f2}.stats-table details{margin-top:6px;font-size:10px;color:#806570}.stats-table details p{margin:5px 0}.status-chip.paused{background:#fff0c9;color:#806316}.status-chip.waiting{background:#eceff9;color:#4e5d91}@media(max-width:790px){.run-options{grid-template-columns:1fr 1fr}.run-options>div:first-child,.run-options>div:nth-child(2){grid-column:1/-1}.run-options>div:last-child{grid-column:1/-1}.academic-tools{flex-wrap:wrap}.academic-tools>div{flex-basis:100%}.academic-tools select{flex:1;max-width:none;min-height:44px}.academic-tools button{min-height:44px}.stats-panel{padding:18px}.run-options .label{font-size:11px}}
@media(min-width:791px){.run-options{grid-template-columns:1.4fr 1.4fr .75fr .75fr 1fr 1fr;align-items:end}}@media(max-width:790px){.run-options>div:last-child{grid-column:auto}}
</style>
</head>
<body>
<a class="skip-link" href="#mainContent">Bỏ qua đến nội dung chính</a>
<svg class="defs" xmlns="http://www.w3.org/2000/svg"><defs>
<symbol id="i-form" viewBox="0 0 24 24"><path d="M6 3h9l4 4v14H6zM14 3v5h5M9 12h7M9 16h7"/><path d="M3 6v15"/></symbol>
<symbol id="i-link" viewBox="0 0 24 24"><path d="m10 13 4-4M8 15l-2 2a4 4 0 0 1-5-5l5-5a4 4 0 0 1 6 0m0 2 2-2a4 4 0 0 1 6 5l-5 5a4 4 0 0 1-6 0" transform="translate(1 0)"/></symbol>
<symbol id="i-dice" viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="5"/><path d="M7 7h.01M17 7h.01M12 12h.01M7 17h.01M17 17h.01" stroke-width="3"/></symbol>
<symbol id="i-check" viewBox="0 0 24 24"><path d="m5 12 4 4L19 6"/></symbol>
<symbol id="i-search" viewBox="0 0 24 24"><circle cx="10" cy="10" r="6.5"/><path d="m15 15 5 5"/></symbol>
<symbol id="i-play" viewBox="0 0 24 24"><path d="m8 4 12 8-12 8z"/></symbol>
<symbol id="i-stop" viewBox="0 0 24 24"><rect x="5" y="5" width="14" height="14" rx="2"/></symbol>
<symbol id="i-spark" viewBox="0 0 24 24"><path d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5zM20 2v4m-2-2h4"/></symbol>
<symbol id="i-arrow" viewBox="0 0 24 24"><path d="M4 12h16m-6-6 6 6-6 6"/></symbol>
<symbol id="i-download" viewBox="0 0 24 24"><path d="M12 3v12m-5-5 5 5 5-5M4 17v4h16v-4"/></symbol>
<symbol id="i-clock" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></symbol>
<symbol id="i-shield" viewBox="0 0 24 24"><path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6z"/><path d="m8 12 3 3 5-6"/></symbol>
<symbol id="i-sliders" viewBox="0 0 24 24"><path d="M4 6h16M4 12h16M4 18h16"/><circle cx="9" cy="6" r="2" fill="white"/><circle cx="15" cy="12" r="2" fill="white"/><circle cx="8" cy="18" r="2" fill="white"/></symbol>
<symbol id="i-alert" viewBox="0 0 24 24"><path d="m12 3 10 18H2zM12 9v5m0 3v.01"/></symbol>
</defs></svg>
<!-- LIÊN HỆ NHẬN KEY: sửa các liên kết Facebook/Zalo trong phần này và phần liên hệ trên trang chủ. -->
<section id="accessGate" class="access-gate" role="dialog" aria-modal="true" aria-labelledby="accessTitle" hidden>
<div class="access-card">
<button id="closeKey" type="button" class="close-key" aria-label="Đóng cửa sổ nhập key">×</button>
<div class="access-brand"><span class="brand-mark"><svg class="icon"><use href="#i-form"/></svg></span><strong>KDautoform</strong></div>
<div class="access-badge"><svg class="icon"><use href="#i-shield"/></svg>KEY SỬ DỤNG KDautoform</div>
<h2 id="accessTitle">Nhập key để tiếp tục.</h2>
<p class="access-description" id="upgradeDescription">Bạn có 10 lượt gửi miễn phí. Khi hết lượt, nhập key được cấp để tiếp tục. Liên hệ Facebook hoặc Zalo bên dưới để nhận key.</p>
<form id="accessForm">
<label class="label" for="accessKey">Key sử dụng</label>
<div class="access-input"><input id="accessKey" type="password" required maxlength="256" placeholder="KD-XXXXXXXX-XXXXXXXX-XXXXXXXX-XXXXXXXX" autocomplete="off" spellcheck="false" autocapitalize="characters" aria-describedby="accessHint accessError"><button type="button" id="toggleKey" aria-label="Hiện key" aria-pressed="false">Hiện</button></div>
<p id="accessHint" style="font-size:11px;color:#806571;line-height:1.7;margin:8px 0 0">Dán mã bắt đầu bằng KD-. Công cụ tự bỏ phần tên key và ký tự thừa khi sao chép.</p>
<p id="accessError" class="access-error" role="alert" aria-live="polite"></p>
<button id="activateKey" type="submit" class="primary"><svg class="icon"><use href="#i-arrow"/></svg>Mở khóa bằng key</button>
</form>
<div id="paidKeyControls" hidden><p class="access-help">Key unlimited đang hoạt động trên trình duyệt này.</p><button type="button" id="deactivateKey" class="test-run">Ngừng dùng key trên trình duyệt này</button></div>
<div class="access-help">Liên hệ nhận key</div>
<div class="access-contacts"><a class="contact-link" href="https://zalo.me/0343415673" target="_blank" rel="noopener noreferrer"><span class="zalo-mark">Zalo</span>0343415673</a><a class="contact-link" href="https://www.facebook.com/khuong.duy.081207/" target="_blank" rel="noopener noreferrer"><span class="fb-mark">f</span>Facebook</a></div>
</div>
<div class="access-foot">KDautoform · Google Forms &amp; Microsoft Forms</div>
</section>
<div id="workspaceApp">

<header class="header"><div class="header-inner">
<a href="/" class="brand" aria-label="KDautoform trang chính"><span class="brand-mark"><svg class="icon"><use href="#i-form"/></svg></span><span>KDautoform</span></a>
<nav class="header-links" aria-label="Điều hướng"><a href="#setupTitle">Công cụ</a><a href="#contact">Liên hệ</a></nav><div class="contact-nav"><button id="logoutKey" type="button" class="contact-link key-session" aria-label="Nhập key unlimited"><svg class="icon"><use href="#i-shield"/></svg><span>Nhập key</span></button><a class="contact-link" href="https://zalo.me/0343415673" target="_blank" rel="noopener noreferrer"><span class="zalo-mark">Zalo</span><span class="phone-label">0343415673</span></a><a class="contact-link" href="https://www.facebook.com/khuong.duy.081207/" target="_blank" rel="noopener noreferrer"><span class="fb-mark">f</span><span>Facebook</span></a></div>
</div></header>
<main id="mainContent" tabindex="-1">
<section class="intro"><div class="hero-copy"><div class="eyebrow">GOOGLE FORMS · NỀN TẢNG CHÍNH</div><h1>Một đường link.<br><span>Chủ động mọi tỷ lệ.</span></h1><p>Không gian thực hành cho học sinh, sinh viên và giảng viên: kiểm thử biểu mẫu, mô phỏng dữ liệu và học cách đọc thống kê. Bắt đầu với 10 lượt miễn phí.</p><div class="hero-actions"><a class="hero-start" href="#setupTitle">Dùng thử miễn phí <svg class="icon"><use href="#i-arrow"/></svg></a><span>Không cần tài khoản</span></div></div><aside class="hero-offer" aria-label="Dùng key sau lượt miễn phí"><div class="offer-label"><svg class="icon"><use href="#i-shield"/></svg>10 LƯỢT MIỄN PHÍ ĐẦU TIÊN</div><h2>Tiếp tục với key.</h2><p>Dùng hết 10 lượt miễn phí, nhập key để mở khóa các lượt tiếp theo.</p><button type="button" id="heroUpgrade">Nhập key <svg class="icon"><use href="#i-arrow"/></svg></button><small>Liên hệ Facebook hoặc Zalo để được cấp key</small></aside></section>
<section class="usage-strip" aria-label="Hạn mức sử dụng"><div class="usage-heading"><span class="usage-icon"><svg class="icon"><use href="#i-shield"/></svg></span><div><strong id="planLabel">10 lượt miễn phí dành cho bạn</strong><p id="usageDetail">Đang kiểm tra hạn mức sử dụng...</p></div></div><div class="usage-meter"><span id="usageCount" aria-live="polite">— / 10 lượt</span><progress id="usageProgress" max="10" value="0" aria-label="Lượt miễn phí đã sử dụng"></progress></div><button type="button" id="upgradeButton">Mở khóa unlimited <svg class="icon"><use href="#i-arrow"/></svg></button></section>
<ol class="how-it-works" aria-label="Ba bước sử dụng"><li><span>01</span><div><strong>Dán link biểu mẫu</strong><small>Tự đọc câu hỏi qua các trang</small></div></li><li><span>02</span><div><strong>Chọn tỷ lệ mong muốn</strong><small>Điều chỉnh hoặc tạo ngẫu nhiên</small></div></li><li><span>03</span><div><strong>Chạy &amp; theo dõi</strong><small>Xem kết quả, tải nhật ký</small></div></li></ol>
<section class="provider-section" aria-label="Chọn loại biểu mẫu"><div class="provider-tabs" role="tablist" aria-label="Nền tảng biểu mẫu"><button type="button" id="tab-google" data-provider="google" role="tab" aria-controls="providerPanel" aria-selected="true" tabindex="0"><strong>Google Forms</strong><small>Phần chính · Đầy đủ tính năng</small></button><button type="button" id="tab-microsoft" data-provider="microsoft" role="tab" aria-controls="providerPanel" aria-selected="false" tabindex="-1"><strong>Microsoft Forms</strong><small>Form Microsoft công khai</small></button><button type="button" id="tab-html" data-provider="html" role="tab" aria-controls="providerPanel" aria-selected="false" tabindex="-1"><strong>Form web khác</strong><small>HTML chuẩn · Thử nghiệm</small></button></div><div id="providerPanel" class="provider-panel" role="tabpanel" aria-labelledby="tab-google"><strong id="providerHeading">Google Forms là phần chính của KDautoform</strong><p id="providerHelp">Giữ đầy đủ đọc nhiều trang, phân nhánh, tỷ lệ từng câu, câu trả lời tiếng Việt và nhật ký kết quả.</p></div></section>
<section class="panel setup" aria-labelledby="setupTitle">
<div class="panel-head"><span class="step">01</span><h2 id="setupTitle">Thiết lập lượt điền</h2><span class="tiny">Dán link để tự đọc toàn bộ form</span></div>
<div class="provider-chips" aria-label="Tính năng dùng chung"><span>Chỉnh tỷ lệ từng câu</span><span>Dữ liệu mô phỏng</span><span>Xuất SPSS .sav</span></div>
<div class="input-grid"><div><label class="label" for="formUrl">Link Google Forms hoặc Microsoft Forms</label><div class="url-wrap"><svg class="icon"><use href="#i-link"/></svg><input id="formUrl" type="url" placeholder="Dán link docs.google.com/forms, forms.gle hoặc forms.office.com" autocomplete="off" spellcheck="false"><button id="scanAgain" class="scan-again" type="button">Đọc lại</button></div></div>
<div><label class="label" for="quantity">Số lượng mong muốn</label><div class="quantity"><button type="button" id="minusCount" aria-label="Giảm số lượng">−</button><input id="quantity" type="number" min="1" max="10" step="1" value="10" aria-label="Số lượng mong muốn"><button type="button" id="plusCount" aria-label="Tăng số lượng">+</button></div></div></div>
<div class="setup-bottom"><div id="scanStatus" class="scan-status" role="status" aria-live="polite"><svg class="icon"><use href="#i-shield"/></svg><span>Google Forms và Microsoft Forms công khai</span></div><span id="quantityHint" class="setup-tip">Tối đa 10 lượt miễn phí</span></div><p id="quantityError" class="quantity-error" role="status" hidden></p>
<details class="run-settings" open><summary>Tốc độ, thời gian chạy &amp; tái tạo dữ liệu</summary><div class="run-options">
<div><label class="label" for="engineMode">Cách điền Google Forms</label><select id="engineMode"><option value="fast">Nhanh · điền theo trang</option><option value="compatible">Tương thích · từng câu</option></select></div>
<div><label class="label" for="timingPreset">Tốc độ yêu cầu</label><select id="timingPreset"><option value="maximum">Tối đa · không chờ thêm</option><option value="fast" selected>Nhịp 2 giây</option><option value="fixed3">Mỗi 3 giây</option><option value="fixed30">Mỗi 30 giây</option><option value="fixed60">Mỗi 1 phút · 60 giây</option><option value="random30_180">Ngẫu nhiên · 30 giây–3 phút</option><option value="fixed180">Cố định · 3 phút</option><option value="custom_fixed">Tùy chỉnh · cố định</option><option value="custom_random">Tùy chỉnh · trong khoảng</option></select></div>
<div><label class="label" for="timingBasis">Cách tính khoảng cách</label><select id="timingBasis"><option value="start">Giữa hai lần bắt đầu</option><option value="completion">Nghỉ sau khi hoàn tất</option></select></div>
<div><label class="label" for="intervalMin">Từ / cố định (giây)</label><input id="intervalMin" type="number" min="0" max="86400" step="1" value="2"></div>
<div><label class="label" for="intervalMax">Đến (giây)</label><input id="intervalMax" type="number" min="0" max="86400" step="1" value="2" disabled></div>
<div><label class="label" for="sampleSeed">Seed tái tạo dữ liệu</label><input id="sampleSeed" type="number" min="0" max="9007199254740991" step="1" placeholder="Để trống: ngẫu nhiên"></div>
<div><label class="label" for="referenceDate">Ngày tham chiếu</label><input id="referenceDate" type="date" min="1900-01-01" max="2100-12-31"></div></div>
<p id="pacingHint" class="run-hint">Lượt đầu bắt đầu khi đến lượt trong hàng đợi. Mỗi form được điền xong trước khi chạy lượt tiếp theo.</p><p class="run-hint">Thời gian hoàn tất phụ thuộc form và mạng. Nhịp 2 giây không bảo đảm điền xong trong 2 giây. Chọn Tối đa để bỏ thời gian chờ thêm. Giữ cùng seed, cấu hình và ngày tham chiếu để lặp lại dữ liệu.</p><p id="serverCapacity" class="run-hint" role="status"></p><p id="durationEstimate" class="duration-estimate" aria-live="polite"></p><p id="runOptionsError" class="quantity-error" role="status" hidden></p></details>
</section>
<div class="workspace">
<section class="questions" aria-labelledby="ratioTitle">
<div class="section-title"><h2 id="ratioTitle">Chia tỷ lệ trả lời</h2><span id="fieldCount" class="count-badge">0 câu hỏi</span><button id="randomAll" type="button" class="ghost-button red-outline" disabled><svg class="icon"><use href="#i-dice"/></svg>Ngẫu nhiên tất cả</button></div>
<div id="formMeta" class="form-meta" hidden></div>
<div id="academicTools" class="academic-tools" hidden><div><strong>Mẫu phân phối để thực hành</strong><p>Giữ phần nhân khẩu học, văn bản và câu chọn nhiều.</p></div><select id="ratioPreset" aria-label="Mẫu phân phối"><option value="balanced">Cân bằng</option><option value="bell">Tập trung mức giữa</option><option value="positive">Đồng ý nhẹ</option></select><button id="applyPreset" type="button" class="small-button red-outline">Áp dụng mẫu</button></div>
<div id="toolbar" class="toolbar" hidden><div class="search-wrap"><svg class="icon"><use href="#i-search"/></svg><input id="searchQuestions" type="search" placeholder="Tìm câu hỏi..." aria-label="Tìm câu hỏi"></div><select id="pageFilter" class="page-select" aria-label="Lọc trang"><option value="">Mọi trang</option></select><button id="undoRates" class="small-button" type="button" disabled aria-label="Hoàn tác thay đổi tỷ lệ">Hoàn tác</button><button id="balanceAll" class="small-button" type="button"><svg class="icon"><use href="#i-sliders"/></svg>Cân bằng</button></div>
<div id="questionList"><div class="panel empty">
<svg class="empty-art" viewBox="0 0 170 115" fill="none" aria-hidden="true"><circle cx="87" cy="58" r="47" fill="#fff2ef"/><rect x="50" y="9" width="78" height="95" rx="10" fill="white" stroke="#e8c9cb" transform="rotate(7 50 9)"/><rect x="37" y="12" width="78" height="94" rx="10" fill="white" stroke="#d8a7ad" transform="rotate(-5 37 12)"/><rect x="49" y="28" width="36" height="5" rx="2.5" fill="#c7424e"/><rect x="49" y="41" width="51" height="3" rx="1.5" fill="#ead9d9"/><rect x="49" y="53" width="41" height="3" rx="1.5" fill="#ead9d9"/><circle cx="52" cy="70" r="4" stroke="#d6979e"/><path d="m50 70 1.5 1.5 3-3" stroke="#c52c38" stroke-width="1.5"/><rect x="63" y="68" width="33" height="4" rx="2" fill="#efc9cc"/><circle cx="52" cy="86" r="4" stroke="#e4c7cb"/><rect x="63" y="84" width="25" height="4" rx="2" fill="#efe2e3"/><rect x="107" y="66" width="37" height="32" rx="9" fill="#c52c38"/><path d="M116 82h19m-6-6 6 6-6 6" stroke="white" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/><path d="m143 24 2 5 5 2-5 2-2 5-2-5-5-2 5-2z" fill="#e8aaac"/><circle cx="25" cy="73" r="3" fill="#eacbcd"/></svg>
<h3>Câu hỏi sẽ xuất hiện tại đây</h3><p>Dán link biểu mẫu ở phía trên. Bạn có thể điều chỉnh tỷ lệ từng đáp án trước khi công cụ bắt đầu.</p><div class="empty-foot"><span><svg class="icon"><use href="#i-check"/></svg>Tự đọc tất cả trang</span><span><svg class="icon"><use href="#i-check"/></svg>Chủ động tỷ lệ</span></div></div></div>
</section>
<aside class="panel summary" aria-label="Tóm tắt và tiến trình">
<div class="summary-top"><div class="summary-kicker"><svg class="icon"><use href="#i-form"/></svg>SỐ LƯỢT BẠN MUỐN</div><div id="summaryCount" class="big-count">100<small>lượt điền</small></div><p>Đúng biểu mẫu. Đủ các trang.<br>Thực hiện lần lượt theo cấu hình của bạn.</p></div>
<div class="summary-body">
<div class="summary-rows"><div class="summary-row"><span>Biểu mẫu</span><strong id="summaryQuestions">Chưa kết nối</strong></div><div class="summary-row"><span>Tỷ lệ hợp lệ</span><strong id="summaryValid">—</strong></div><div class="summary-row"><span>Phân bổ</span><strong id="summaryMode">Theo mong muốn</strong></div></div>
<div class="summary-separator"></div>
<div class="summary-controls"><label class="toggle-line" for="autoRun">Tự chạy sau khi đọc form<span class="switch"><input type="checkbox" id="autoRun" checked><span></span></span></label><label class="toggle-line" for="showBrowser">Hiện cửa sổ trình duyệt<span class="switch"><input type="checkbox" id="showBrowser"><span></span></span></label></div>
<div id="countdown" class="countdown" hidden></div>
<button class="primary" id="startButton" disabled><svg class="icon"><use href="#i-play"/></svg><span>Dán link để bắt đầu</span></button>
<button id="pauseButton" type="button" class="pause-run" hidden>Tạm dừng</button><button id="testButton" class="test-run" disabled><svg class="icon"><use href="#i-shield"/></svg>Điền thử 1 lượt, chưa gửi</button>
<div class="test-note">Dữ liệu mô phỏng · Không có bước tạo tài khoản</div>
</div>
<div id="jobPanel" class="job-panel" hidden><div class="job-head"><span>Tiến trình thực hiện</span><span id="jobStatus" class="status-chip">ĐANG CHẠY</span></div><div class="job-numbers"><strong id="jobDone">0</strong><span id="jobTotal">/ 100 lượt</span></div><div id="jobBar" class="job-bar"><i></i></div><div id="jobDetail" class="job-detail" role="status" aria-live="polite"></div><p id="runMeta" class="run-meta"></p><p id="responseTiming" class="run-meta" role="status" aria-live="polite"></p><div id="jobEvents" class="events"></div><a id="downloadLog" class="download" hidden><svg class="icon"><use href="#i-download"/></svg>Tải nhật ký kết quả</a><a id="downloadSpss" class="download spss-download" hidden><svg class="icon"><use href="#i-download"/></svg>Tải dữ liệu SPSS (.sav)</a><a id="downloadCsv" class="download spss-download" hidden><svg class="icon"><use href="#i-download"/></svg>CSV + bảng mã + báo cáo (.zip)</a><p id="spssNote" class="spss-note" hidden>Chỉ xuất phản hồi đã được xác nhận. Dữ liệu mô phỏng được ghi nhãn trong file.</p></div>
</aside>
</div>
<details id="statsPanel" class="panel stats-panel" hidden><summary>Thống kê mô tả của đợt chạy</summary><p class="stats-note">Chỉ tính phản hồi đã được xác nhận. Trung bình và SD mẫu mô tả mã thang điểm; không phải kết luận kiểm định hay dữ liệu từ người tham gia thật.</p><button id="loadReport" class="small-button red-outline" type="button">Xem / cập nhật thống kê</button><div id="reportContent" class="report-content" aria-live="polite">Bấm để xem tần suất, số thiếu và thang điểm.</div></details>
<footer id="contact" class="contact-footer"><div class="footer-copy"><strong>Liên hệ nhận key</strong><p>Nhắn Facebook hoặc Zalo để nhận key và được hỗ trợ sử dụng.</p></div><div class="footer-links"><a href="https://zalo.me/0343415673" target="_blank" rel="noopener noreferrer"><span class="zalo-mark">Zalo</span>0343415673</a><a href="https://www.facebook.com/khuong.duy.081207/" target="_blank" rel="noopener noreferrer"><span class="fb-mark">f</span>Liên hệ Facebook</a></div></footer>
<div class="copyright">KDautoform · SIMPLE INPUT. YOUR OWN DISTRIBUTION.</div>
</main>
<div id="toast" class="toast" role="status" hidden></div>
</div>
<script>
"use strict";
const TOKEN="__APP_TOKEN__";
const $=id=>document.getElementById(id);
const icon=name=>'<svg class="icon"><use href="#i-'+name+'"/></svg>';
const escapeHTML=value=>String(value??"").replace(/[&<>"']/g,ch=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]));
const colors=["#bc2635","#d34e59","#e78186","#eeb0af","#f1cac5","#e7d9d3","#c98c95","#dcb1b5"];
const kinds={single:"Chọn một",dropdown:"Danh sách",multiple:"Chọn nhiều",scale:"Chấm điểm",grid:"Bảng đánh giá",grid_multiple:"Bảng chọn nhiều",text:"Trả lời ngắn",paragraph:"Tự luận",date:"Ngày tháng",time:"Thời gian"};
const state={schema:null,config:{},scanId:null,scanToken:0,scanning:false,job:null,armed:false,autoTimer:null,remaining:0,pollTimer:null,forceCheck:false,access:null,spssAvailable:false,csvAvailable:false,providerMode:"google",polling:false,pollErrors:0,undo:[]};
const emptyMarkup=$("questionList").innerHTML;
let urlTimer=null,toastTimer=null;
function toast(message){$("toast").textContent=message;$("toast").hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>$("toast").hidden=true,4200)}
async function api(path,data){const response=await fetch(path,{method:data?"POST":"GET",headers:data?{"Content-Type":"application/json","X-Local-Token":TOKEN}:{},body:data?JSON.stringify(data):undefined});let result;try{result=await response.json()}catch{throw Error("Máy chủ chưa trả về dữ liệu. Vui lòng thử lại.")}if(result.plan)applyAccess(result);if(!response.ok){if(response.status===402){cancelCountdown();if(result.locked)openKeyModal(result.error)}if(response.status===401&&!path.startsWith("/api/auth/"))status(result.error,"error");const error=Error(result.error||"Không thực hiện được thao tác.");error.status=response.status;throw error}return result}
function activeJob(){return state.job&&["queued","running","stopping"].includes(state.job.status)}
function providerFromLink(value){try{const host=new URL(value).hostname.toLowerCase();if(["docs.google.com","forms.gle","forms.google.com"].includes(host))return "google";if(["forms.office.com","forms.office365.com","forms.cloud.microsoft","forms.microsoft.com"].includes(host))return "microsoft"}catch{}return null}
function validLink(value){if(state.providerMode==="html")return /^https:\/\//i.test(value);return /^https:\/\/(?:docs\.google\.com\/forms\/|forms\.gle\/|forms\.google\.com\/|forms\.office\.com\/|forms\.office365\.com\/|forms\.cloud\.microsoft\/|forms\.microsoft\.com\/)/i.test(value)}
function selectProvider(provider,reset=false){
if(!["google","microsoft","html"].includes(provider)||activeJob())return;
state.providerMode=provider;localStorage.setItem("formpilot.provider",provider);
const copy={google:["Google Forms là phần chính của KDautoform","Giữ đầy đủ đọc nhiều trang, phân nhánh, tỷ lệ từng câu, câu trả lời tiếng Việt và nhật ký kết quả.","Link Google Forms","https://docs.google.com/forms/d/e/.../viewform"],microsoft:["Microsoft Forms công khai","Tự đọc câu hỏi và các phần, chỉnh tỷ lệ và điền theo nhánh. Biểu mẫu cần cho phép Anyone can respond.","Link Microsoft Forms","https://forms.office.com/r/..."],html:["Form web khác · HTML chuẩn thử nghiệm","Hỗ trợ form HTML POST một trang với ô nhập và lựa chọn chuẩn. Form tùy biến, đăng nhập, CAPTCHA hoặc nhiều bước chưa được hỗ trợ. Dùng Điền thử để kiểm tra trước.","Link form HTML công khai","https://website-cua-ban.example/khao-sat"]}[provider];
$("providerHeading").textContent=copy[0];$("providerHelp").textContent=copy[1];$("providerPanel").setAttribute("aria-labelledby","tab-"+provider);
document.querySelectorAll("[data-provider]").forEach(node=>{const chosen=node.dataset.provider===provider;node.setAttribute("aria-selected",String(chosen));node.tabIndex=chosen?0:-1});
$("formUrl").closest(".input-grid").querySelector("label").textContent=copy[2];$("formUrl").placeholder=copy[3];
if(provider==="html")$("autoRun").checked=false;
if(reset){clearTimeout(urlTimer);cancelCountdown();state.scanToken++;state.schema=null;state.config={};state.scanId=null;state.scanning=false;state.armed=false;state.undo=[];$("formUrl").value="";localStorage.removeItem("formpilot.scan");localStorage.removeItem("formpilot.draft");localStorage.setItem("formpilot.url","");$("questionList").innerHTML=emptyMarkup;$("toolbar").hidden=true;$("academicTools").hidden=true;$("formMeta").hidden=true;$("fieldCount").textContent="0 câu hỏi";status("Dán link "+(provider==="html"?"form HTML chuẩn":provider==="microsoft"?"Microsoft Forms":"Google Forms")+" để bắt đầu.");renderSummary()}
}
function status(text,kind=""){const node=$("scanStatus");node.className="scan-status "+kind;node.innerHTML=(kind==="loading"?'<span class="spinner"></span>':icon(kind==="success"?"check":kind==="error"?"alert":"shield"))+"<span>"+escapeHTML(text)+"</span>"}
function quantity(){const raw=$("quantity").value.trim();if(raw==="")return null;const value=Number(raw);return Number.isInteger(value)&&value>=0?value:null}
function fieldValid(field){const entry=state.config[field.id];if(!entry)return false;if(field.options.length){const values=field.options.map(option=>Number(entry.rates[option]));if(values.some(value=>!Number.isFinite(value)||value<0||value>100))return false;const sum=values.reduce((a,b)=>a+b,0);return ["multiple","grid_multiple"].includes(field.kind)?sum>=field.minimum*100-.001&&sum<=field.maximum*100+.001:Math.abs(sum-100)<.001}return entry.mode!=="fixed"||Boolean(entry.value.trim())}
function allowedCount(){const n=quantity();return n!==null&&Boolean(state.access)&&(state.access.plan==="unlimited"||(n>0&&n<=state.access.remaining))}
function runSchedule(){const mode=["random30_180","custom_random"].includes($("timingPreset").value)?"random":"fixed",minRaw=$("intervalMin").value.trim(),maxRaw=$("intervalMax").value.trim(),minimum=Number(minRaw),maximum=mode==="fixed"?minimum:Number(maxRaw);if(!minRaw||(mode==="random"&&!maxRaw)||![minimum,maximum].every(n=>Number.isFinite(n)&&n>=0&&n<=86400)||maximum<minimum)return null;return {mode,min_seconds:minimum,max_seconds:maximum,basis:$("timingBasis").value}}
function runSeed(){const raw=$("sampleSeed").value.trim();if(!raw)return null;const value=Number(raw);return Number.isSafeInteger(value)&&value>=0?value:undefined}
function runReferenceDate(){const el=$("referenceDate");return el.value&&el.validity.valid?el.value:null}
function formatDuration(value){if(!Number.isFinite(value))return "—";let seconds=Math.max(0,Math.ceil(value)),hours=Math.floor(seconds/3600);seconds%=3600;const minutes=Math.floor(seconds/60);seconds%=60;return (hours?hours+" giờ ":"")+(minutes?minutes+" phút ":"")+(seconds||(!hours&&!minutes)?seconds+" giây":"")}
function updateRunOptions(){const schedule=runSchedule(),seed=runSeed(),active=Boolean(activeJob());for(const id of ["timingPreset","timingBasis","intervalMin","sampleSeed","referenceDate"])$(id).disabled=active;$("intervalMax").disabled=active||!["random30_180","custom_random"].includes($("timingPreset").value);$("engineMode").disabled=active||state.providerMode!=="google";const error=!schedule?"Chọn khoảng thời gian hợp lệ từ 0 đến 86.400 giây; giá trị Đến không được nhỏ hơn Từ.":seed===undefined?"Seed cần là số nguyên không âm, tối đa 9007199254740991.":!runReferenceDate()?"Chọn ngày tham chiếu hợp lệ để tái tạo dữ liệu.":"";$("runOptionsError").hidden=!error;$("runOptionsError").textContent=error;const n=quantity(),measured=state.job?.average_response_seconds;const basis=schedule?.basis==="completion";$("pacingHint").textContent=basis?"Nghỉ đủ số giây đã chọn sau khi hoàn tất form, rồi trở lại hàng đợi.":"Khoảng cách tính giữa hai lần bắt đầu. Form lâu hơn nhịp đặt sẽ được điền xong trước; không chạy dồn để bù trễ.";const eta=(interval)=>measured?(basis?n*measured+(n-1)*interval:measured+(n-1)*Math.max(measured,interval)):(n-1)*interval;$("durationEstimate").textContent=!schedule||n===null?"":n===0?"Chạy liên tục đến khi bạn dừng.":n===1?"Một lượt được xử lý khi đến vị trí trong hàng đợi.":(measured?"Ước tính theo tốc độ vừa đo: ":"Tổng khoảng chờ/nhịp tối thiểu: ")+formatDuration(eta(schedule.min_seconds))+(schedule.max_seconds!==schedule.min_seconds?" – "+formatDuration(eta(schedule.max_seconds)):"")+(measured?". Chưa tính chờ người khác trong hàng đợi.":". Chưa gồm toàn bộ thời gian xử lý form và hàng đợi.")}
function persistRunOptions(){localStorage.setItem("formpilot.runOptions",JSON.stringify({engine:$("engineMode").value,preset:$("timingPreset").value,basis:$("timingBasis").value,min:$("intervalMin").value,max:$("intervalMax").value,seed:$("sampleSeed").value,referenceDate:$("referenceDate").value}))}
function restoreRunOptions(){const today=new Date();$("referenceDate").value=today.getFullYear()+"-"+String(today.getMonth()+1).padStart(2,"0")+"-"+String(today.getDate()).padStart(2,"0");try{const saved=JSON.parse(localStorage.getItem("formpilot.runOptions")||"null");if(saved){if(["start","completion"].includes(saved.basis))$("timingBasis").value=saved.basis;if(["fast","compatible"].includes(saved.engine))$("engineMode").value=saved.engine;if([...$("timingPreset").options].some(o=>o.value===saved.preset))$("timingPreset").value=saved.preset;for(const [id,key] of [["intervalMin","min"],["intervalMax","max"],["sampleSeed","seed"],["referenceDate","referenceDate"]])if(saved[key]!==undefined)$(id).value=saved[key]}}catch{}updateRunOptions()}
function ready(){return Boolean(state.access)&&!state.access.locked&&Boolean(state.schema)&&!state.scanning&&allowedCount()&&Boolean(runSchedule())&&runSeed()!==undefined&&Boolean(runReferenceDate())&&state.schema.fields.every(fieldValid)}
function apportion(weights,total=100){const sum=weights.reduce((a,b)=>a+b,0);if(!sum)return weights.map((_,i)=>i===0?total:0);const ideal=weights.map(v=>v/sum*total),values=ideal.map(Math.floor);const order=values.map((_,i)=>i).sort((a,b)=>(ideal[b]-values[b])-(ideal[a]-values[a]));const left=total-values.reduce((a,b)=>a+b,0);for(let i=0;i<left;i++)values[order[i]]++;return values}
function fold(value){return String(value).toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g,"").replace(/đ/g,"d")}
function randomRates(field){let weights=field.options.map(()=>.8+Math.random()*.5);const labels=field.options.map(fold);const scores=labels.map(label=>label.includes("khong dong y")?label.includes("hoan toan")?1:2:label.includes("dong y")?label.includes("hoan toan")?5:4:label.includes("binh thuong")||label.includes("trung lap")?3:0);if(scores.length===5&&scores.slice().sort().join()==="1,2,3,4,5")weights=scores.map(score=>[12,17,25,28,18][score-1]+Math.random()*6-3);else if(field.kind==="scale"&&field.options.length>=3)weights=field.options.map((_,i)=>Math.exp(-.5*Math.pow((i/(field.options.length-1)-.62)/.30,2))*(.9+Math.random()*.2));else if(labels.some(v=>/^(co|yes)\b/.test(v))&&labels.some(v=>/^(khong|no)\b/.test(v)))weights=labels.map(v=>/^(co|yes)\b/.test(v)?1.5+Math.random()*.2:1);if(["multiple","grid_multiple"].includes(field.kind)){const low=field.minimum*100,high=field.maximum*100;const total=Math.min(high,Math.max(low,140+Math.floor(Math.random()*100)));const values=weights.map(()=>0);for(let n=0;n<total;n++){const available=weights.map((_,i)=>i).filter(i=>values[i]<100);const sum=available.reduce((a,i)=>a+weights[i],0);let point=Math.random()*sum,chosen=available[available.length-1];for(const i of available){point-=weights[i];if(point<=0){chosen=i;break}}values[chosen]++}return values}return apportion(weights)}
function cardId(field){return "card-"+field.id.replace(/[^a-zA-Z0-9_-]/g,"-")}
function fieldNumber(field){const ids=[...new Set(state.schema.fields.map(f=>f.item_id))];const number=String(ids.indexOf(field.item_id)+1).padStart(2,"0");return field.row?number+"."+String(field.row_index+1):number}
function buildCard(field){const config=state.config[field.id],multi=["multiple","grid_multiple"].includes(field.kind);let body="";if(field.options.length){body='<div class="answers">'+field.options.map((option,i)=>'<div class="answer-row"><div class="answer-label"><i class="option-dot" style="background:'+colors[i%colors.length]+'"></i><span>'+escapeHTML(option)+'</span></div><input type="range" min="0" max="100" step="1" class="rate-range" data-field="'+field.id+'" data-option="'+i+'" value="'+config.rates[option]+'" aria-label="Tỷ lệ '+escapeHTML(option)+'"><label class="rate-box"><input type="number" min="0" max="100" step="1" class="rate-number" data-field="'+field.id+'" data-option="'+i+'" value="'+config.rates[option]+'" aria-label="Tỷ lệ mong muốn '+escapeHTML(option)+'"><span>%</span></label></div>').join("")+'</div><div class="rate-stack">'+field.options.map((_,i)=>'<i data-segment="'+i+'" style="background:'+colors[i%colors.length]+'"></i>').join("")+'</div><div class="q-bottom"><span>'+(multi?"Chọn "+field.minimum+"–"+field.maximum+" đáp án mỗi lượt":"Tỷ lệ mong muốn cho mỗi đáp án")+'</span><span class="sum"></span><button class="small-button normalize-one" data-field="'+field.id+'" type="button">Chuẩn hóa</button></div>'}else{const temporal=["date","time"].includes(field.kind);body='<div class="text-mode">'+icon("spark")+'<span>'+(temporal?"Tự tạo ngày / giờ phù hợp":"Trả lời theo hồ sơ và nội dung câu hỏi")+'</span>'+(!temporal?'<select class="text-mode-select" data-field="'+field.id+'" aria-label="Chế độ câu trả lời"><option value="auto" '+(config.mode==="auto"?"selected":"")+'>Tự động</option><option value="fixed" '+(config.mode==="fixed"?"selected":"")+'>Nội dung tự nhập</option></select>':"")+'</div>'+(!temporal?'<textarea class="fixed-text" data-field="'+field.id+'" aria-label="Nội dung tự nhập" placeholder="Nhập câu trả lời bạn muốn sử dụng..." '+(config.mode==="fixed"?"":"hidden")+'>'+escapeHTML(config.value||"")+'</textarea>':"")}
return '<article class="panel question-card" id="'+cardId(field)+'" data-field="'+field.id+'" data-page="'+field.page+'"><div class="q-top"><span class="q-label">Câu '+fieldNumber(field)+'</span><span class="q-kind">'+kinds[field.kind]+'</span>'+(field.required?'<span class="required">• Bắt buộc</span>':"")+(field.options.length?'<button class="small-button random-one" data-field="'+field.id+'" type="button">'+icon("dice")+'Ngẫu nhiên</button>':"")+'</div><h3>'+escapeHTML(field.question)+'</h3>'+(field.row?'<p class="row-name">'+escapeHTML(field.row)+'</p>':"")+body+'</article>'}
function updateCard(field){const card=$(cardId(field));if(!card)return;const valid=fieldValid(field);card.classList.toggle("invalid",!valid);if(!field.options.length)return;const rates=field.options.map(option=>Number(state.config[field.id].rates[option]));const total=rates.reduce((a,b)=>a+b,0);field.options.forEach((option,i)=>{const segment=card.querySelector('[data-segment="'+i+'"]');segment.style.width=(total?Math.max(0,rates[i])/total*100:0)+"%"});const sum=card.querySelector(".sum");sum.innerHTML=(valid?icon("check"):icon("alert"))+(Number.isFinite(total)?Number(total.toFixed(2)):"—")+"%";const multi=["multiple","grid_multiple"].includes(field.kind);card.querySelector(".normalize-one").hidden=multi;sum.title=multi?"Tổng tỷ lệ có thể vượt 100% do được chọn nhiều đáp án":"Tổng tỷ lệ phải bằng 100%"}
function renderQuestions(){if(!state.schema){$("questionList").innerHTML=emptyMarkup;return}$("questionList").innerHTML=state.schema.fields.map(buildCard).join("");state.schema.fields.forEach(updateCard);$("fieldCount").textContent=state.schema.question_count+" câu hỏi";$("formMeta").hidden=false;$("formMeta").innerHTML="<strong>"+escapeHTML(state.schema.title)+"</strong><small>"+(state.schema.provider==="microsoft"?"Microsoft Forms":state.schema.provider==="html"?"Form HTML":"Google Forms")+" · "+state.schema.pages.length+" trang · "+state.schema.fields.length+" mục trả lời · "+(state.schema.branching?"Có phân nhánh":"Tự điền đủ toàn bộ trang")+"</small>";$("toolbar").hidden=false;$("academicTools").hidden=false;$("pageFilter").innerHTML='<option value="">Mọi trang</option>'+state.schema.pages.map(page=>'<option value="'+page.index+'">Trang '+(page.index+1)+'</option>').join("");filterQuestions();renderSummary()}
function filterQuestions(){if(!state.schema)return;const term=fold($("searchQuestions").value),page=$("pageFilter").value;let count=0;for(const field of state.schema.fields){const visible=(!term||fold(field.question+" "+(field.row||"")).includes(term))&&(page===""||String(field.page)===page);const card=$(cardId(field));if(card){card.hidden=!visible;if(visible)count++}}let empty=$("noResults");if(!count&&!empty){empty=document.createElement("div");empty.id="noResults";empty.className="no-results";empty.textContent="Không có câu hỏi phù hợp."; $("questionList").append(empty)}else if(count&&empty)empty.remove()}
function renderSummary(){const count=quantity(),isActive=activeJob(),locked=Boolean(state.access?.locked),unlimited=state.access?.plan==="unlimited";$("summaryCount").innerHTML=(count===0?"∞":count===null?"—":count.toLocaleString("vi-VN"))+"<small>lượt điền</small>";$("summaryQuestions").textContent=state.schema?state.schema.question_count+" câu · "+state.schema.pages.length+" trang":"Chưa kết nối";const valid=state.schema?state.schema.fields.filter(fieldValid).length:0,total=state.schema?.fields.length||0;$("summaryValid").textContent=total?valid+"/"+total+" mục":"—";$("summaryValid").className=total?(valid===total?"good":"bad"):"";$("summaryMode").textContent=state.schema?(count>0&&!state.schema.branching&&!state.schema.fields.some(f=>f.exclusive_columns)?"Theo số lượt":"Theo xác suất"):"Theo mong muốn";const button=$("startButton");button.classList.toggle("stop",Boolean(isActive));button.disabled=isActive?state.job.status==="stopping":locked?false:!ready();button.innerHTML=icon(isActive?"stop":locked?"shield":"play")+"<span>"+(isActive?(state.job.status==="stopping"?"Đang dừng...":"Dừng lượt chạy"):locked?"Nhập key để tiếp tục":state.remaining>0?"Bắt đầu ngay":state.schema?"Bắt đầu điền form":"Dán link để bắt đầu")+"</span>";$("testButton").disabled=!ready()||isActive;$("randomAll").disabled=!state.schema||isActive;$("balanceAll").disabled=!state.schema||isActive;$("undoRates").disabled=!state.undo.length||isActive;$("formUrl").disabled=Boolean(isActive)||locked;$("quantity").disabled=Boolean(isActive)||locked;$("minusCount").disabled=Boolean(isActive)||locked;$("plusCount").disabled=Boolean(isActive)||locked||(!unlimited&&count>=state.access?.remaining);$("scanAgain").disabled=Boolean(isActive)||state.scanning||locked;document.querySelectorAll(".provider-tabs button").forEach(el=>el.disabled=Boolean(isActive));document.querySelectorAll(".question-card input,.question-card select,.question-card textarea,.question-card button").forEach(el=>el.disabled=Boolean(isActive)||locked);for(const id of ["ratioPreset","applyPreset"])$(id).disabled=Boolean(isActive)||!state.schema||locked;updateRunOptions();const badCount=Boolean(state.access)&&!isActive&&!locked&&!allowedCount();$("quantityError").hidden=!badCount;$("quantityError").textContent=badCount?(unlimited?"Nhập số nguyên từ 0 trở lên.":"Bạn còn "+state.access.remaining+" lượt miễn phí. Chọn từ 1 đến "+state.access.remaining+" hoặc mở khóa unlimited."):""}
function cancelCountdown(){clearInterval(state.autoTimer);state.autoTimer=null;state.remaining=0;$("countdown").hidden=true;renderSummary()}
function scheduleAuto(){persistDraft();cancelCountdown();if(!state.access||state.access.locked||!state.armed||!$("autoRun").checked||!ready()||activeJob())return;state.remaining=12;$("countdown").hidden=false;function paint(){$("countdown").innerHTML="Tự bắt đầu sau <strong>"+state.remaining+" giây</strong>. Chỉnh tỷ lệ để đặt lại thời gian. <button type='button' id='pauseAuto'>Tạm dừng</button>";renderSummary()}paint();state.autoTimer=setInterval(()=>{state.remaining--;if(state.remaining<=0){cancelCountdown();startJob(false).catch(error=>toast(error.message))}else paint()},1000)}
function rememberRates(){state.undo.push(JSON.stringify(state.config));if(state.undo.length>25)state.undo.shift();$("undoRates").disabled=false}
function persistDraft(){if(state.scanId)localStorage.setItem("formpilot.draft",JSON.stringify({scanId:state.scanId,config:state.config}))}
function undoRates(){if(!state.undo.length||activeJob())return;state.config=JSON.parse(state.undo.pop());renderQuestions();$("undoRates").disabled=!state.undo.length;persistDraft();scheduleAuto();toast("Đã hoàn tác thay đổi tỷ lệ.")}
function updateRate(fieldId,optionIndex,value){rememberRates();const field=state.schema.fields.find(f=>f.id===fieldId);if(!field)return;const rate=Number(value);state.config[fieldId].rates[field.options[optionIndex]]=rate;const card=$(cardId(field));card.querySelectorAll('[data-option="'+optionIndex+'"]').forEach(input=>{if(input!==document.activeElement)input.value=value});updateCard(field);renderSummary();persistDraft();scheduleAuto()}
function refreshCard(field){const old=$(cardId(field));if(old){old.outerHTML=buildCard(field);updateCard(field)}}
function normalizeField(field,balanced=false){if(!field.options.length)return;if(["multiple","grid_multiple"].includes(field.kind)){if(balanced){const target=Math.min(field.maximum*100,Math.max(field.minimum*100,180));let values=apportion(field.options.map(()=>1),target);state.config[field.id].rates=Object.fromEntries(field.options.map((v,i)=>[v,values[i]]))}return}const values=apportion(field.options.map(option=>balanced?1:Math.max(0,Number(state.config[field.id].rates[option])||0)));state.config[field.id].rates=Object.fromEntries(field.options.map((v,i)=>[v,values[i]]))}
async function scanForm(){const url=$("formUrl").value.trim();const detected=providerFromLink(url);if(detected&&detected!==state.providerMode)selectProvider(detected);cancelCountdown();if(activeJob())return;if(state.access?.locked){openKeyModal("Bạn đã dùng hết 10 lượt miễn phí.");return}if(!validLink(url)){status(url?"Vui lòng nhập link hợp lệ cho loại biểu mẫu đã chọn.":"Google Forms và Microsoft Forms công khai",url?"error":"");return}const token=++state.scanToken;state.scanning=true;state.armed=true;state.schema=null;state.config={};$("formMeta").hidden=true;$("toolbar").hidden=true;$("academicTools").hidden=true;$("fieldCount").textContent="Đang đọc...";$("questionList").innerHTML='<div class="loading-skeleton" aria-label="Đang đọc câu hỏi"></div>';status("Đang đọc câu hỏi và tất cả các trang...","loading");renderSummary();try{const result=await api("/api/scan",{url,provider:state.providerMode});if(token!==state.scanToken)return;state.scanId=result.scan_id;const poll=async()=>{if(token!==state.scanToken)return;try{const value=await api("/api/scans/"+state.scanId);if(token!==state.scanToken)return;if(value.status==="scanning"){status(value.queue_waiting?"Biểu mẫu đang xếp hàng đọc"+(value.queue_position?" · vị trí "+value.queue_position:"")+"...":"Đang đọc câu hỏi và tất cả các trang...","loading");setTimeout(poll,1500);return}if(value.status==="error")throw Error(value.error);state.schema=value.schema;state.config=value.config;state.undo=[];$("undoRates").disabled=true;state.scanning=false;localStorage.setItem("formpilot.scan",state.scanId);localStorage.setItem("formpilot.url",url);renderQuestions();status("Đã đọc "+value.schema.question_count+" câu hỏi qua "+value.schema.pages.length+" trang.","success");scheduleAuto()}catch(error){if(token===state.scanToken)scanError(error)}};poll()}catch(error){if(token===state.scanToken)scanError(error)}}
function scanError(error){state.scanning=false;state.schema=null;state.config={};state.armed=false;status(error.message,"error");$("fieldCount").textContent="0 câu hỏi";$("questionList").innerHTML=emptyMarkup;renderSummary();toast(error.message)}
async function startJob(checkOnly){cancelCountdown();if(state.access?.locked){openKeyModal();return}if(!ready()||activeJob())return;state.armed=false;const count=quantity(),schedule=runSchedule(),seed=runSeed();try{const result=await api("/api/jobs",{scan_id:state.scanId,count,config:state.config,schedule,seed,engine:$("engineMode").value,reference_date:runReferenceDate(),headless:!$("showBrowser").checked,mode:checkOnly?"check":"send"});localStorage.setItem("formpilot.job",result.job_id);state.pollErrors=0;state.job={id:result.job_id,mode:result.mode,status:"queued",processed:0,total:result.mode==="check"?1:count,events:[],schedule,seed,engine:state.providerMode==="google"?$("engineMode").value:"compatible"};$("statsPanel").hidden=true;$("reportContent").textContent="Bấm để xem thống kê của đợt này.";renderJob();pollJob()}catch(error){toast(error.message);renderSummary()}}
function queuePoll(delay){clearTimeout(state.pollTimer);state.pollTimer=setTimeout(pollJob,delay)}
async function pollJob(){if(!state.job)return;if(state.polling){queuePoll(500);return;}clearTimeout(state.pollTimer);state.polling=true;const id=state.job.id;try{const value=await api("/api/jobs/"+id);if(state.job?.id!==id)return;state.job=value;state.pollErrors=0;renderJob();if(activeJob())queuePoll(document.hidden?15000:state.job.waiting||state.job.paused||state.job.queue_waiting?5000:2000)}catch(error){state.pollErrors++;if(error.status===401||error.status===404||error.message.includes("Không tìm thấy lượt chạy")){state.job.status="error";state.job.error="Phiên chạy không còn trên máy chủ. Kiểm tra kết quả đã lưu trước khi chạy lại.";renderJob()}else{if(state.pollErrors===1)toast("Mất kết nối tiến độ. Công cụ sẽ tự nối lại, không gửi lại yêu cầu chạy.");$("runMeta").textContent="Đang kết nối lại với máy chủ...";queuePoll(Math.min(30000,2000*2**Math.min(4,state.pollErrors)))}}finally{state.polling=false}}
function secondsLabel(value){return Number(value).toLocaleString("vi-VN",{maximumFractionDigits:2})+" giây"}
function renderResponseTiming(){const job=state.job,node=$("responseTiming");if(!job||!Number.isFinite(job.last_response_seconds)){node.textContent="";return}const m=job.last_performance||{},name=job.engine==="fast"?"Nhanh":"Tương thích";const parts=["Xử lý: "+secondsLabel(job.last_response_seconds),name];if(Number.isFinite(job.average_response_seconds))parts.push("TB "+secondsLabel(job.average_response_seconds)+"/lượt");if(Number.isFinite(job.last_start_interval_seconds))parts.push("Hai lần bắt đầu: "+secondsLabel(job.last_start_interval_seconds));if(Number.isFinite(job.last_confirmation_interval_seconds))parts.push("Hai xác nhận: "+secondsLabel(job.last_confirmation_interval_seconds));if(job.processing_overrun_seconds>.05&&job.target_interval_seconds>0)parts.push("Xử lý vượt nhịp "+secondsLabel(job.processing_overrun_seconds));if(job.last_schedule_overrun_seconds>.05)parts.push("Chờ quá lịch "+secondsLabel(job.last_schedule_overrun_seconds));node.textContent=parts.join(" · ");node.title=[["load_ms","Tải"],["fill_ms","Điền"],["navigation_ms","Chuyển trang"],["submit_ms","Xác nhận"]].filter(([key])=>Number.isFinite(m[key])).map(([key,label])=>label+": "+secondsLabel(m[key]/1000)).join(" · ")+(Number.isFinite(m.batch_fields)?" · Điền nhanh: "+m.batch_fields+" ô; tương thích: "+(m.fallback_fields||0)+" ô":"")+" · Thời gian xử lý gồm tạo/đóng phiên và kiểm tra hạn mức; không gồm hàng đợi."}
function renderJob(){const job=state.job;if(!job)return;$("jobPanel").hidden=false;const labels={queued:"ĐANG XẾP HÀNG",running:"ĐANG THỰC HIỆN",stopping:"ĐANG DỪNG",stopped:"ĐÃ DỪNG",completed:"HOÀN TẤT",error:"CẦN KIỂM TRA"};$("jobStatus").textContent=labels[job.status]||job.status;$("jobStatus").className="status-chip "+(activeJob()&&job.paused?"paused":activeJob()&&job.waiting?"waiting":job.status);if(activeJob()&&job.paused)$("jobStatus").textContent="TẠM DỪNG";else if(activeJob()&&job.waiting)$("jobStatus").textContent="ĐANG CHỜ";else if(activeJob()&&job.queue_waiting)$("jobStatus").textContent="ĐANG XẾP HÀNG";$("jobDone").textContent=(job.processed||0).toLocaleString("vi-VN");$("jobTotal").textContent="/ "+(job.total||"∞")+" lượt";const percent=job.total?Math.min(100,(job.processed||0)/job.total*100):0;$("jobBar").classList.toggle("indeterminate",!job.total&&activeJob());$("jobBar").firstElementChild.style.width=percent+"%";$("jobDetail").textContent=job.error|| (job.status==="completed"?(job.mode==="check"?"Đã điền đủ các trang, dừng trước Gửi. Chưa gửi phản hồi.":"Biểu mẫu đã xác nhận "+job.confirmed+" lượt gửi thành công."):job.current_question?("Trang "+job.current_page+" · "+job.current_question):job.status==="stopped"?"Lượt chạy đã dừng.":"Đang chuẩn bị công cụ...");$("jobEvents").innerHTML=(job.events||[]).slice(-6).map(event=>'<div class="event '+escapeHTML(event.level)+'"><span>'+escapeHTML(event.message)+'</span></div>').join("");const events=$("jobEvents");events.scrollTop=events.scrollHeight;$("downloadLog").hidden=!job.log_path;$("downloadLog").href="/api/jobs/"+job.id+"/download";const sav=state.spssAvailable&&job.confirmed>0;$("downloadSpss").hidden=!sav;$("spssNote").hidden=!sav;$("downloadSpss").href="/api/jobs/"+job.id+"/export.sav";$("downloadCsv").hidden=!state.csvAvailable||!job.confirmed;$("downloadCsv").href="/api/jobs/"+job.id+"/export.zip";$("statsPanel").hidden=!job.confirmed;$("pauseButton").hidden=!activeJob();$("pauseButton").disabled=job.status==="stopping";$("pauseButton").textContent=job.pause_requested?"Tiếp tục":"Tạm dừng";$("runMeta").textContent=(job.seed!==null&&job.seed!==undefined?"Seed: "+job.seed+" · ":"")+(job.elapsed_seconds!==undefined?"Đã chạy "+formatDuration(job.elapsed_seconds):"");paintRunTiming();renderResponseTiming();renderSummary()}
$("formUrl").addEventListener("input",()=>{clearTimeout(urlTimer);cancelCountdown();state.scanToken++;state.armed=false;if(validLink($("formUrl").value.trim()))urlTimer=setTimeout(scanForm,850);else{state.schema=null;state.config={};state.scanning=false;$("questionList").innerHTML=emptyMarkup;$("toolbar").hidden=true;$("academicTools").hidden=true;$("formMeta").hidden=true;$("fieldCount").textContent="0 câu hỏi";status("Dán link Google Forms hoặc Microsoft Forms để bắt đầu.");renderSummary()}});
$("scanAgain").addEventListener("click",scanForm);
document.querySelectorAll(".provider-tabs button").forEach(button=>button.addEventListener("click",()=>selectProvider(button.dataset.provider,true)));
document.querySelector(".provider-tabs").addEventListener("keydown",event=>{if(!["ArrowLeft","ArrowRight","Home","End"].includes(event.key)||activeJob())return;const buttons=[...document.querySelectorAll(".provider-tabs button")];let i=buttons.indexOf(document.activeElement);if(i<0)return;event.preventDefault();i=event.key==="Home"?0:event.key==="End"?buttons.length-1:(i+(event.key==="ArrowRight"?1:-1)+buttons.length)%buttons.length;buttons[i].click();buttons[i].focus()});
$("quantity").addEventListener("input",()=>{localStorage.setItem("formpilot.count",$("quantity").value);renderSummary();scheduleAuto()});
$("minusCount").addEventListener("click",()=>{$("quantity").value=Math.max(1,(quantity()||1)-1);$("quantity").dispatchEvent(new Event("input"))});
$("plusCount").addEventListener("click",()=>{$("quantity").value=(quantity()||0)+1;$("quantity").dispatchEvent(new Event("input"))});
$("autoRun").addEventListener("change",()=>{if($("autoRun").checked)state.armed=true;scheduleAuto()});
$("showBrowser").addEventListener("change",scheduleAuto);
$("randomAll").addEventListener("click",()=>{if(!state.schema||activeJob())return;rememberRates();state.schema.fields.forEach(field=>{if(field.options.length){const values=randomRates(field);state.config[field.id].rates=Object.fromEntries(field.options.map((v,i)=>[v,values[i]]));refreshCard(field)}});filterQuestions();renderSummary();scheduleAuto();toast("Đã tạo tỷ lệ ngẫu nhiên cho các câu hỏi.")});
$("balanceAll").addEventListener("click",()=>{rememberRates();state.schema.fields.forEach(field=>{normalizeField(field,true);refreshCard(field)});filterQuestions();renderSummary();scheduleAuto();toast("Đã chia đều tỷ lệ giữa các đáp án.")});
$("questionList").addEventListener("input",event=>{const node=event.target;if(node.matches(".rate-number,.rate-range"))updateRate(node.dataset.field,Number(node.dataset.option),node.value);if(node.matches(".fixed-text")){state.config[node.dataset.field].value=node.value;updateCard(state.schema.fields.find(f=>f.id===node.dataset.field));renderSummary();scheduleAuto()}});
$("questionList").addEventListener("change",event=>{if(event.target.matches(".text-mode-select")){const field=state.schema.fields.find(f=>f.id===event.target.dataset.field);state.config[field.id].mode=event.target.value;refreshCard(field);renderSummary();scheduleAuto()}});
$("questionList").addEventListener("click",event=>{const random=event.target.closest(".random-one"),normalize=event.target.closest(".normalize-one");const node=random||normalize;if(!node||activeJob())return;const field=state.schema.fields.find(f=>f.id===node.dataset.field);rememberRates();if(random){const values=randomRates(field);state.config[field.id].rates=Object.fromEntries(field.options.map((v,i)=>[v,values[i]]))}else normalizeField(field);refreshCard(field);renderSummary();scheduleAuto()});
$("countdown").addEventListener("click",event=>{if(event.target.closest("#pauseAuto")){$("autoRun").checked=false;cancelCountdown();toast("Đã tạm dừng tự chạy. Bạn có thể điều chỉnh tỷ lệ.")}});
$("undoRates").addEventListener("click",undoRates);document.addEventListener("keydown",event=>{if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==="z"&&!event.target.matches("input,textarea,select")){event.preventDefault();undoRates()}});
$("searchQuestions").addEventListener("input",filterQuestions);$("pageFilter").addEventListener("change",filterQuestions);
$("testButton").addEventListener("click",()=>startJob(true));
function paintRunTiming(){const job=state.job;if(!job||!activeJob()||job.status==="stopping")return;if(job.paused)$("jobDetail").textContent="Đã tạm dừng. Khi tiếp tục, phản hồi chưa gửi sẽ được điền lại với cùng đáp án.";else if(job.pause_requested)$("jobDetail").textContent="Sẽ tạm dừng ở bước kiểm tra tiếp theo; yêu cầu gửi đang diễn ra có thể hoàn tất.";else if(job.queue_waiting){$("jobDetail").textContent="Đang xếp hàng"+(job.queue_position?" · vị trí "+job.queue_position:"")+". Bạn có thể giữ trang mở hoặc quay lại sau. Chờ hàng đợi không trừ lượt miễn phí."}else if(job.waiting){const seconds=job.next_run_at?Math.max(0,job.next_run_at-Date.now()/1000):job.remaining_seconds;$("jobDetail").textContent="Lượt tiếp theo sau "+formatDuration(seconds)+". Bạn có thể tạm dừng hoặc dừng đợt chạy."}}
$("pauseButton").addEventListener("click",async()=>{if(!activeJob())return;const action=state.job.pause_requested?"resume":"pause";$("pauseButton").disabled=true;try{state.job=await api("/api/jobs/"+state.job.id+"/"+action,{});renderJob()}catch(error){toast(error.message);$("pauseButton").disabled=false}});
$("timingPreset").addEventListener("change",()=>{const value=$("timingPreset").value,map={maximum:[0,0],fast:[2,2],fixed3:[3,3],fixed30:[30,30],fixed60:[60,60],random30_180:[30,180],fixed180:[180,180]};if(map[value]){$("intervalMin").value=map[value][0];$("intervalMax").value=map[value][1]}persistRunOptions();renderSummary();scheduleAuto()});
for(const id of ["engineMode","timingBasis","intervalMin","intervalMax","sampleSeed","referenceDate"])$(id).addEventListener("input",()=>{if(!["engineMode","timingBasis","sampleSeed","referenceDate"].includes(id))$("timingPreset").value=["random30_180","custom_random"].includes($("timingPreset").value)?"custom_random":"custom_fixed";persistRunOptions();renderSummary();scheduleAuto()});
$("applyPreset").addEventListener("click",async()=>{if(!state.scanId||activeJob())return;cancelCountdown();const scanId=state.scanId,token=state.scanToken,draft=JSON.stringify(state.config);$("applyPreset").disabled=true;try{const value=await api("/api/scans/"+scanId+"/preset",{preset:$("ratioPreset").value,config:JSON.parse(draft)});if(scanId!==state.scanId||token!==state.scanToken)return;if(JSON.stringify(state.config)!==draft){toast("Tỷ lệ đã được sửa trong khi chờ. Hãy áp dụng mẫu lại nếu cần.");return}rememberRates();state.config=value.config;renderQuestions();persistDraft();scheduleAuto();toast("Đã áp dụng mẫu phân phối. Bạn vẫn có thể chỉnh từng câu.")}catch(error){toast(error.message)}finally{renderSummary()}});
$("loadReport").addEventListener("click",async()=>{if(!state.job?.confirmed)return;const jobId=state.job.id;$("loadReport").disabled=true;try{const report=await api("/api/jobs/"+jobId+"/report");if(state.job?.id!==jobId)return;const f=v=>v===null?"—":Number(v).toLocaleString("vi-VN",{maximumFractionDigits:3});$("reportContent").innerHTML='<p>'+report.confirmed+' phản hồi đã xác nhận · Dữ liệu mô phỏng</p><table class="stats-table"><thead><tr><th>Câu hỏi</th><th>N hợp lệ</th><th>Thiếu</th><th>TB</th><th>SD mẫu</th></tr></thead><tbody>'+report.fields.map(row=>'<tr><td>'+escapeHTML(row.question)+(row.row?' · '+escapeHTML(row.row):'')+'<details><summary>Tần suất đáp án</summary>'+row.frequencies.map(item=>'<p>'+escapeHTML(item.option)+(row.score_map&&row.score_map[item.option]!==undefined?' [mã '+f(row.score_map[item.option])+']':'')+': '+item.count+' ('+f(item.percent)+'%)</p>').join('')+'</details></td><td>'+f(row.valid_n)+'</td><td>'+f(row.missing)+'</td><td>'+f(row.mean)+'</td><td>'+f(row.stddev)+'</td></tr>').join('')+'</tbody></table>'}catch(error){toast(error.message)}finally{$("loadReport").disabled=false}});
async function downloadResult(event,kind){event.preventDefault();const link=event.currentTarget;if(link.getAttribute("aria-busy")==="true")return;link.setAttribute("aria-busy","true");const jobId=state.job.id,href=link.href;try{const response=await fetch(href);if(!response.ok){let info={};try{info=await response.json()}catch{}throw Error(info.error||"Chưa xuất được dữ liệu.")}const blob=await response.blob(),url=URL.createObjectURL(blob),a=document.createElement("a");a.href=url;a.download="KDautoform_"+jobId+"."+kind;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);toast(kind==="sav"?"Đã tải .sav. Mở trong SPSS bằng File → Open → Data.":"Đã tải CSV, bảng mã và báo cáo trong file ZIP.")}catch(error){toast(error.message)}finally{link.removeAttribute("aria-busy")}}
$("downloadSpss").addEventListener("click",event=>downloadResult(event,"sav"));$("downloadCsv").addEventListener("click",event=>downloadResult(event,"zip"));
$("startButton").addEventListener("click",async()=>{if(activeJob()){try{state.job=await api("/api/jobs/"+state.job.id+"/stop",{});renderJob()}catch(error){toast(error.message)}}else if(state.access?.locked){openKeyModal()}else startJob(false)});
async function init(){restoreRunOptions();const oldCount=localStorage.getItem("formpilot.count");if(oldCount!==null&&Number.isInteger(Number(oldCount))&&Number(oldCount)>=0)$("quantity").value=oldCount;$("formUrl").value=localStorage.getItem("formpilot.url")||"";selectProvider(providerFromLink($("formUrl").value)||localStorage.getItem("formpilot.provider")||"google");try{const info=await api("/api/state");state.forceCheck=info.force_check;$("serverCapacity").textContent="Máy chủ xử lý tối đa "+info.browser_workers+" phản hồi cùng lúc; các lượt còn lại tự xếp hàng.";state.spssAvailable=Boolean(info.spss_export);state.csvAvailable=Boolean(info.csv_export);if(info.plan!=="unlimited"&&(quantity()===null||quantity()===0||quantity()>info.remaining))$("quantity").value=Math.max(1,info.remaining);$("showBrowser").checked=info.headless_only?false:$("showBrowser").checked;$("showBrowser").disabled=Boolean(info.headless_only);$("showBrowser").closest("label").hidden=Boolean(info.headless_only);if(info.force_check){$("testButton").textContent="Máy chủ đang ở chế độ kiểm tra";$("startButton").title="Lượt chạy dừng trước nút Gửi"}const scanId=localStorage.getItem("formpilot.scan"),jobId=localStorage.getItem("formpilot.job");if(scanId){try{const scan=await api("/api/scans/"+scanId);if(scan.status==="ready"){state.scanId=scanId;state.schema=scan.schema;state.config=scan.config;try{const draft=JSON.parse(localStorage.getItem("formpilot.draft")||"null");if(draft&&draft.scanId===scanId&&Object.keys(draft.config).length===scan.schema.fields.length)state.config=draft.config}catch{}renderQuestions();status("Biểu mẫu đã sẵn sàng. Không tự chạy khi tải lại trang.","success")}}catch{localStorage.removeItem("formpilot.scan")}}if(jobId){try{state.job=await api("/api/jobs/"+jobId);renderJob();if(activeJob())pollJob()}catch{localStorage.removeItem("formpilot.job")}}}catch(error){status("Chưa kết nối được máy chủ. Vui lòng tải lại trang.","error");$("usageDetail").textContent="Chưa kết nối được máy chủ. Vui lòng thử tải lại trang."}renderSummary()}
let keyReturnFocus=null;
function applyAccess(info){
const previous=state.access;state.access={plan:info.plan,remaining:info.remaining,used:info.used,free_limit:info.free_limit,locked:info.locked,key_name:info.key_name};
if(previous&&["plan","remaining","used","free_limit","locked","key_name"].every(key=>previous[key]===state.access[key]))return;
const unlimited=info.plan==="unlimited",locked=Boolean(info.locked),strip=document.querySelector(".usage-strip");
strip.classList.toggle("is-locked",locked);strip.classList.toggle("is-unlimited",unlimited);
$("planLabel").textContent=unlimited?"Unlimited đã mở khóa":locked?"Bạn đã dùng hết 10 lượt miễn phí":"Bạn còn "+info.remaining+" lượt miễn phí";
$("usageDetail").textContent=unlimited?"Không giới hạn số lượt và số đợt chạy. Key dùng lại được.":locked?"Bạn đã dùng hết 10 lượt miễn phí. Nhập key hoặc liên hệ Facebook/Zalo để được cấp key.":"Tự đọc form và điền thử không trừ lượt. Hạn mức được giữ khi tải lại trang.";
$("usageCount").textContent=unlimited?"∞ · Không giới hạn":info.used+" / "+info.free_limit+" lượt đã dùng";
$("usageProgress").max=info.free_limit;$("usageProgress").value=info.used;$("usageProgress").hidden=unlimited;
$("upgradeButton").innerHTML=(unlimited?"Quản lý key":"Mở khóa unlimited")+" "+icon("arrow");
$("logoutKey").querySelector("span").textContent=unlimited?"Unlimited":"Nhập key";
$("logoutKey").title=unlimited?"Key: "+info.key_name:"Nhập key unlimited";
$("paidKeyControls").hidden=!unlimited;$("quantity").min=unlimited?"0":"1";
if(unlimited)$("quantity").removeAttribute("max");else $("quantity").max=Math.max(1,info.remaining);
$("quantityHint").textContent=unlimited?"Nhập 0 để chạy liên tục":"Còn "+info.remaining+" / "+info.free_limit+" lượt miễn phí";
if(locked)cancelCountdown();renderSummary();
}
function openKeyModal(message=""){
clearTimeout(urlTimer);cancelCountdown();state.armed=false;keyReturnFocus=document.activeElement;
$("accessError").textContent=message;$("accessKey").removeAttribute("aria-invalid");
$("accessGate").hidden=false;$("workspaceApp").inert=true;document.body.classList.add("modal-open");
setTimeout(()=>$("accessKey").focus(),30);
}
function closeKeyModal(){
$("accessGate").hidden=true;$("workspaceApp").inert=false;document.body.classList.remove("modal-open");
if(keyReturnFocus?.isConnected)keyReturnFocus.focus();
}
async function unlockAccess(info){
applyAccess(info);$("accessKey").value="";$("accessError").textContent="";closeKeyModal();toast("Đã mở khóa unlimited. Bạn có thể chạy nhiều đợt bằng key này.");
}
function authInit(){init()}
function normalizeKey(value){
const text=String(value||"").normalize("NFKC").replace(/[‐‑‒–—―−﹣－]/g,"-").replace(/[\s\u00ad\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]/g,"").toUpperCase();
const match=text.match(/KD-?([0-9A-F]{8})-?([0-9A-F]{8})-?([0-9A-F]{8})-?([0-9A-F]{8})(?![0-9A-F])/);
return match?"KD-"+match.slice(1).join("-"):text;
}
$("accessForm").addEventListener("submit",async event=>{
event.preventDefault();const key=normalizeKey($("accessKey").value);if(!key)return;$("accessKey").value=key;
$("activateKey").disabled=true;$("accessError").textContent="Đang kiểm tra key...";
try{const info=await api("/api/auth/login",{key});await unlockAccess(info)}catch(error){$("accessError").textContent=error.message;$("accessKey").setAttribute("aria-invalid","true")}finally{$("activateKey").disabled=false}
});
$("accessKey").addEventListener("paste",event=>{const key=normalizeKey(event.clipboardData?.getData("text")||"");if(/^KD-(?:[0-9A-F]{8}-){3}[0-9A-F]{8}$/.test(key)){event.preventDefault();$("accessKey").value=key;$("accessKey").dispatchEvent(new Event("input",{bubbles:true}))}});
$("accessKey").addEventListener("input",()=>{$("accessKey").removeAttribute("aria-invalid");$("accessError").textContent=""});
$("toggleKey").addEventListener("click",()=>{const show=$("accessKey").type==="password";$("accessKey").type=show?"text":"password";$("toggleKey").textContent=show?"Ẩn":"Hiện";$("toggleKey").setAttribute("aria-pressed",String(show));$("accessKey").focus()});
for(const id of ["logoutKey","upgradeButton","heroUpgrade"])$(id).addEventListener("click",()=>openKeyModal());
$("closeKey").addEventListener("click",closeKeyModal);
$("accessGate").addEventListener("click",event=>{if(event.target===$("accessGate"))closeKeyModal()});
$("deactivateKey").addEventListener("click",async()=>{try{await api("/api/auth/logout",{});closeKeyModal();toast("Đã ngừng dùng key trên trình duyệt này. Key vẫn có thể nhập lại.")}catch(error){toast(error.message)}});
document.addEventListener("keydown",event=>{if($("accessGate").hidden)return;if(event.key==="Escape"){event.preventDefault();closeKeyModal()}if(event.key==="Tab"){const nodes=[...$("accessGate").querySelectorAll("button,a[href],input")].filter(el=>!el.disabled&&el.getClientRects().length);const first=nodes[0],last=nodes[nodes.length-1];if(event.shiftKey&&document.activeElement===first){event.preventDefault();last.focus()}else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus()}}});

const runClock=setInterval(paintRunTiming,1000);document.addEventListener("visibilitychange",()=>{if(!document.hidden&&activeJob()){clearTimeout(state.pollTimer);pollJob()}});
window.addEventListener("beforeunload",()=>{clearInterval(state.autoTimer);clearTimeout(state.pollTimer);clearInterval(runClock)});window.addEventListener("pageshow",event=>{if(event.persisted)location.reload()});authInit();
</script>
</body></html>"""

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
        self.exact = total > 0 and not schema["branching"] and not any(
            field["exclusive_columns"] for field in schema["fields"])
        self.remaining = {}
        if self.exact:
            for field in schema["fields"]:
                if not field["options"]:
                    continue
                percents = [self.config[field["id"]]["rates"][option] for option in field["options"]]
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

    def reconcile_profile(self, answers, profile, rng, index):
        for field in self.schema["fields"]:
            if field["kind"] not in ("single", "dropdown"):
                continue
            value = answers[field["id"]]
            intent = text_intent(field)
            label = plain(value)
            if intent == "gender" and label in ("nam", "nu", "male", "female", "man", "woman"):
                gender = "Nam" if label in ("nam", "male", "man") else "Nữ"
                if profile["gender"] != gender:
                    for _ in range(100):
                        alternate = make_profile(self.schema, rng, index, self.base.reference_date)
                        if alternate["gender"] == gender:
                            for key in ("full_name", "surname", "given_name", "middle_name", "gender"):
                                profile[key] = alternate[key]
                            birth_year = profile["birth_date"]["year"]
                            profile["email"] = re.sub(r"[^a-z0-9]", "", plain(profile["full_name"])) + str(birth_year) + "@example.com"
                            break
            elif intent in ("school", "faculty", "occupation"):
                profile[intent] = value
            elif intent == "student_year":
                for year in range(1, 5):
                    test_profile = dict(profile, student_year=year)
                    if coherent_choice(field, test_profile) == value:
                        profile["student_year"] = year
                        break

    def sample(self, index):
        answers = self.base.sample(index)
        profile = dict(self.base.last_profile)
        rng = random.Random(f"web:{self.seed}:{index}")
        used_columns = defaultdict(set)
        groups = Counter(field["item_id"] for field in self.schema["fields"])
        for field in self.schema["fields"]:
            if not field["options"]:
                continue
            options = field["options"]
            available = [i for i, value in enumerate(options)
                         if not field["exclusive_columns"] or value not in used_columns[field["item_id"]]]
            if not available:
                raise FormError("Không còn cột hợp lệ trong bảng: " + field["question"])
            rates = [self.config[field["id"]]["rates"][option] for option in options]
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
                    while len(selected) < amount:
                        candidates = [i for i, value in enumerate(remaining) if value > 0 and i not in selected]
                        selected.append(rng.choices(candidates, weights=[remaining[i] for i in candidates])[0])
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
                for _ in range(amount):
                    candidates = [i for i in available if i not in selected and rates[i] > 0]
                    if not candidates:
                        raise FormError("Tỷ lệ không đủ đáp án hợp lệ trong bảng: " + field["question"])
                    selected.append(rng.choices(candidates, weights=[rates[i] for i in candidates])[0])
            else:
                selected = [rng.choices(available, weights=[rates[i] for i in available])[0]]
            value = [options[i] for i in selected] if multi else options[selected[0]]
            answers[field["id"]] = value
            if field["exclusive_columns"]:
                used_columns[field["item_id"]].update(value if isinstance(value, list) else [value])
            groups[field["item_id"]] -= 1
        self.reconcile_profile(answers, profile, rng, index)
        for field in self.schema["fields"]:
            entry = self.config[field["id"]]
            if field["kind"] in ("text", "paragraph"):
                answers[field["id"]] = entry["value"] if entry.get("mode") == "fixed" else sample_text(field, rng, index, profile)
            elif field["kind"] == "date" and text_intent(field) == "birth_date":
                answers[field["id"]] = dict(profile["birth_date"])
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
                       "provider": schema.get("provider", "google"), "schedule": job["schedule"],
                       "reference_date": job["reference_date"], "engine": job["engine"]})
        sampler, log = context["sampler"], context["log"]
        index = job["processed"] + 1
        if "pending" not in context:
            context["pending"] = {"answers": sampler.sample(index), "profile": sampler.last_profile}
        pending = context["pending"]
        answers, profile = pending["answers"], pending["profile"]
        def checkpoint():
            if control.stopped or not active_job_key(job, app.key_store):
                raise WebStopRequested()
            if control.paused:
                raise WebPauseRequested()
        class ProgressBrowser(form_browser(schema["url"])):
            def before_field(self, field):
                checkpoint()
                with app.lock:
                    changed = job["current_page"] != field["page"] + 1
                    job.update(current_page=field["page"] + 1, current_question=field["question"])
                if changed:
                    app.event(job, f'Đang điền trang {field["page"] + 1}/{len(schema["pages"])}.')
            def before_navigation(self):
                checkpoint()
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
                                     "version": "9.3", "max_browser_workers": 10, "name": "KDautoform", "providers": ["google", "microsoft", "html"],
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





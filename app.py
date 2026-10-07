"""Gate5 PC lending. Python 3.10+, standard library only, localhost training app."""
import argparse
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
JST = timezone(timedelta(hours=9), "Asia/Tokyo")
MESSAGES = {
 "E-01": "選択した端末は存在しません。端末を選び直してください。",
 "E-02": "この端末は現在貸出中です。別の端末を選択してください。",
 "E-03": "この端末は修理中または廃棄済みのため、貸出できません。",
 "E-04": "この端末は貸出対象のノートPCではありません。",
 "E-05": "現在貸出可能な端末はありません。時間をおいて再度確認してください。",
 "E-06": "この端末は記録の確認が必要なため、操作できません。管理部に連絡してください。",
 "E-07": "在籍中の社員のみ新規貸出を利用できます。",
 "E-08": "利用者情報を確認できません。再ログインしてください。解消しない場合は管理部に連絡してください。",
 "E-09": "貸出は1人1台までです。現在の端末を返却してから申請してください。",
 "E-10": "返却期限を過ぎた端末があります。返却してから申請してください。",
 "E-11": "貸出する端末を選択してください。",
 "E-12": "入力内容が不正です。画面を開き直して操作してください。",
 "E-13": "返却予定日を入力してください。",
 "E-14": "返却予定日は有効な日付をYYYY-MM-DD形式で入力してください。",
 "E-15": "返却予定日は明日から90日以内で入力してください。",
 "E-16": "利用目的を入力してください。",
 "E-17": "利用目的は100文字以内で入力してください。",
 "E-18": "指定された貸出は操作できません。自分の貸出一覧を確認してください。",
 "E-19": "処理を完了できませんでした。入力内容は保持されています。時間をおいて再度お試しください。",
 "E-20": "処理結果を確認できません。再申請せず、「結果を確認する」を押してください。",
 "E-21": "確認内容の有効期限が切れているか、確認が完了していません。入力内容を確認し直してください。",
 "E-22": "確認画面の内容と送信内容が一致しません。最初から確認し直してください。",
 "E-23": "この操作は許可されていません。画面を開き直してください。",
 "E-24": "この操作では端末の状態を変更できません。貸出または返却の画面から操作してください。",
 "E-25": "利用目的に使用できない制御文字が含まれています。",
 "S-03": "この貸出は既に返却済みです。追加の更新は行っていません。",
 "I-01": "現在借りている端末はありません。",
}
LOG = logging.getLogger("gate5")


class Connection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


class RuleError(Exception):
    def __init__(self, code, status=400, fields=None):
        self.code, self.status, self.fields = code, status, fields or {}
        super().__init__(MESSAGES[code])

    def response(self):
        return {"ok": False, "code": self.code, "message": str(self),
                "fields": {k: {"code": v, "message": MESSAGES[v]} for k, v in self.fields.items()}}


def utcnow():
    return datetime.now(timezone.utc)


def stamp(value):
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def business_date(value):
    return value.astimezone(JST).date()


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def identifier(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise RuleError("E-12")
    if not re.fullmatch(r"[0-9]{1,19}", str(value)) or not 1 <= int(value) <= 9223372036854775807:
        raise RuleError("E-12")
    return int(value)


def allowed(payload, keys, confirmation=False):
    if not isinstance(payload, dict):
        raise RuleError("E-12")
    extra = set(payload) - set(keys)
    if "status" in extra:
        raise RuleError("E-24", 405)
    if confirmation and extra & {"device_id", "due_date", "purpose", "lending_id"}:
        raise RuleError("E-22", 409)
    if extra:
        raise RuleError("E-12")


def validate_lend(payload, now):
    allowed(payload, {"device_id", "due_date", "purpose"})
    fields, clean = {}, {}
    if payload.get("device_id") in (None, ""):
        fields["device_id"] = "E-11"
    else:
        try:
            clean["device_id"] = identifier(payload["device_id"])
        except RuleError:
            fields["device_id"] = "E-12"
    due = payload.get("due_date")
    if due in (None, ""):
        fields["due_date"] = "E-13"
    else:
        try:
            if not isinstance(due, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", due):
                raise ValueError()
            parsed = date.fromisoformat(due)
            today = business_date(now)
            if not today + timedelta(days=1) <= parsed <= today + timedelta(days=90):
                fields["due_date"] = "E-15"
            clean["due_date"] = due
        except ValueError:
            fields["due_date"] = "E-14"
    purpose = payload.get("purpose", "")
    if not isinstance(purpose, str):
        fields["purpose"] = "E-12"
    else:
        purpose = purpose.strip()
        if not purpose:
            fields["purpose"] = "E-16"
        elif any(ord(c) < 32 or 127 <= ord(c) <= 159 or 0xD800 <= ord(c) <= 0xDFFF for c in purpose):
            fields["purpose"] = "E-25"
        elif len(purpose) > 100:
            fields["purpose"] = "E-17"
        clean["purpose"] = purpose
    if fields:
        raise RuleError(next(iter(fields.values())), fields=fields)
    return clean


class Store:
    def __init__(self, path, clock=utcnow, fault=None):
        self.path, self.clock, self.fault = Path(path), clock, fault

    def connect(self):
        db = sqlite3.connect(str(self.path), timeout=5, isolation_level=None, factory=Connection)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @contextmanager
    def transaction(self):
        db = self.connect()
        try:
            # SQLite serializes writers, covering the employee/device/operation locks.
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript((ROOT / "schema.sql").read_text(encoding="utf-8"))
        with self.transaction() as db:
            if db.execute("SELECT 1 FROM app_meta WHERE key='seed_version'").fetchone():
                return
            if db.execute("SELECT 1 FROM employees UNION ALL SELECT 1 FROM devices LIMIT 1").fetchone():
                raise RuntimeError("既存データがあるDBには合成データを追加できません。別のDBファイルを指定してください。")
            now, today = self.clock(), business_date(self.clock())
            db.executemany("INSERT INTO employees VALUES(?,?,?,?)", [
                (1, "佐藤 花子", "情報システム部", "ACTIVE"),
                (2, "鈴木 一郎", "営業部", "ACTIVE"),
                (3, "田中 美咲", "技術部", "ACTIVE"),
                (4, "高橋 次郎", "営業部", "RETIRED"),
                (5, "伊藤 葵", "管理部", "LEAVE"),
                (6, "山本 蓮", "管理部", "ACTIVE")])
            for i in range(1, 21):
                status = {3: "LENT", 4: "REPAIR", 10: "DISPOSED", 12: "LENT", 18: "LENT"}.get(i, "AVAILABLE")
                db.execute("INSERT INTO devices VALUES(?,?,?,?,?,?)", (i, f"PC-{i:04}",
                    f"ノート{chr(65 + (i-1)//5)}", "LAPTOP", status, "2024-04-01"))
            db.executemany("INSERT INTO devices VALUES(?,?,?,?,?,?)", [
                (21, "TB-0001", "タブレットA", "TABLET", "AVAILABLE", "2024-04-01"),
                (22, "MN-0001", "モニターA", "MONITOR", "AVAILABLE", "2024-04-01")])
            for device, user, days in [(3, 3, -1), (12, 4, 7), (18, 5, 7)]:
                db.execute("INSERT INTO lendings(device_id,user_id,lent_at,due_date,purpose) VALUES(?,?,?,?,?)",
                    (device, user, stamp(now-timedelta(days=10)), (today+timedelta(days=days)).isoformat(), "研修用サンプル"))
            for device in [5, 7, 9, 14, 15]:
                reason = "配布資料の調査対象を再現した合成データ（原本の補正ではありません）"
                db.execute("INSERT INTO device_holds VALUES(?,?,?,NULL)", (device, reason, stamp(now)))
                db.execute("INSERT INTO maintenance_audit(device_id,operator,reason,before_json,after_json,changed_at) VALUES(?,?,?,?,?,?)",
                    (device, "合成データ初期化", reason, "null", encode({"held": True}), stamp(now)))
            db.execute("INSERT INTO app_meta VALUES('seed_version','synthetic-v1')")

    @staticmethod
    def actor(db, user_id):
        row = db.execute("SELECT * FROM employees WHERE id=?", (user_id,)).fetchone()
        if not row:
            raise RuleError("E-08", 401)
        return dict(row)

    @staticmethod
    def device(db, device_id):
        row = db.execute("""SELECT d.*, (SELECT COUNT(*) FROM lendings l WHERE l.device_id=d.id AND l.returned_at IS NULL) open_count,
            EXISTS(SELECT 1 FROM device_holds h WHERE h.device_id=d.id AND h.released_at IS NULL) held
            FROM devices d WHERE d.id=?""", (device_id,)).fetchone()
        if not row:
            raise RuleError("E-01", 404)
        return dict(row)

    @staticmethod
    def check_integrity(device):
        expected = 1 if device["status"] == "LENT" else 0
        if device["held"] or device["open_count"] != expected:
            raise RuleError("E-06", 409)

    def check_lend(self, db, user_id, clean, now):
        user = self.actor(db, user_id)
        device = self.device(db, clean["device_id"])
        self.check_integrity(device)
        if device["device_type"] != "LAPTOP":
            raise RuleError("E-04")
        if device["status"] == "LENT":
            raise RuleError("E-02", 409)
        if device["status"] != "AVAILABLE":
            raise RuleError("E-03", 409)
        if user["employment_status"] != "ACTIVE":
            raise RuleError("E-07", 403)
        loans = db.execute("SELECT due_date FROM lendings WHERE user_id=? AND returned_at IS NULL", (user_id,)).fetchall()
        if any(l["due_date"] < business_date(now).isoformat() for l in loans):
            raise RuleError("E-10", 409)
        if loans:
            raise RuleError("E-09", 409)
        return device

    @staticmethod
    def loan(db, user_id, lending_id):
        row = db.execute("""SELECT l.*,d.asset_no,d.model_name FROM lendings l JOIN devices d ON d.id=l.device_id
            WHERE l.id=? AND l.user_id=?""", (lending_id, user_id)).fetchone()
        if not row:
            raise RuleError("E-18", 403)
        return dict(row)

    def dashboard(self, user_id):
        with self.connect() as db:
            # One read transaction keeps counts, candidates and user state consistent.
            db.execute("BEGIN")
            user = self.actor(db, user_id)
            today = business_date(self.clock())
            devices = [self.device(db, r[0]) for r in db.execute("SELECT id FROM devices ORDER BY asset_no").fetchall()]
            candidates = [d for d in devices if d["device_type"] == "LAPTOP" and d["status"] == "AVAILABLE" and not d["held"] and d["open_count"] == 0]
            loans = [dict(r) for r in db.execute("""SELECT l.*,d.asset_no,d.model_name FROM lendings l JOIN devices d ON d.id=l.device_id
                WHERE l.user_id=? AND l.returned_at IS NULL ORDER BY due_date,l.id""", (user_id,))]
            for loan in loans:
                device = next(d for d in devices if d["id"] == loan["device_id"])
                loan["overdue"] = loan["due_date"] < today.isoformat()
                loan["blocked"] = bool(device["held"] or device["status"] != "LENT" or device["open_count"] != 1)
            history = [dict(r) for r in db.execute("""SELECT request_key,operation,completed_at,result_json FROM operation_requests
                WHERE user_id=? AND status='SUCCEEDED' ORDER BY completed_at DESC LIMIT 8""", (user_id,))]
            for row in history:
                row["result"] = json.loads(row.pop("result_json"))
            db.commit()
        notice = "E-07" if user["employment_status"] != "ACTIVE" else "E-10" if any(l["overdue"] for l in loans) else "E-09" if loans else None
        return {"user": user, "today": today.isoformat(), "min_date": (today+timedelta(days=1)).isoformat(),
                "max_date": (today+timedelta(days=90)).isoformat(), "devices": candidates,
                "loans": loans, "history": history, "lend_notice": notice,
                "counts": {"available": len(candidates), "mine": len(loans), "overdue": sum(l["overdue"] for l in loans)}}

    def prepare(self, user_id, operation, payload):
        with self.transaction() as db:
            self.actor(db, user_id)
            now = self.clock()
            if operation == "LEND":
                clean = validate_lend(payload, now)
                device = self.check_lend(db, user_id, clean, now)
                display = {**clean, "asset_no": device["asset_no"], "model_name": device["model_name"]}
            else:
                allowed(payload, {"lending_id"})
                clean = {"lending_id": identifier(payload.get("lending_id"))}
                loan = self.loan(db, user_id, clean["lending_id"])
                if loan["returned_at"] is None:
                    self.check_integrity(self.device(db, loan["device_id"]))
                display = loan
            key = secrets.token_urlsafe(32)
            db.execute("INSERT INTO operation_requests(request_key,user_id,operation,payload_json,created_at,expires_at) VALUES(?,?,?,?,?,?)",
                (key, user_id, operation, encode(clean), stamp(now), stamp(now+timedelta(minutes=30))))
            return {"request_key": key, "operation": operation, "display": display,
                    "expires_at": stamp(now+timedelta(minutes=30))}

    @staticmethod
    def request(db, user_id, key, operation=None):
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", key):
            raise RuleError("E-21", 409)
        row = db.execute("SELECT * FROM operation_requests WHERE request_key=?", (key,)).fetchone()
        if not row:
            raise RuleError("E-21", 409)
        if row["user_id"] != user_id:
            raise RuleError("E-23", 403)
        if operation and row["operation"] != operation:
            raise RuleError("E-22", 409)
        return dict(row)

    def result(self, user_id, key):
        with self.connect() as db:
            self.actor(db, user_id)
            row = self.request(db, user_id, key)
            if row["status"] == "SUCCEEDED":
                return {"state": "SUCCEEDED", "operation": row["operation"], "result": json.loads(row["result_json"])}
            if stamp(self.clock()) >= row["expires_at"]:
                raise RuleError("E-21", 409)
            return {"state": "READY", "operation": row["operation"], "payload": json.loads(row["payload_json"])}

    def cancel(self, user_id, key):
        with self.transaction() as db:
            self.actor(db, user_id)
            row = self.request(db, user_id, key)
            if row["status"] == "SUCCEEDED":
                return {"state": "SUCCEEDED", "result": json.loads(row["result_json"])}
            db.execute("UPDATE operation_requests SET expires_at=? WHERE request_key=?", (stamp(self.clock()), key))
        return {"state": "CANCELLED"}

    def commit(self, user_id, operation, payload):
        allowed(payload, {"request_key"}, confirmation=True)
        key, target = payload.get("request_key"), None
        try:
            with self.transaction() as db:
                self.actor(db, user_id)
                row = self.request(db, user_id, key, operation)
                if row["status"] == "SUCCEEDED":
                    return json.loads(row["result_json"])
                now = self.clock()  # acquired AFTER SQLite's writer lock
                if stamp(now) >= row["expires_at"]:
                    raise RuleError("E-21", 409)
                clean = json.loads(row["payload_json"])
                if operation == "LEND":
                    clean = validate_lend(clean, now)
                    target = clean["device_id"]
                    device = self.check_lend(db, user_id, clean, now)
                    target = device["id"]
                    cur = db.execute("INSERT INTO lendings(device_id,user_id,lent_at,due_date,purpose) VALUES(?,?,?,?,?)",
                        (target, user_id, stamp(now), clean["due_date"], clean["purpose"]))
                    lending_id = cur.lastrowid
                    if self.fault:
                        self.fault(operation)
                    changed = db.execute("UPDATE devices SET status='LENT' WHERE id=? AND status='AVAILABLE'", (target,)).rowcount
                    if changed != 1:
                        raise RuleError("E-02", 409)
                    result = {"code": "S-01", "message": f"{device['asset_no']} を貸し出しました。返却予定日は {clean['due_date'].replace('-', '/')} です。",
                        "lending_id": lending_id, "asset_no": device["asset_no"], "model_name": device["model_name"],
                        "due_date": clean["due_date"], "purpose": clean["purpose"], "lent_at": stamp(now), "operation": operation}
                else:
                    loan = self.loan(db, user_id, clean["lending_id"])
                    target, lending_id = loan["device_id"], loan["id"]
                    already = loan["returned_at"] is not None
                    if not already:
                        device = self.device(db, target)
                        self.check_integrity(device)
                        if device["status"] != "LENT" or stamp(now) < loan["lent_at"]:
                            raise RuleError("E-06", 409)
                        changed = db.execute("UPDATE lendings SET returned_at=? WHERE id=? AND user_id=? AND returned_at IS NULL",
                            (stamp(now), lending_id, user_id)).rowcount
                        if changed != 1:
                            raise RuleError("E-06", 409)
                        if self.fault:
                            self.fault(operation)
                        if db.execute("UPDATE devices SET status='AVAILABLE' WHERE id=? AND status='LENT'", (target,)).rowcount != 1:
                            raise RuleError("E-06", 409)
                    result = {"code": "S-03" if already else "S-02", "message": MESSAGES["S-03"] if already else f"{loan['asset_no']} を返却しました。",
                        "lending_id": lending_id, "asset_no": loan["asset_no"], "model_name": loan["model_name"],
                        "due_date": loan["due_date"], "returned_at": loan["returned_at"] if already else stamp(now), "operation": operation}
                db.execute("UPDATE operation_requests SET status='SUCCEEDED',result_lending_id=?,result_json=?,completed_at=? WHERE request_key=?",
                    (lending_id, encode(result), stamp(now), key))
            self.audit(user_id, target, key, result["code"])
            return result
        except RuleError as exc:
            self.audit(user_id, target, key, exc.code)
            raise
        except sqlite3.Error:
            # A commit error can be ambiguous. Inspect the durable receipt using a new connection.
            try:
                status = self.result(user_id, key)
                if status["state"] == "SUCCEEDED":
                    return status["result"]
            except (sqlite3.Error, RuleError):
                self.audit(user_id, target, key, "E-20")
                raise RuleError("E-20", 503)
            self.audit(user_id, target, key, "E-19")
            raise RuleError("E-19", 503)

    @staticmethod
    def audit(user_id, target, key, code):
        LOG.info(encode({"at": stamp(utcnow()), "user_id": user_id, "device_id": target,
                         "request_key": key if isinstance(key, str) and len(key) == 43 else None, "code": code}))


class WebServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, store, user_id=1, session_seconds=1800):
        self.store, self.user_id = store, user_id
        self.sessions, self.session_lock = {}, threading.Lock()
        self.session_seconds = session_seconds
        super().__init__(address, Handler)
        self.cookie_name = f"gate5_session_{self.server_port}"


class Handler(BaseHTTPRequestHandler):
    server_version = "Gate5"

    def log_message(self, fmt, *args):
        # Request URLs can contain operation keys; audit only sanitized business events.
        pass

    def reply(self, payload, status=200, cookie=None):
        raw = encode(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if cookie:
            self.send_header("Set-Cookie", f"{self.server.cookie_name}={cookie}; HttpOnly; SameSite=Strict; Path=/")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def check_origin(self):
        port = self.server.server_port
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host") not in hosts:
            raise RuleError("E-23", 403)
        if self.headers.get("Origin") and self.headers["Origin"] not in {"http://"+h for h in hosts}:
            raise RuleError("E-23", 403)

    def session(self, create=False):
        jar = SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", ""))
            token = jar[self.server.cookie_name].value if self.server.cookie_name in jar else None
        except Exception:
            token = None
        with self.server.session_lock:
            # Discard expired sessions to keep the demo server bounded.
            self.server.sessions = {k: v for k, v in self.server.sessions.items() if v["expires"] > time.monotonic()}
            session = self.server.sessions.get(token)
            if session:
                return session, None
            if not create:
                raise RuleError("E-08", 401)
            token = secrets.token_urlsafe(32)
            session = {"user_id": self.server.user_id, "csrf": secrets.token_urlsafe(32),
                       "expires": time.monotonic()+self.server.session_seconds}
            self.server.sessions[token] = session
            return session, token

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_PUT(self):
        self.reply(RuleError("E-24", 405).response(), 405)

    do_PATCH = do_PUT
    do_DELETE = do_PUT

    def dispatch(self, method):
        user_id, path = None, urlsplit(self.path).path
        try:
            self.check_origin()
            static = {"/": ("index.html", "text/html"), "/static/app.js": ("static/app.js", "text/javascript"),
                      "/static/style.css": ("static/style.css", "text/css")}
            if method == "GET" and path in static:
                filename, mime = static[path]
                raw = (ROOT / filename).read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", mime+"; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
                self.end_headers()
                self.wfile.write(raw)
                return
            if path == "/api/bootstrap" and method == "GET":
                session, cookie = self.session(create=True)
                data = self.server.store.dashboard(session["user_id"])
                self.reply({"ok": True, **data, "csrf": session["csrf"], "messages": MESSAGES}, cookie=cookie)
                return
            session, _ = self.session()
            user_id = session["user_id"]
            if method == "GET":
                if path == "/api/dashboard":
                    self.reply({"ok": True, **self.server.store.dashboard(user_id)})
                elif path.startswith("/api/results/"):
                    self.reply({"ok": True, **self.server.store.result(user_id, path.rsplit("/", 1)[1])})
                else:
                    raise RuleError("E-24", 405)
                return
            if not secrets.compare_digest(self.headers.get("X-CSRF-Token", ""), session["csrf"]):
                raise RuleError("E-23", 403)
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                raise RuleError("E-12")
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 16384:
                    raise ValueError()
                payload = json.loads(self.rfile.read(size).decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError()
            except (ValueError, UnicodeError):
                raise RuleError("E-12")
            store = self.server.store
            if path in ("/api/lend/prepare", "/api/return/prepare"):
                operation = "LEND" if "/lend/" in path else "RETURN"
                data = store.prepare(user_id, operation, payload)
            elif path in ("/api/lend/commit", "/api/return/commit"):
                operation = "LEND" if "/lend/" in path else "RETURN"
                data = {"result": store.commit(user_id, operation, payload)}
            elif path == "/api/cancel":
                allowed(payload, {"request_key"})
                data = store.cancel(user_id, payload.get("request_key"))
            else:
                raise RuleError("E-24", 405)
            self.reply({"ok": True, **data})
        except RuleError as exc:
            Store.audit(user_id, None, None, exc.code)
            self.reply(exc.response(), exc.status)
        except sqlite3.Error:
            code = "E-20" if path.startswith("/api/results/") else "E-19"
            Store.audit(user_id, None, None, code)
            self.reply(RuleError(code, 503).response(), 503)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # committed receipt remains available to the client's result query


def main():
    parser = argparse.ArgumentParser(description="Gate5 PC貸出・返却（研修用）")
    parser.add_argument("--port", type=int, default=8055)
    parser.add_argument("--db", type=Path, default=ROOT / "gate5.db")
    parser.add_argument("--user-id", type=int, default=1, help="研修用の認証済み本人ID。画面から変更不可")
    parser.add_argument("--fault-after-first-update", choices=["LEND", "RETURN"], help="試験専用：最初の更新後に失敗させる")
    args = parser.parse_args()
    LOG.setLevel(logging.INFO)
    handler = RotatingFileHandler(ROOT / "gate5.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    LOG.addHandler(handler)
    def fault(operation):
        if operation == args.fault_after_first_update:
            raise sqlite3.OperationalError("test-only injected failure")
    store = Store(args.db, fault=fault if args.fault_after_first_update else None)
    store.initialize()
    with store.connect() as db:
        store.actor(db, args.user_id)
    server = WebServer(("127.0.0.1", args.port), store, args.user_id)
    print(f"Gate5: http://127.0.0.1:{server.server_port}/  user={args.user_id}  db={args.db}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

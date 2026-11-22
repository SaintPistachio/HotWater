"""Owner-only controls. Caddy terminates HTTPS and proxies to this loopback server."""

import argparse
import getpass
import hashlib
import hmac
import http.cookies
import json
import math
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_STORE = ROOT.parent if ROOT.name == "runtime" and ROOT.parent.name == "admin" else ROOT / "data" / "admin"
STORE = Path(os.environ.get("HOTWATER_ADMIN_STORE", DEFAULT_STORE))
ORIGIN = "https://192.168.50.10:8443"
DUCKDNS_ZONE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.duckdns\.org")
RELAY = "http://127.0.0.1:8124"
PORT = 8126
COOKIE = "__Host-hotwater"
IDLE, LIFETIME, DELIVERY_WINDOW = 1800, 28800, 180
ACTIVE = ("submitting", "queued", "delivered", "acknowledged")
IDENT = re.compile(r"[a-f0-9]{32}\Z")
NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))
FAULT_LABELS = {1: "Dry fire", 2: "Tank temperature above high limit", 3: "Upper sensor failure",
                4: "Lower sensor failure", 6: "Controller error", 21: "Upper element failure",
                22: "Lower element failure", 31: "Water leak", 45: "Low controller battery",
                46: "Shutoff valve error", 101: "Upper thermostat error", 102: "Lower thermostat error"}


def db_connection():
    db = sqlite3.connect(STORE / "commands.sqlite3", timeout=3)
    db.row_factory = sqlite3.Row
    return db


def initialize():
    with closing(db_connection()) as db, db:
        db.execute("""CREATE TABLE IF NOT EXISTS commands (
            id TEXT PRIMARY KEY, created INTEGER NOT NULL, kind TEXT NOT NULL, value TEXT NOT NULL,
            days INTEGER, status TEXT NOT NULL, updated INTEGER NOT NULL, detail TEXT
        )""")
        db.executescript("""CREATE TABLE IF NOT EXISTS schedules (
            id TEXT PRIMARY KEY, due INTEGER NOT NULL, mode TEXT NOT NULL, days INTEGER,
            status TEXT NOT NULL, detail TEXT, authority TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS schedules_status_due ON schedules(status, due);
        CREATE TABLE IF NOT EXISTS presets (
            name TEXT PRIMARY KEY COLLATE NOCASE, target INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS mode_observation (
            id INTEGER PRIMARY KEY CHECK (id = 1), mode TEXT NOT NULL, since INTEGER NOT NULL,
            returned INTEGER
        );""")


def atomic_text(path, text):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def same_secret(supplied, expected):
    return hmac.compare_digest(supplied.encode(), expected.encode())


def password_hash(password, salt):
    return hashlib.scrypt(password.encode(), salt=salt, n=2**17, r=8, p=1, maxmem=256 * 1024**2).hex()


def configure_password():
    if not STORE.is_dir():
        raise SystemExit("Prepare the restricted admin directory using the admin installer first.")
    password = getpass.getpass("New HotWater admin password: ")
    if not 14 <= len(password) <= 128 or password != getpass.getpass("Repeat password: "):
        raise SystemExit("Use 14 to 128 characters and enter the same password twice.")
    salt = secrets.token_bytes(16)
    atomic_text(STORE / "password.json", json.dumps({"salt": salt.hex(), "hash": password_hash(password, salt)}))
    print("Admin password saved. Restart the admin task to invalidate previous sessions.")


def relay_call(path, payload=None):
    request = urllib.request.Request(RELAY + path,
        data=None if payload is None else json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"} if payload is not None else {})
    try:
        with NO_PROXY.open(request, timeout=3) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, {"error": "Relay refused the request"}
    except (OSError, ValueError):
        return 503, {"error": "Relay response unavailable"}


def permissions():
    try:
        data = json.loads((STORE / "verified.json").read_text(encoding="utf-8"))
        return {key: data.get(key) is True for key in ("temperature", "modes")}
    except (OSError, ValueError, AttributeError):
        return {"temperature": False, "modes": False}


def reconcile(db, state, now):
    if not state:
        return
    pending = list((state.get("pending") or {}).values())
    relay_results = {row.get("admin_id"): row for row in (*pending, state.get("last_write") or {})}
    rows = db.execute("""SELECT id, created, status FROM commands
                         WHERE status IN ('submitting','queued','delivered','acknowledged','unresolved')""").fetchall()
    for row in rows:
        result = relay_results.get(row["id"])
        if result:
            status = result.get("outcome") or result.get("status")
            if status in ("unconfirmed", "not_applied") or (status == "expired" and result.get("delivered")):
                status = "unresolved"
            if status in ACTIVE and now - row["created"] > DELIVERY_WINDOW + 10:
                status = "unresolved"
        elif now - row["created"] > DELIVERY_WINDOW + 10:
            status = "unresolved"
        else:
            continue
        if status and status != row["status"]:
            db.execute("UPDATE commands SET status=?, updated=? WHERE id=?", (status, now, row["id"]))


def current_state():
    code, state = relay_call("/state")
    return state if code == 200 and isinstance(state, dict) else {}


def mode_valid(value, days):
    return ((value == "standard" and days is None)
            or (value == "vacation" and type(days) is int and 1 <= days <= 99))


def fault_label(code):
    return f"{code:03d} - {FAULT_LABELS.get(code, 'See the owner’s manual')}"


def schedule_authority(credentials):
    return hashlib.sha256(credentials["salt"].encode("ascii")).hexdigest()


def current_authority():
    try:
        return schedule_authority(json.loads((STORE / "password.json").read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def observe_mode(db, state, now):
    mode = (state.get("admin_control") or {}).get("mode")
    stamp = state.get("last_telemetry")
    if (mode not in ("standard", "vacation") or state.get("online") is not True
            or state.get("contents_stale") or type(stamp) not in (int, float)
            or not 0 <= now - stamp < 600):
        return
    before = db.execute("SELECT mode, since, returned FROM mode_observation WHERE id=1").fetchone()
    if before is None:
        db.execute("INSERT INTO mode_observation VALUES (1, ?, ?, NULL)", (mode, int(stamp)))
    elif mode != before["mode"] and stamp >= before["since"]:
        returned = int(stamp) if mode == "standard" else before["returned"]
        db.execute("UPDATE mode_observation SET mode=?, since=?, returned=? WHERE id=1", (mode, int(stamp), returned))


def status_payload():
    state = current_state()
    control = state.get("admin_control") or {}
    allowed = permissions()
    with closing(db_connection()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        reconcile(db, state, int(time.time()))
        commands = [dict(row) for row in db.execute("SELECT * FROM commands ORDER BY created DESC, rowid DESC LIMIT 20")]
        schedules = [dict(row) for row in db.execute("""SELECT s.id, s.due, s.mode, s.days,
            COALESCE(c.status, s.status) AS status, COALESCE(c.detail, s.detail) AS detail FROM schedules s
            LEFT JOIN commands c ON s.id=c.id ORDER BY (s.status='scheduled') DESC,
            CASE WHEN s.status='scheduled' THEN s.due ELSE -s.due END""")]
        presets = [dict(row) for row in db.execute("SELECT name, target FROM presets ORDER BY name")]
        observation = db.execute("SELECT mode, since, returned FROM mode_observation WHERE id=1").fetchone()
    fault = state.get("active_fault")
    history = state.get("fault_history")
    age = state.get("last_telemetry_age")
    ready = control.get("ready") is True and not state.get("contents_stale")
    busy = bool(state.get("pending")) or any(command["status"] in ACTIVE for command in commands)
    reason = ""
    if type(fault) is int and fault > 0:
        reason = f"Heater fault {fault_label(fault)}."
    elif state.get("online") is False:
        reason = "The heater is offline."
    elif state.get("contents_stale") or (type(age) in (int, float) and age >= 600):
        reason = "The heater readings are stale."
    elif not state:
        reason = "The local heater relay is unavailable."
    elif not ready:
        reason = "Waiting for a verified local connection and fresh heater data."
    elif busy:
        reason = "Waiting for the heater to finish the pending command."
    mode = control.get("mode") or state.get("mode")
    return {"target": state.get("setpoint"), "active_target": state.get("active_setpoint"),
            "max": state.get("max_setpoint"), "mode": mode, "ready": ready,
            "temperature_enabled": allowed["temperature"],
            "modes_enabled": allowed["modes"] and control.get("mode_supported") is True,
            "busy": busy, "commands": commands, "schedules": schedules, "presets": presets,
            "vacation_days": state.get("vacation_days"),
            "vacation_since": observation["since"] if mode == "vacation" and observation and observation["mode"] == mode else None,
            "returned_to_standard": observation["returned"] if observation else None,
            "diagnostics": {"reason": reason, "operating_state": state.get("operating_state"),
                            "element": state.get("heating_element"), "watts": state.get("watts"),
                            "last_telemetry": state.get("last_telemetry"), "online": state.get("online"),
                            "fault": fault_label(fault) if type(fault) is int and fault > 0 else None,
                            "faults_available": isinstance(history, list),
                            "fault_history": [fault_label(code) for code in history
                                              if type(code) is int and code > 0] if isinstance(history, list) else []}}


def submit(body, scheduled=False):
    ident, kind, value, days = (body.get(key) for key in ("id", "kind", "value", "days"))
    if not isinstance(ident, str) or not IDENT.fullmatch(ident):
        return 400, {"error": "Invalid request identifier"}
    allowed = permissions()
    state = current_state()
    control = state.get("admin_control") or {}
    now = int(time.time())
    with closing(db_connection()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        schedule = db.execute("SELECT * FROM schedules WHERE id=?", (ident,)).fetchone()
        if scheduled:
            if schedule is None or schedule["status"] != "scheduled" or not schedule["due"] <= now < schedule["due"] + DELIVERY_WINDOW:
                return 409, {"error": "The schedule is no longer due"}
            if schedule["authority"] != current_authority():
                return 403, {"error": "Schedule authorization changed. Save a new schedule after signing in."}
            kind, value, days = "mode", schedule["mode"], schedule["days"]
        elif schedule is not None:
            return 409, {"error": "This identifier belongs to a scheduled change"}
        reconcile(db, state, now)
        previous = db.execute("SELECT * FROM commands WHERE id=?", (ident,)).fetchone()
        if previous:
            return 200, dict(previous)
        if (control.get("ready") is not True or state.get("contents_stale") or state.get("pending")
                or db.execute("SELECT 1 FROM commands WHERE status IN ('submitting','queued','delivered','acknowledged')").fetchone()):
            return 409, {"error": "Heater unavailable or another change is pending"}
        if kind == "setpoint" and allowed["temperature"]:
            ceiling = state.get("max_setpoint")
            if (type(ceiling) not in (int, float) or not math.isfinite(ceiling) or type(value) is not int
                    or not 95 <= value <= min(150, ceiling) or days is not None):
                return 400, {"error": "Use a whole number in the heater's reported range"}
            if value == state.get("setpoint"):
                return 409, {"error": "The heater already reports that target"}
        elif kind == "mode" and allowed["modes"] and control.get("mode_supported"):
            if not mode_valid(value, days):
                return 400, {"error": "Select Standard or a Vacation of 1 to 99 days"}
            if value == control.get("mode"):
                if scheduled:
                    if value == "vacation" and state.get("vacation_days") != days:
                        db.execute("UPDATE schedules SET status='blocked', detail=? WHERE id=?",
                                   ("Vacation was already active at a different or unknown duration.", ident))
                    else:
                        db.execute("UPDATE schedules SET status='already_set' WHERE id=?", (ident,))
                return 409, {"error": "The heater already reports that mode"}
        else:
            return 403, {"error": "This control has not been enabled"}
        db.execute("INSERT INTO commands VALUES (?, ?, ?, ?, ?, 'submitting', ?, NULL)",
                   (ident, now, kind, str(value), days, now))
        if scheduled:
            db.execute("UPDATE schedules SET status='submitted' WHERE id=?", (ident,))
    code, response = relay_call("/admin/write", {"id": ident, "kind": kind, "value": value, "days": days})
    with closing(db_connection()) as db, db:
        if code == 200:
            db.execute("UPDATE commands SET status='queued' WHERE id=? AND status='submitting'", (ident,))
        elif 400 <= code < 500:
            db.execute("UPDATE commands SET status='rejected', detail=? WHERE id=? AND status='submitting'",
                       (response["error"], ident))
    if code == 200:
        return 202, {"message": "Change accepted by relay. Waiting for heater confirmation."}
    if code >= 500:
        return 503, {"error": "Submission outcome unavailable. Check command status before trying again."}
    return code, response


def add_schedule(body, authority):
    ident, due, mode, days = (body.get(key) for key in ("id", "due", "mode", "days"))
    if (not isinstance(ident, str) or not IDENT.fullmatch(ident)
            or type(due) is not int or not 0 < due <= 253402300799 or not mode_valid(mode, days)):
        return 400, {"error": "Choose a date and a valid Standard or Vacation setting"}
    if authority != current_authority():
        return 403, {"error": "Schedule authorization changed. Sign in after the admin service restarts."}
    if not permissions()["modes"]:
        return 403, {"error": "Mode controls are disabled"}
    with closing(db_connection()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute("SELECT due, mode, days FROM schedules WHERE id=?", (ident,)).fetchone()
        if existing is not None:
            if tuple(existing) == (due, mode, days):
                return 200, {"message": "Scheduled change saved."}
            return 409, {"error": "This identifier belongs to a different schedule"}
        if due <= time.time():
            return 400, {"error": "Choose a time in the future"}
        if (db.execute("SELECT 1 FROM schedules WHERE due=? AND status='scheduled'", (due,)).fetchone()
                or db.execute("SELECT 1 FROM commands WHERE id=?", (ident,)).fetchone()):
            return 409, {"error": "Another change already uses this time or identifier"}
        db.execute("INSERT INTO schedules VALUES (?, ?, ?, ?, 'scheduled', NULL, ?)", (ident, due, mode, days, authority))
    return 201, {"message": "Scheduled change saved."}


def cancel_schedule(body):
    ident = body.get("id")
    if not isinstance(ident, str) or not IDENT.fullmatch(ident):
        return 400, {"error": "Invalid schedule identifier"}
    with closing(db_connection()) as db, db:
        result = db.execute("UPDATE schedules SET status='cancelled' WHERE id=? AND status='scheduled'", (ident,))
        if not result.rowcount:
            return 409, {"error": "This schedule is no longer waiting to start"}
    return 200, {"message": "Scheduled change cancelled."}


def update_preset(body, delete=False):
    name = body.get("name")
    name = name.strip() if isinstance(name, str) else ""
    if not 1 <= len(name) <= 48 or not name.isprintable():
        return 400, {"error": "Enter a preset name of 1 to 48 characters"}
    target = body.get("target")
    if not delete and (type(target) is not int or not 95 <= target <= 150):
        return 400, {"error": "Enter a whole number between 95 and 150 °F"}
    with closing(db_connection()) as db, db:
        if delete:
            db.execute("DELETE FROM presets WHERE name=?", (name,))
        else:
            db.execute("""INSERT INTO presets VALUES (?, ?) ON CONFLICT(name)
                DO UPDATE SET name=excluded.name, target=excluded.target""", (name, target))
    return 200, {"message": "Preset removed." if delete else "Temperature preset saved."}


def process_schedules(now):
    state = current_state()
    authority = current_authority()
    with closing(db_connection()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        observe_mode(db, state, now)
        reconcile(db, state, int(now))
        if authority is not None:
            db.execute("""UPDATE schedules SET status='blocked', detail='Admin password changed. Save a new schedule.'
                          WHERE status='scheduled' AND authority<>?""", (authority,))
        db.execute("""UPDATE schedules SET status='missed',
                      detail=COALESCE(detail, 'The start window passed without a submission.')
                      WHERE status='scheduled' AND due<=?""", (now - DELIVERY_WINDOW,))
        due = db.execute("SELECT id FROM schedules WHERE status='scheduled' AND due<=? ORDER BY due", (now,)).fetchall()
    for row in due:
        code, result = submit({"id": row["id"]}, scheduled=True)
        if code in (400, 403):
            with closing(db_connection()) as db, db:
                db.execute("UPDATE schedules SET status='blocked', detail=? WHERE id=? AND status='scheduled'",
                           (result["error"], row["id"]))
        elif code == 409 and result.get("error") == "Heater unavailable or another change is pending":
            with closing(db_connection()) as db, db:
                db.execute("UPDATE schedules SET detail=? WHERE id=? AND status='scheduled'",
                           (result["error"], row["id"]))


def scheduler(stop):
    last_error = None
    while not stop.is_set():
        try:
            process_schedules(time.time())
            last_error = None
        except sqlite3.Error as error:
            message = str(error)
            if message != last_error:
                print(f"HotWater scheduling unavailable: {message}", file=sys.stderr, flush=True)
            last_error = message
        stop.wait(5)


class Sessions:
    def __init__(self, credentials, proxy_key):
        self.credentials = credentials
        self.proxy_key = proxy_key
        self.lock = threading.Lock()
        self.verify_slot = threading.BoundedSemaphore(1)
        self.tokens = {}
        self.attempts = []

    def login(self, password):
        now = time.monotonic()
        with self.lock:
            self.attempts = [at for at in self.attempts if now - at < 60]
            if len(self.attempts) >= 8:
                return 429, None
            self.attempts.append(now)
        if not isinstance(password, str) or not 14 <= len(password) <= 128:
            return 401, None
        if not self.verify_slot.acquire(blocking=False):
            return 429, None
        try:
            expected = self.credentials["hash"]
            actual = password_hash(password, bytes.fromhex(self.credentials["salt"]))
        finally:
            self.verify_slot.release()
        if not hmac.compare_digest(expected, actual):
            return 401, None
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.lock:
            self.prune(now)
            self.tokens[token] = (now, now, csrf)
        return 200, token

    def prune(self, now):
        self.tokens = {token: value for token, value in self.tokens.items()
                       if now - value[0] < LIFETIME and now - value[1] < IDLE}

    def get(self, token, touch=False):
        now = time.monotonic()
        with self.lock:
            self.prune(now)
            value = self.tokens.get(token)
            if value and touch:
                self.tokens[token] = (value[0], now, value[2])
            return value[2] if value else None

    def logout(self, token):
        with self.lock:
            self.tokens.pop(token, None)


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(8)

    def log_message(self, fmt, *args):
        pass

    def proxy_origin(self):
        if not same_secret(self.headers.get("X-HotWater-Proxy", ""), self.server.sessions.proxy_key):
            return None
        host = self.headers.get("Host")
        if host == urllib.parse.urlsplit(ORIGIN).netloc:
            return ORIGIN
        public_origin = getattr(self.server, "public_origin", None)
        if public_origin and host == urllib.parse.urlsplit(public_origin).netloc:
            return public_origin
        return None

    def send(self, code, body=b"", content_type="application/json; charset=utf-8", **headers):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; connect-src 'self'; style-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in headers.items():
            self.send_header(key.replace("_", "-"), value)
        self.end_headers()
        self.wfile.write(data)

    def token(self):
        try:
            item = http.cookies.SimpleCookie(self.headers.get("Cookie", "")).get(COOKIE)
        except http.cookies.CookieError:
            return None
        return item.value if item else None

    def do_GET(self):
        origin = self.proxy_origin()
        if not origin:
            self.send(403)
            return
        path = urllib.parse.urlsplit(self.path).path
        if path in self.server.assets:
            data, mime = self.server.assets[path]
            self.send(200, data, mime)
        elif path == "/login":
            self.send(200, self.server.login_page, "text/html; charset=utf-8")
        elif path in ("/", "/api/status"):
            csrf = self.server.sessions.get(self.token(), touch=path == "/")
            if not csrf:
                self.send(303, Location="/login") if path == "/" else self.send(401)
            elif path == "/":
                page = self.server.admin_page if origin == ORIGIN else self.server.public_admin_page
                self.send(200, page, "text/html; charset=utf-8")
            else:
                try:
                    self.send(200, {**status_payload(), "csrf": csrf})
                except sqlite3.Error:
                    self.send(503, {"error": "Command history unavailable"})
        else:
            self.send(404)

    def do_POST(self):
        origin = self.proxy_origin()
        if not origin or self.headers.get("Origin") != origin:
            self.send(403)
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 1 <= size <= 2048 or self.headers.get_content_type() != "application/json":
                raise ValueError
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                raise ValueError
        except (OSError, ValueError):
            self.send(400, {"error": "Invalid request data"})
            return
        if self.path == "/api/login":
            code, token = self.server.sessions.login(body.get("password"))
            if token:
                self.server.sessions.logout(self.token())
                self.send(200, {"ok": True}, Set_Cookie=f"{COOKIE}={token}; Path=/; Secure; HttpOnly; SameSite=Strict")
            else:
                self.send(code, {"error": "Sign-in failed. Check the password or wait a minute and retry."})
            return
        token = self.token()
        csrf = self.server.sessions.get(token)
        if not csrf or not same_secret(self.headers.get("X-CSRF-Token", ""), csrf):
            self.send(403, {"error": "Session expired or request rejected"})
            return
        self.server.sessions.get(token, touch=True)
        try:
            if self.path == "/api/logout":
                self.server.sessions.logout(token)
                self.send(200, {"ok": True}, Set_Cookie=f"{COOKIE}=; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=Strict")
            elif self.path == "/api/commands":
                self.send(*submit(body))
            elif self.path == "/api/schedules":
                self.send(*add_schedule(body, schedule_authority(self.server.sessions.credentials)))
            elif self.path == "/api/schedules/cancel":
                self.send(*cancel_schedule(body))
            elif self.path in ("/api/presets", "/api/presets/delete"):
                self.send(*update_preset(body, delete=self.path.endswith("/delete")))
            elif self.path == "/api/cancel":
                ident = body.get("id")
                if not isinstance(ident, str) or not IDENT.fullmatch(ident):
                    self.send(400, {"error": "Invalid command identifier"})
                    return
                code, result = relay_call("/admin/cancel", {"id": ident})
                if code == 200:
                    with closing(db_connection()) as db, db:
                        db.execute("""UPDATE commands SET status='cancelled', updated=?
                                      WHERE id=? AND status IN ('submitting','queued')""", (int(time.time()), ident))
                self.send(code, {"ok": True} if code == 200 else result)
            else:
                self.send(404)
        except sqlite3.Error:
            self.send(503, {"error": "Command history unavailable. Check status before retrying."})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--set-password", action="store_true")
    args = parser.parse_args()
    if args.set_password:
        configure_password()
        return
    credentials = json.loads((STORE / "password.json").read_text(encoding="utf-8"))
    proxy_key = (STORE / "proxy.key").read_text(encoding="ascii").strip()
    if not re.fullmatch(r"[0-9a-f]{64}", proxy_key):
        raise SystemExit("Proxy credentials are missing or invalid.")
    initialize()
    with ThreadingHTTPServer(("127.0.0.1", PORT), Handler) as server:
        server.sessions = Sessions(credentials, proxy_key)
        server.login_page = (ROOT / "admin-login.html").read_bytes()
        server.admin_page = (ROOT / "admin.html").read_bytes()
        zone = os.environ.get("HOTWATER_DUCKDNS_ZONE", "")
        if DUCKDNS_ZONE.fullmatch(zone):
            server.public_origin = f"https://admin.{zone}:8443"
            server.public_admin_page = server.admin_page.replace(
                b"http://192.168.50.10:8125/", f"https://stats.{zone}:8443/".encode("ascii")
            )
        server.assets = {f"/{name}": ((ROOT / name).read_bytes(), mime) for name, mime in
                         (("admin.css", "text/css"), ("admin.js", "text/javascript"))}
        print(f"HotWater admin listening on 127.0.0.1:{PORT} for the HTTPS proxy", flush=True)
        stop = threading.Event()
        worker = threading.Thread(target=scheduler, args=(stop,), daemon=True)
        worker.start()
        try:
            server.serve_forever()
        finally:
            stop.set()
            worker.join()


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, sqlite3.Error) as error:
        print(f"Admin startup failed: {error}", file=sys.stderr, flush=True)
        raise SystemExit(1) from None

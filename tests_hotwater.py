"""Regression checks for sparse analytics and owner-only heater commands."""

import asyncio
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from datetime import date, timedelta
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import admin
import hotwater_analytics as analytics
from icomm_relay import Relay


class AnalyticsTest(unittest.TestCase):
    def test_vacation_charge_does_not_change_saved_lows(self):
        start = analytics.local_stamp(date(2026, 11, 22), 18)
        with tempfile.TemporaryDirectory() as folder, patch.object(analytics, "HISTORY_DB", Path(folder) / "db"):
            analytics.initialize_history()
            with closing(analytics.open_history()) as db, db:
                # Older versions clamped the target denominator and recorded 0% or 100%.
                db.executemany("INSERT INTO samples VALUES (?, ?, ?, ?, ?, ?)", [
                    (start, 76, 89, 150, 1104, 1000),
                    (start + 60, 76, 0, 50, 1104, 1050),
                    (start + 75, 76, 100, 50, 0, 1060),
                ])
            analytics.record({"stamp": start + 90, "age_s": 0, "online": True, "contents_stale": False,
                              "setpoint": 50, "deficit_wh": 0, "available_gallons": 76, "energy_wh": 1070})
            with closing(analytics.open_history()) as db:
                saved = db.execute("SELECT * FROM samples WHERE stamp = ?", (start + 90,)).fetchone()
            self.assertEqual((start + 90, None, None, 50, None, 1070), saved)
            result = analytics.history(start + 100)
            self.assertEqual([(start, 89)], result["points"])
            self.assertEqual(89, result["days"][0]["low_pct"])
            self.assertEqual(0.07, result["days"][0]["observed_kwh"])

    def test_history_ignores_one_impossible_dip_and_keeps_a_sustained_low(self):
        start = analytics.local_stamp(date(2026, 11, 22), 18)
        with tempfile.TemporaryDirectory() as folder, patch.object(analytics, "HISTORY_DB", Path(folder) / "db"):
            analytics.initialize_history()
            with closing(analytics.open_history()) as db, db:
                db.executemany("INSERT INTO samples VALUES (?, ?, ?, ?, ?, ?)", [
                    (start, 76, 89, 150, 1104, 1000),
                    (start + 60, 0, 0, 150, 10500, 1000),
                    (start + 75, 76, 89, 150, 1104, 1000),
                    (start + 375, 5, 6, 150, 9800, 1000),
                    (start + 450, 5, 6, 150, 9800, 1010),
                ])
            pending = analytics.history(start + 60)
            self.assertEqual([(start, 89)], pending["points"])
            result = analytics.history(start + 450)
            self.assertEqual([(start, 89), (start + 375, 6)], result["points"])
            self.assertEqual(6, result["days"][0]["low_pct"])
            insight = analytics.insights(start + 450)
            self.assertEqual(5, insight["replay"][0]["low"])
            self.assertNotIn(start + 60, [row[0] for row in analytics.history_rows(start, start + 450)])

    def test_history_keeps_a_dip_with_plausible_recovery(self):
        start = analytics.local_stamp(date(2026, 11, 22), 18)
        with tempfile.TemporaryDirectory() as folder, patch.object(analytics, "HISTORY_DB", Path(folder) / "db"):
            analytics.initialize_history()
            with closing(analytics.open_history()) as db, db:
                db.executemany("INSERT INTO samples VALUES (?, ?, ?, ?, ?, ?)", [
                    (start, 76, 89, 150, 1104, 1000),
                    (start + 300, 65, 78, 150, 2300, 1000),
                    (start + 780, 76, 89, 150, 1104, 1733),
                ])
            result = analytics.history(start + 780)
            self.assertEqual(78, result["days"][0]["low_pct"])

    def test_draw_replay_and_gaps(self):
        rows = [(0, 150, 300, 0, 70), (90, 150, 300, 0, 70),
                (270, 150, 1350, 0, 61), (450, 150, 2300, 0, 54),
                (540, 150, 2200, 138, 55), (900, 150, 300, 688, 70)]
        event, = analytics.observed_draws(rows)
        self.assertTrue(event["complete"])
        self.assertEqual((90, 450, 54, 900), tuple(event[key] for key in ("start", "end", "low", "recovered")))
        gap = analytics.observed_draws(rows[:4] + [(1300, 150, 300, 0, 70)])
        self.assertFalse(gap[0]["complete"])
        reset = analytics.observed_draws(rows[:4] + [(540, 150, 200, -1, 70)])
        self.assertFalse(reset[0]["complete"])

    def test_daily_coverage_and_midnight_boundary(self):
        rows = [(stamp, 79) for stamp in range(0, 3600, 300)]
        self.assertTrue(analytics.has_daily_coverage(rows, 0, 3600))
        self.assertFalse(analytics.has_daily_coverage(rows[3:], 0, 3600))
        self.assertFalse(analytics.has_daily_coverage(rows[:3] + rows[5:], 0, 3600))
        with tempfile.TemporaryDirectory() as folder, patch.object(analytics, "HISTORY_DB", Path(folder) / "db"):
            analytics.initialize_history()
            today = date(2026, 11, 22)
            midnight = analytics.local_stamp(today)
            morning = analytics.local_stamp(today, 7)
            now = analytics.local_stamp(today, 8)
            self.assertEqual(16, analytics.insights(now)["reference"])
            with closing(analytics.open_history()) as db, db:
                db.executemany("INSERT INTO samples VALUES (?, ?, ?, ?, ?, ?)", [
                    (midnight - 120, 70, 85, 150, 1500, 1000),
                    (midnight - 60, 68, 83, 150, 1785, 1020),
                    (midnight + 60, 67, 82, 150, 1890, 1025),
                    (midnight + 120, 66, 81, 150, 1995, 1040),
                    (morning, 45, 55, 150, 4725, 1200),
                    (morning + 60, 45, 55, 150, 4725, 1225),
                ])
            days = analytics.history(now)["days"]
            self.assertEqual([(55, 0.2), (83, 0.02)], [(d["low_pct"], d["observed_kwh"]) for d in days[:2]])
            self.assertEqual(45, analytics.insights(now)["replay"][0]["low"])

            draws = [{"start": morning + n * 500, "end": morning + n * 500 + 300, "complete": True,
                      "volume": 10 + n, "before": 70, "low": 50} for n in range(6)]
            with patch.object(analytics, "observed_draws", return_value=draws):
                result = analytics.insights(now)
            self.assertTrue(result["learned_reference"])
            self.assertEqual(11, result["reference"])
            self.assertEqual(draws, result["replay"][0]["draws"])


class Request:
    def __init__(self, data, remote="127.0.0.1"):
        self.data = data
        self.remote = remote

    async def json(self):
        return self.data


class RelayTest(unittest.TestCase):
    def test_safe_command_lifecycle(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"DATA_DIR": folder,
                "QUEUE_FILE": str(Path(folder) / "queue.json")}):
            relay = Relay()
            relay._admin_ready = lambda: True
            relay.lan.session_verified = True
            relay.state.update(setpoint=150.0, max_setpoint=150.0, mode="unknown")
            payload = {"id": "1" * 32, "kind": "setpoint", "value": 145}
            call = lambda body, remote="127.0.0.1": asyncio.run(relay.admin_write_handler(Request(body, remote)))
            self.assertEqual(403, call(payload, "192.168.0.8").status)
            self.assertEqual(400, call({**payload, "value":151}).status)
            self.assertEqual(400, call({**payload, "kind":"mode", "value":"vacation", "days":1}).status)
            response = call(payload)
            self.assertEqual(200, response.status)
            self.assertEqual(json.loads(response.body), json.loads(call(payload).body))
            self.assertIsNone(relay._next_write(cloud=True))
            write = relay.take_pending_for_lan()
            self.assertEqual(write.admin_id, payload["id"])
            rejected = asyncio.run(relay.admin_cancel_handler(Request({"id": payload["id"]})))
            self.assertEqual(409, rejected.status)
            relay.state["setpoint"] = 145
            relay._confirm_pending()
            self.assertEqual("confirmed", relay.last_write["outcome"])
            next_ident = "2" * 32
            self.assertEqual(200, call({**payload, "id": next_ident, "value":150}).status)
            copy = Relay()
            copy._load_queue()
            self.assertEqual(next_ident, copy.pending["setpoint"].admin_id)
            cancelled = asyncio.run(relay.admin_cancel_handler(Request({"id": next_ident})))
            self.assertEqual("cancelled", json.loads(cancelled.body)["command"]["outcome"])
            self.assertEqual(200, call({**payload, "id":"3" * 32}).status)
            relay.pending["setpoint"].queued -= 181
            self.assertIsNone(relay.take_pending_for_lan())
            self.assertEqual("expired", relay.last_write["outcome"])


class AdminTest(unittest.TestCase):
    def test_scheduled_modes_fire_once_with_expiry_and_owner_rotation(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(admin, "STORE", Path(folder)), \
             patch.object(admin.time, "time", return_value=1000) as clock:
            (admin.STORE / "password.json").write_text('{"salt":"aa"}', encoding="utf-8")
            (admin.STORE / "verified.json").write_text('{"modes":true}', encoding="utf-8")
            admin.initialize()
            authority = admin.current_authority()
            state = {"online":True, "last_telemetry":1000, "fault_history":[31, 0, 46],
                     "admin_control":{"ready":True, "mode_supported":True, "mode":"standard"}}
            writes = []

            def relay(path, payload=None):
                if path == "/state":
                    return 200, state
                self.assertEqual("/admin/write", path)
                writes.append(payload)
                return (503, {}) if payload["value"] == "standard" else (200, {})

            with patch.object(admin, "relay_call", side_effect=relay):
                vacation = {"id":"a" * 32, "due":1010, "mode":"vacation", "days":2}
                early_return = {"id":"b" * 32, "due":1020, "mode":"standard"}
                missed = {"id":"c" * 32, "due":1005, "mode":"vacation", "days":1}
                later = {"id":"d" * 32, "due":1300, "mode":"vacation", "days":3}
                cancelled = {"id":"e" * 32, "due":1220, "mode":"vacation", "days":3}
                for schedule in (vacation, early_return, missed, later, cancelled):
                    self.assertEqual(201, admin.add_schedule(schedule, authority)[0])
                self.assertEqual(200, admin.add_schedule(vacation, authority)[0])
                self.assertEqual(200, admin.cancel_schedule(cancelled)[0])
                admin.process_schedules(1000)
                self.assertFalse(writes)
                clock.return_value = 1185  # First schedule missed its window; the next is still due.
                admin.process_schedules(1185)
                self.assertEqual([vacation["id"]], [write["id"] for write in writes])
                self.assertEqual(409, admin.cancel_schedule(vacation)[0])
                state.update(last_telemetry=1185, vacation_days=2,
                             last_write={"admin_id":vacation["id"], "outcome":"confirmed"})
                state["admin_control"]["mode"] = "vacation"
                admin.process_schedules(1185)
                admin.process_schedules(1185)  # The Standard submission failed ambiguously. Do not send it again.
                self.assertEqual([vacation["id"], early_return["id"]], [write["id"] for write in writes])
                self.assertEqual(1185, admin.status_payload()["vacation_since"])
                state.update(last_telemetry=1186, vacation_days=None,
                             last_write={"admin_id":early_return["id"], "outcome":"confirmed"})
                state["admin_control"]["mode"] = "standard"
                clock.return_value = 1186
                admin.process_schedules(1186)
                (admin.STORE / "password.json").write_text('{"salt":"bb"}', encoding="utf-8")
                clock.return_value = 1300
                admin.process_schedules(1300)
                status = admin.status_payload()
                self.assertEqual(1186, status["returned_to_standard"])
                self.assertEqual(["031 - Water leak", "046 - Shutoff valve error"], status["diagnostics"]["fault_history"])
                self.assertEqual({"a":"confirmed", "b":"confirmed", "c":"missed", "d":"blocked", "e":"cancelled"},
                                 {row["id"][0]:row["status"] for row in status["schedules"]})

    def test_http_protection_audit_and_uncertain_outcome(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(admin, "STORE", Path(folder)), \
             patch.object(admin, "password_hash", return_value="known-hash"):
            (admin.STORE / "verified.json").write_text('{"temperature":true,"modes":true}', encoding="utf-8")
            (admin.STORE / "password.json").write_text('{"salt":"aa"}', encoding="utf-8")
            admin.initialize()
            state = {"setpoint":150,"max_setpoint":150,"admin_control":{"ready":True},"pending":{}}
            with admin.ThreadingHTTPServer(("127.0.0.1", 0), admin.Handler) as server, \
                 patch.object(admin, "relay_call", side_effect=lambda p, body=None:
                              (200, state) if p == "/state" else (503, {"error":"unavailable"})) as relay:
                server.sessions = admin.Sessions({"salt":"aa", "hash":"known-hash"}, "proxy-secret")
                server.admin_page = server.login_page = b"test page"
                admin_host = "admin.hotwater-test.duckdns.org:8443"
                server.public_origin = f"https://{admin_host}"
                server.public_admin_page = b"public admin page"
                server.assets = {}
                thread = threading.Thread(target=server.serve_forever)
                thread.start()
                self.addCleanup(thread.join)
                try:
                    def http(method, url, data=None, cookie=None, csrf=None, origin=admin.ORIGIN, trusted=True,
                             host="192.168.50.10:8443"):
                        headers = {"Host":host, "Origin":origin,"Content-Type":"application/json"}
                        if trusted:
                            headers["X-HotWater-Proxy"] = "proxy-secret"
                        if cookie:
                            headers["Cookie"] = cookie
                        if csrf:
                            headers["X-CSRF-Token"] = csrf
                        with closing(HTTPConnection("127.0.0.1", server.server_port, timeout=3)) as conn:
                            conn.request(method, url, body=json.dumps(data) if data is not None else None, headers=headers)
                            response = conn.getresponse()
                            return response.status, response.getheader("Set-Cookie"), response.read()

                    self.assertEqual(403, http("GET", "/login", trusted=False)[0])
                    self.assertEqual(403, http("GET", "/login", host=admin_host, trusted=False)[0])
                    self.assertEqual(403, http("GET", "/login", host="stats.hotwater-test.duckdns.org:8443")[0])
                    self.assertEqual(200, http("GET", "/login", host=admin_host)[0])
                    login = {"password":"test-password-long"}
                    self.assertEqual(403, http("POST", "/api/login", login, host=admin_host)[0])
                    self.assertEqual(403, http("POST", "/api/login", login, host=admin_host,
                                               origin="https://stats.hotwater-test.duckdns.org:8443")[0])
                    code, public_cookie, _ = http("POST", "/api/login", login, host=admin_host,
                                                  origin=server.public_origin)
                    self.assertEqual(200, code)
                    self.assertEqual(b"public admin page", http("GET", "/", host=admin_host,
                                                               cookie=public_cookie.split(";", 1)[0])[2])
                    self.assertEqual(303, http("GET", "/")[0])
                    self.assertEqual(401, http("GET", "/api/status")[0])
                    self.assertEqual(403, http("POST", "/api/login", {"password":"test-password-long"}, origin="https://example.com")[0])
                    self.assertEqual(401, http("POST", "/api/login", {"password":"too-short"})[0])
                    self.assertEqual(403, http("POST", "/api/commands", {})[0])
                    code, raw_cookie, _ = http("POST", "/api/login", {"password":"test-password-long"})
                    self.assertEqual(200, code)
                    self.assertIn("Secure; HttpOnly; SameSite=Strict", raw_cookie)
                    cookie = raw_cookie.split(";", 1)[0]
                    token = cookie.split("=", 1)[1]
                    csrf = json.loads(http("GET", "/api/status", cookie=cookie)[2])["csrf"]
                    for endpoint in ("/api/schedules", "/api/schedules/cancel", "/api/presets", "/api/presets/delete"):
                        self.assertEqual(403, http("POST", endpoint, {}, cookie)[0])
                    scheduled = {"id":"2" * 32, "mode":"standard", "due":int(time.time()) + 3600}
                    self.assertEqual(201, http("POST", "/api/schedules", scheduled, cookie, csrf)[0])
                    self.assertEqual(200, http("POST", "/api/schedules/cancel", scheduled, cookie, csrf)[0])
                    preset = {"name":"Normal", "target":145}
                    self.assertEqual(200, http("POST", "/api/presets", preset, cookie, csrf)[0])
                    saved = json.loads(http("GET", "/api/status", cookie=cookie)[2])["presets"]
                    self.assertEqual([preset], saved)
                    self.assertEqual(200, http("POST", "/api/presets/delete", preset, cookie, csrf)[0])
                    command = {"id":"1" * 32, "kind":"setpoint", "value":145}
                    self.assertEqual(403, http("POST", "/api/commands", command, cookie)[0])
                    self.assertEqual(403, http("POST", "/api/commands", command, cookie, csrf, origin="https://example.com")[0])
                    self.assertEqual(503, http("POST", "/api/commands", command, cookie, csrf)[0])
                    self.assertEqual(200, http("POST", "/api/commands", command, cookie, csrf)[0])
                    self.assertEqual(1, sum(call.args[0] == "/admin/write" for call in relay.call_args_list))
                    state["last_write"] = {"admin_id":command["id"], "outcome":"confirmed"}
                    audit = json.loads(http("GET", "/api/status", cookie=cookie)[2])["commands"]
                    self.assertEqual("confirmed", audit[0]["status"])
                    self.assertEqual(200, http("POST", "/api/logout", {}, cookie, csrf)[0])
                    self.assertEqual(401, http("GET", "/api/status", cookie=cookie)[0])
                    _, raw_cookie, _ = http("POST", "/api/login", {"password":"test-password-long"})
                    cookie = raw_cookie.split(";", 1)[0]
                    token = cookie.split("=", 1)[1]
                    now = time.monotonic()
                    server.sessions.tokens[token] = (now - 2000, now - 1801, "csrf")
                    self.assertEqual(401, http("GET", "/api/status", cookie=cookie)[0])
                finally:
                    server.shutdown()
                    thread.join()


if __name__ == "__main__":
    unittest.main()

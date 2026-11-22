"""Phone-friendly hot water gauge that reads the local iCOMM relay state.

Standard library only. Settings come from environment variables:
  RELAY_STATE   relay state URL      (default http://127.0.0.1:8124/state)
  GAUGE_PORT    port to serve on     (default 8125)
  GAUGE_HOST    bind address         (default 0.0.0.0)
  TANK_GALLONS  rated storage volume (default 45)
  INLET_F       cold inlet temp, F   (default 55)
  RATED_WATTS   element power, W     (default 5500)
  HOTWATER_HISTORY_DB                (default data/history.sqlite3)
"""

import json
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.request
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# The embedded Windows Python launcher uses a fixed module search path.
PROJECT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT))
import hotwater_analytics as analytics

RELAY_STATE = os.environ.get("RELAY_STATE", "http://127.0.0.1:8124/state")
GAUGE_PORT = int(os.environ.get("GAUGE_PORT", "8125"))
GAUGE_HOST = os.environ.get("GAUGE_HOST", "0.0.0.0")
POLL_S = 15


def summarize(state):
    number = analytics.number
    setpoint = number(state.get("active_setpoint")) or number(state.get("setpoint"))
    deficit = number(state.get("heat_deficit_wh"))
    top = number(state.get("water_top"))
    bottom = number(state.get("water_mid"))  # water_mid is this model's bottom thermistor.
    percent = analytics.tank_charge(setpoint, deficit)
    sample = {
        "percent": percent,
        "app_percent": state.get("hot_water_availability"),
        "level": state.get("hot_water_level"),
        "setpoint": setpoint,
        "top_f": top,
        "bottom_f": bottom,
        "deficit_wh": deficit,
        "heating": state.get("operating_state"),
        "element": state.get("heating_element"),
        "watts": state.get("watts"),
        "mode": state.get("mode"),
        "fault": state.get("active_fault"),
        "online": state.get("online"),
        "age_s": number(state.get("last_telemetry_age")),
        "fetched": int(time.time()),
        "stamp": number(state.get("last_telemetry")),
        "energy_wh": number(state.get("energy_wh")),
        "contents_stale": bool(state.get("contents_stale")),
        "comfort_f": analytics.COMFORT_F,
    }
    sample["reading_fresh"] = analytics.usable_reading(sample)
    sample["available_gallons"] = None
    if sample["reading_fresh"]:
        sample["available_gallons"] = analytics.available_gallons(top, bottom, setpoint, deficit)
    return sample


def fetch_state():
    with urllib.request.urlopen(RELAY_STATE, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


@lru_cache(maxsize=1)
def insights_for_minute(minute):
    return analytics.insights(minute * 60)


def collect_history(stop):
    initialized = False
    previous_error = None
    while not stop.is_set():
        try:
            if not initialized:
                analytics.initialize_history()
                initialized = True
            analytics.record(summarize(fetch_state()))
            if previous_error is not None:
                print("Hot water history collection resumed.", flush=True)
            previous_error = None
        except (OSError, sqlite3.Error, ValueError, AttributeError) as exc:
            error = f"{type(exc).__name__}: {exc}"
            if error != previous_error:
                print(f"Hot water history collection could not record a sample: {error}", flush=True)
            previous_error = error
        stop.wait(POLL_S)


class Handler(BaseHTTPRequestHandler):
    def _send(self, status, body, ctype):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api":
            try:
                body = summarize(fetch_state())
            except Exception as exc:  # report any relay problem to the page
                body = {"error": "relay unavailable (%s)" % type(exc).__name__}
            else:
                body["recent_flow"] = None
                rates = []
                try:
                    body["recent_flow"] = analytics.recent_flow(body)
                    rates = analytics.recent_recovery_rates(body["stamp"])
                except sqlite3.Error:
                    pass  # Keep live telemetry available if the history DB is down.
                body["recovery"] = analytics.recovery(body, rates)
            self._send(200, json.dumps(body), "application/json")
        elif path == "/api/history":
            try:
                body = analytics.history()
            except sqlite3.Error:
                self._send(503, '{"error": "history unavailable"}', "application/json")
            else:
                self._send(200, json.dumps(body), "application/json")
        elif path == "/api/insights":
            try:
                body = insights_for_minute(int(time.time()) // 60)
            except sqlite3.Error:
                self._send(503, '{"error": "history unavailable"}', "application/json")
            else:
                self._send(200, json.dumps(body), "application/json")
        elif path == "/hub":
            self._send(200, self.server.hub_page, "text/html; charset=utf-8")
        elif path == "/":
            page = self.server.page
            if self.server.public_host and self.headers.get("Host") == self.server.public_host:
                page = self.server.public_page
            self._send(200, page, "text/html; charset=utf-8")
        else:
            self._send(404, "not found", "text/plain; charset=utf-8")

    def log_message(self, fmt, *args):
        pass


def main():
    page = (PROJECT / "gauge.html").read_text(encoding="utf-8")
    admin_footer = '<footer><a href="https://192.168.50.10:8443/">Admin controls</a></footer>'
    with ThreadingHTTPServer((GAUGE_HOST, GAUGE_PORT), Handler) as server:
        server.hub_page = page.replace("<body>", '<body class="hub-view">').replace("<!-- admin footer -->", "")
        server.page = page.replace("<!-- admin footer -->", admin_footer)
        server.public_host = None
        try:
            zone = (PROJECT / "data" / "admin" / "duckdns-zone").read_text(encoding="ascii").strip()
        except (OSError, ValueError):
            zone = ""
        if re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.duckdns\.org", zone):
            server.public_host = f"stats.{zone}:8443"
            server.public_page = server.page.replace("https://192.168.50.10:8443/", f"https://admin.{zone}:8443/")
        stop = threading.Event()
        collector = threading.Thread(target=collect_history, args=(stop,), daemon=True)
        collector.start()
        print(f"Hot water gauge on http://{GAUGE_HOST}:{GAUGE_PORT}/ reading {RELAY_STATE}", flush=True)
        print(f"Hot water history: {analytics.HISTORY_DB} (polling every {POLL_S}s)", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()
            collector.join()


if __name__ == "__main__":
    main()

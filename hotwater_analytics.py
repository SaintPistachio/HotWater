"""Estimate remaining hot water and record heater telemetry locally."""

import math
import os
import sqlite3
import time
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import median

WH_PER_GALLON_DEGREE = 8.34 / 3.412
TANK_GALLONS = float(os.environ.get("TANK_GALLONS", "45"))
INLET_F = float(os.environ.get("INLET_F", "55"))
COMFORT_F = 105  # Mixed water reference for the estimate, independent of the thermostat.
RATED_WATTS = float(os.environ.get("RATED_WATTS", "5500"))
HISTORY_DB = Path(os.environ.get("HOTWATER_HISTORY_DB",
                                 Path(__file__).resolve().parent / "data" / "history.sqlite3"))
STALE_S = 600  # Same freshness window as the relay.
REFERENCE_GALLONS = 16
MAX_HEATING_W = 7500
MAX_DRAW_GPM = 12


def number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def full_tank_wh(setpoint):
    return max(TANK_GALLONS * WH_PER_GALLON_DEGREE * (setpoint - INLET_F), 1)


def tank_charge(setpoint, deficit):
    # Vacation targets do not define a usable hot tank.
    if (setpoint is None or not math.isfinite(setpoint) or setpoint < COMFORT_F or setpoint <= INLET_F
            or deficit is None or not math.isfinite(deficit) or deficit < 0):
        return None
    return max(0, min(100, round(100 * (1 - deficit / full_tank_wh(setpoint)))))


def available_gallons(top, bottom, setpoint, deficit):
    """Estimate mixed water at COMFORT_F from the usable part of the tank."""
    if (top is None or bottom is None or not 32 <= bottom <= 170
            or not 32 <= top <= 170 or not 32 <= INLET_F < COMFORT_F
            or not 0 < TANK_GALLONS):
        return None
    if top < COMFORT_F:
        return 0.0

    # Only count the part of the profile above the hot water cutoff.
    usable_fraction = 1.0
    if bottom < COMFORT_F < top:
        usable_fraction = (top - COMFORT_F) / (top - bottom)
    elif bottom < COMFORT_F:
        return 0.0
    usable_bottom = max(bottom, COMFORT_F)
    mean_usable_temp = (top + usable_bottom) / 2
    heat = TANK_GALLONS * usable_fraction * (mean_usable_temp - INLET_F)
    if setpoint is not None and setpoint >= COMFORT_F and deficit is not None:
        reported_heat = (full_tank_wh(setpoint) - max(0, deficit)) / WH_PER_GALLON_DEGREE
        heat = max(0, min(heat, reported_heat))
    return round(heat / (COMFORT_F - INLET_F), 1)


def usable_reading(sample):
    age = sample["age_s"]
    return (sample["online"] is not False and not sample["contents_stale"]
            and age is not None and 0 <= age < STALE_S)


def open_history():
    return sqlite3.connect(HISTORY_DB, timeout=2)


def initialize_history():
    with closing(open_history()) as db, db:
        db.execute("""CREATE TABLE IF NOT EXISTS samples (
            stamp REAL PRIMARY KEY, gallons REAL, tank_pct INTEGER, setpoint REAL,
            deficit_wh REAL, energy_wh REAL
        )""")


def record(sample):
    if not usable_reading(sample) or sample["stamp"] is None:
        return
    target = sample["setpoint"]
    deficit = sample["deficit_wh"]
    percent = None if sample.get("fault") else tank_charge(target, deficit)
    # Retain the counter across mode changes so daily energy remains measurable.
    with closing(open_history()) as db, db:
        db.execute("INSERT OR IGNORE INTO samples VALUES (?, ?, ?, ?, ?, ?)", (
            sample["stamp"], sample["available_gallons"] if percent is not None else None,
            percent, target, deficit if percent is not None else None, sample["energy_wh"],
        ))


def suspect_charge_dip(before, row, after):
    old_stamp, _, old_pct, old_target, old_deficit, _ = before
    stamp, _, pct, target, deficit, _ = row
    elapsed = stamp - old_stamp
    if target != old_target or old_pct is None or old_pct - pct < 10 or not 0 < elapsed <= STALE_S:
        return False
    if after is None:
        # Hold an unusually sharp latest drop until another reading can confirm it.
        max_use = MAX_DRAW_GPM * elapsed / 60 * WH_PER_GALLON_DEGREE * (COMFORT_F - INLET_F)
        return deficit - old_deficit > max_use + 200
    next_stamp, _, next_pct, next_target, next_deficit, _ = after
    if target != next_target or next_pct is None or next_pct - pct < 10 or not 0 < next_stamp - stamp <= STALE_S:
        return False
    # A one-reading dip cannot reverse by more than the elements could add, plus telemetry rounding.
    return deficit - next_deficit > MAX_HEATING_W * (next_stamp - stamp) / 3600 + 200


def history_rows(start, end, *, include_suspect_energy=False):
    with closing(open_history()) as db:
        rows = db.execute("""SELECT stamp, gallons, tank_pct, setpoint, deficit_wh, energy_wh FROM samples
                             WHERE stamp BETWEEN ? AND ? ORDER BY stamp""", (start - STALE_S, end)).fetchall()

    for index, (stamp, _, pct, target, deficit, energy) in enumerate(rows):
        if pct is None or not 0 <= pct <= 100 or tank_charge(target, deficit) is None:
            rows[index] = (stamp, None, None, target, None, energy)

    for index, row in enumerate(rows):
        stamp, gallons, pct, target, deficit, energy = row
        if stamp < start:
            continue
        after = rows[index + 1] if index + 1 < len(rows) else None
        if pct is not None and index > 0 and suspect_charge_dip(rows[index - 1], row, after):
            if not include_suspect_energy:
                continue
            gallons = pct = deficit = None
        yield stamp, gallons, pct, target, deficit, energy


def energy_delta(old, new, seconds):
    if old is None or new is None or new < old:
        return None
    delta = new - old
    # Guard against a counter reset or discontinuous reading.
    return delta if delta <= MAX_HEATING_W * seconds / 3600 + 100 else None


def draw_volume(old_deficit, deficit, old_energy, energy, seconds):
    heating = energy_delta(old_energy, energy, seconds)
    if old_deficit is None or deficit is None or heating is None or not INLET_F < COMFORT_F:
        return None
    change = deficit - old_deficit
    gallons = (change + heating) / (WH_PER_GALLON_DEGREE * (COMFORT_F - INLET_F))
    return gallons if change >= 100 and 0.65 <= gallons * 60 / seconds <= MAX_DRAW_GPM else 0


def recent_flow(sample):
    """Infer the equivalent hot water draw from tank cooling plus heating input."""
    stamp = sample["stamp"]
    if (not usable_reading(sample) or sample["age_s"] > 120 or stamp is None
            or sample["available_gallons"] is None or sample["percent"] is None
            or sample["setpoint"] is None or sample["setpoint"] < COMFORT_F or sample["fault"]):
        return None
    rows = list(history_rows(stamp - 240, stamp))
    candidates = []
    for old_stamp, old_gallons, _, old_target, old_deficit, old_energy in reversed(rows):
        if old_target != sample["setpoint"]:
            break
        seconds = stamp - old_stamp
        if seconds >= 45 and old_deficit is not None:
            candidates.append((abs(seconds - 90), seconds, old_deficit, old_energy, old_gallons))
    if not candidates:
        return None

    _, seconds, old_deficit, old_energy, old_gallons = min(candidates, key=lambda row: row[0])
    gallons = draw_volume(old_deficit, sample["deficit_wh"], old_energy, sample["energy_wh"], seconds)
    if not gallons:
        return None
    gpm = gallons * 60 / seconds
    remaining = sample["available_gallons"]
    minutes = None
    if old_gallons is not None and old_gallons - remaining >= 0.5:
        minutes = round(remaining * seconds / (old_gallons - remaining) / 60)
    return {"gpm": round(gpm, 1), "minutes": minutes, "window_s": round(seconds)}


def recovery_rates(rows):
    """Find sustained recoveries whose heat gain agrees with the power counter."""
    rates = []
    baseline = None
    for stamp, target, deficit, energy in rows:
        if target is None or target < COMFORT_F or deficit is None or energy is None:
            baseline = None
            continue
        if baseline is None:
            baseline = (stamp, target, deficit, energy)
            continue
        start, old_target, old_deficit, old_energy = baseline
        seconds = stamp - start
        if target == old_target and seconds < 60:
            continue
        baseline = (stamp, target, deficit, energy)
        if target != old_target or seconds > 300:
            continue
        heat_in = energy_delta(old_energy, energy, seconds)
        recovered = old_deficit - deficit
        if (heat_in and recovered >= 75 and 0.6 <= recovered / heat_in <= 1.25):
            rates.append(recovered * 3600 / seconds)
    return rates


def recovery(sample, rates):
    if (not usable_reading(sample) or sample["fault"] or sample["percent"] is None
            or sample["stamp"] is None or sample["setpoint"] is None
            or sample["setpoint"] < COMFORT_F or not 32 <= INLET_F < COMFORT_F):
        return None
    learned = len(rates) >= 3
    rate = median(rates) if learned else RATED_WATTS
    if not 0 < rate <= MAX_HEATING_W:
        return None
    minutes = 0 if sample["percent"] == 100 else max(0, math.ceil(sample["deficit_wh"] * 60 / rate))
    return {"minutes": minutes,
            "gallons_per_hour": round(rate / (WH_PER_GALLON_DEGREE * (COMFORT_F - INLET_F)), 1),
            "learned": learned, "samples": len(rates)}


def recent_recovery_rates(stamp):
    if stamp is None:
        return []
    return recovery_rates((time_s, target, deficit, energy) for time_s, _, _, target, deficit, energy
                          in history_rows(stamp - 7 * 86400, stamp))


def local_stamp(day, hour=0):
    return datetime(day.year, day.month, day.day, hour).timestamp()


def history(now=None):
    now = time.time() if now is None else now
    today = date.fromtimestamp(now)
    first_day = today - timedelta(days=6)
    # Each plotted five-minute bucket keeps its lowest sample and real timestamp.
    buckets = {}
    daily_lows = {}
    daily_energy = {}
    previous = None
    for stamp, _, percent, _, _, energy in history_rows(local_stamp(first_day), now, include_suspect_energy=True):
        day = date.fromtimestamp(stamp)
        if percent is not None:
            if stamp >= now - 86400:
                bucket = int(stamp // 300)
                if bucket not in buckets or percent < buckets[bucket][1]:
                    buckets[bucket] = (stamp, percent)
            daily_lows[day] = min(percent, daily_lows.get(day, percent))
        if previous is not None and previous[0] == day:
            _, old_stamp, old_energy = previous
            delta = energy_delta(old_energy, energy, stamp - old_stamp)
            if delta is not None:
                daily_energy[day] = daily_energy.get(day, 0) + delta
        if energy is not None:
            previous = (day, stamp, energy)

    days = []
    for offset in range(7):
        day = today - timedelta(days=offset)
        wh = daily_energy.get(day)
        days.append({"date": day.isoformat(), "low_pct": daily_lows.get(day),
                     "observed_kwh": round(wh / 1000, 2) if wh is not None else None})
    return {"start": now - 86400, "end": now, "points": list(buckets.values()), "days": days}


def observed_draws(rows):
    """Return inferred draw episodes and bracket each with observed no-draw intervals."""
    episodes = []
    baseline = None
    event = None
    recovering = None
    preceded_by_quiet = False
    for stamp, target, deficit, energy, gallons in rows:
        if baseline is None:
            baseline = (stamp, target, deficit, energy, gallons)
            continue
        start, old_target, old_deficit, old_energy, old_gallons = baseline
        seconds = stamp - start
        if target == old_target and seconds < 60 and deficit is not None and energy is not None:
            continue
        baseline = (stamp, target, deficit, energy, gallons)
        volume = None
        if (60 <= seconds <= 360 and target == old_target and target is not None
                and target >= COMFORT_F and gallons is not None and old_gallons is not None):
            volume = draw_volume(old_deficit, deficit, old_energy, energy, seconds)
        if volume is None:
            event = recovering = None
            preceded_by_quiet = False
        elif volume > 0:
            if event is None:
                recovering = None
                event = {"start": start, "end": stamp, "before": old_gallons,
                         "low": min(gallons, old_gallons), "volume": 0.0,
                         "complete": False, "recovered": None}
                event["quiet_start"] = preceded_by_quiet
                episodes.append(event)
            event["end"] = stamp
            event["low"] = min(event["low"], gallons)
            event["volume"] += volume
            preceded_by_quiet = False
        else:
            if event is not None:
                event["complete"] = event["quiet_start"]
                recovering, event = event, None
            if recovering is not None and gallons >= recovering["before"]:
                recovering["recovered"] = stamp
                recovering = None
            preceded_by_quiet = True
    return episodes


def has_daily_coverage(points, start, end):
    """Reserve readings cover the day, or today so far, within the freshness window."""
    return (len(points) >= 2 and points[0][0] - start <= STALE_S and end - points[-1][0] <= STALE_S
            and all(b[0] - a[0] <= STALE_S for a, b in zip(points, points[1:])))


def insights(now=None):
    now = time.time() if now is None else now
    today = date.fromtimestamp(now)
    first = today - timedelta(days=14)
    rows = [(stamp, target, deficit, energy, gallons) for stamp, gallons, _, target, deficit, energy
            in history_rows(local_stamp(first), now)]
    daily_reserves = {}
    for stamp, _, _, _, gallons in rows:
        if gallons is not None:
            daily_reserves.setdefault(date.fromtimestamp(stamp), []).append((stamp, gallons))
    draws = observed_draws(rows)
    replay = []
    for offset in range(7):
        day = today - timedelta(days=offset)
        start, end = local_stamp(day), local_stamp(day + timedelta(days=1))
        points = daily_reserves.get(day, [])
        if points:
            replay.append({"date": day.isoformat(), "covered": has_daily_coverage(points, start, min(end, now)),
                           "low": round(min(gallons for _, gallons in points), 1),
                           "draws": [draw for draw in draws if start <= draw["start"] < end]})

    def duration(draw):
        return (draw["end"] - draw["start"]) / 60

    eligible = [draw for draw in draws if draw["complete"]]
    ranked = sorted(eligible, key=lambda draw: draw["volume"] / duration(draw))
    groups = []
    if len(ranked) >= 6:
        midpoint = len(ranked) // 2
        for name, group in (("Lighter", ranked[:midpoint]), ("Heavier", ranked[midpoint:])):
            groups.append({"name": name, "count": len(group), "loss_per_minute": round(median(
                max(0, draw["before"] - draw["low"]) / duration(draw) for draw in group), 1)})

    longer_draws = [draw for draw in ranked if duration(draw) >= 5]
    lighter_draws = longer_draws[:(len(longer_draws) + 1) // 2]
    learned_reference = len(lighter_draws) >= 3
    reference = median(draw["volume"] for draw in lighter_draws) if learned_reference else REFERENCE_GALLONS
    week_start = local_stamp(today - timedelta(days=6))
    previous_start = local_stamp(today - timedelta(days=13))
    trend = []
    for start, end in ((previous_start, week_start), (week_start, now + 1)):
        rates = recovery_rates(row[:4] for row in rows if start <= row[0] < end)
        trend.append({"count": len(rates), "gallons_per_hour": round(median(rates) /
                      (WH_PER_GALLON_DEGREE * (COMFORT_F - INLET_F)), 1) if len(rates) >= 3 else None})
    return {"reference": round(reference, 1), "learned_reference": learned_reference,
            "replay": replay, "draw_groups": groups, "recovery_trend": trend}

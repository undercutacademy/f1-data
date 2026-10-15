#!/usr/bin/env python3
"""
Build one session's data tree from OpenF1 (api.openf1.org) instead of FastF1.

F1's livetiming feed (what FastF1 reads) rejects GitHub-Actions / datacenter IPs,
but OpenF1 does not, so this is what the cloud ingester runs. It writes the same
files, keys and formats as process_session.py so the frontend can't tell them apart:

  drivers.json, corners.json, laps/{DRV}.json, telemetry/{DRV}_{LAP}.json,
  analysis.json, weather.json

Differences from the FastF1 path (documented, not bugs):
  - driver_ahead / dist_ahead exist for races only (from OpenF1 `intervals`, which
    is a time gap; converted to metres with the car's own speed).
  - corners come from the MultiViewer circuit API (same source FastF1 uses).
  - `deleted` is null (OpenF1 has no per-lap deletion flag).

Usage:
    python scripts/openf1_session.py <year> "<event name>" <session_type>
Env:
    F1_DATA_OUT   output root (default: repo root)
"""
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

API = "https://api.openf1.org/v1"
NUM_POINTS = 500
QUICKLAP_THRESHOLD = 1.07  # FastF1's pick_quicklaps default, relative to the driver's best
# Free tier: 3 req/s and 30 req/min. Stay just under the per-minute cap.
MIN_REQUEST_GAP_S = 2.1

SESSION_NAMES = {
    "FP1": {"Practice 1"}, "FP2": {"Practice 2"}, "FP3": {"Practice 3"},
    "Q": {"Qualifying"}, "SQ": {"Sprint Qualifying", "Sprint Shootout"},
    "S": {"Sprint"}, "R": {"Race"},
}
RACE_TYPES = {"R", "S"}


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def fmt_lap(seconds) -> str:
    """Same format as process_session.fmt_lap: M:SS.mmm or N/A."""
    if seconds is None or (isinstance(seconds, float) and math.isnan(seconds)) or seconds <= 0:
        return "N/A"
    mins = int(seconds // 60)
    return f"{mins}:{seconds % 60:06.3f}"


def parse_ts(s: str) -> float:
    """OpenF1 ISO timestamp -> POSIX seconds."""
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")


# ─── HTTP ─────────────────────────────────────────────────────────────────────

_last_request = 0.0


def get(endpoint: str, **params):
    """GET an OpenF1 endpoint. 404 means 'no rows' and returns []."""
    global _last_request
    query = "&".join(f"{k}={urllib.parse.quote(str(v), safe='<>=:.-T')}" for k, v in params.items())
    url = f"{API}/{endpoint}?{query}" if query else f"{API}/{endpoint}"
    for attempt in range(6):
        wait = MIN_REQUEST_GAP_S - (time.monotonic() - _last_request)
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "f1-data-ingest (overcutacademy)"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return []
            if e.code in (429, 500, 502, 503, 504) and attempt < 5:
                time.sleep(10 * (attempt + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt < 5:
                time.sleep(10 * (attempt + 1))
                continue
            raise
    return []


# ─── Session lookup ───────────────────────────────────────────────────────────

def find_session(year: int, event_name: str, session_type: str, start_utc: datetime | None = None):
    """The OpenF1 session for (event, type). Matched on start time when known
    (robust to naming differences), else on meeting + session name."""
    sessions = get("sessions", year=year)
    names = SESSION_NAMES[session_type]
    if start_utc is not None:
        for s in sessions:
            if abs(parse_ts(s["date_start"]) - start_utc.timestamp()) <= 3600 and s["session_name"] in names:
                return s
    meetings = {m["meeting_key"]: m for m in get("meetings", year=year)}
    want = slugify(event_name)
    for s in sessions:
        m = meetings.get(s["meeting_key"], {})
        if slugify(m.get("meeting_name", "")) == want and s["session_name"] in names:
            return s
    return None


# ─── Signal helpers (mirror process_session.py) ───────────────────────────────

def _interp(dist, vals, common_dist):
    return np.interp(common_dist, dist, np.asarray(vals, dtype=float))


def _nearest_before(dist, vals, common_dist):
    idx = np.clip(np.searchsorted(dist, common_dist, side="right") - 1, 0, len(dist) - 1)
    return np.asarray(vals)[idx]


def step_lookup(times, values, t, default=None):
    """Value of a step series (sorted times) at time t."""
    i = int(np.searchsorted(times, t, side="right")) - 1
    return values[i] if i >= 0 else default


# ─── Builders ─────────────────────────────────────────────────────────────────

def build_drivers(drivers_raw, results):
    by_num = {}
    for d in drivers_raw:  # endpoint can repeat a driver; last one wins
        by_num[d["driver_number"]] = d
    order = {r["driver_number"]: i for i, r in enumerate(results)}
    out = []
    for num, d in sorted(by_num.items(), key=lambda kv: (order.get(kv[0], 999), kv[0])):
        colour = (d.get("team_colour") or "FFFFFF").lstrip("#")
        first, last = d.get("first_name") or "", d.get("last_name") or ""
        out.append({
            "abbreviation": d["name_acronym"],
            "full_name": f"{first} {last}".strip() or d.get("full_name", d["name_acronym"]),
            "number": str(num),
            "team": d.get("team_name") or "",
            "team_color": f"#{colour}",
        })
    return out


def sort_results(results):
    """Classified first by position, then unclassified by laps completed.
    OpenF1 sometimes gives a non-numeric position (seen in 2026 qualifying);
    those are treated as unclassified."""
    results = [dict(r, position=r["position"] if isinstance(r.get("position"), int) else None)
               for r in results]
    return sorted(results, key=lambda r: (r.get("position") is None, r.get("position") or 0,
                                          -(r.get("number_of_laps") or 0)))


def status_windows(race_control, laps_by_driver):
    """(kind, start_ts, end_ts) for SC / VSC / red flag, kind as FastF1 digit."""
    typical_lap = np.median([l["lap_duration"] for ls in laps_by_driver.values() for l in ls
                             if l.get("lap_duration")] or [90.0])
    msgs = sorted(race_control, key=lambda m: m["date"])
    windows, open_ = [], {}
    for m in msgs:
        t, text, flag = parse_ts(m["date"]), (m.get("message") or "").upper(), (m.get("flag") or "").upper()
        if "VSC DEPLOYED" in text or "VIRTUAL SAFETY CAR DEPLOYED" in text:
            open_["6"] = t
        elif ("VSC ENDING" in text or "VIRTUAL SAFETY CAR ENDING" in text) and "6" in open_:
            windows.append(("6", open_.pop("6"), t + 15))
        elif "SAFETY CAR DEPLOYED" in text:
            open_["4"] = t
        elif "SAFETY CAR IN THIS LAP" in text and "4" in open_:
            # SC laps run until the end of the lap in which it comes in.
            windows.append(("4", open_.pop("4"), t + typical_lap))
        elif flag == "RED" or "RED FLAG" in text:
            # Races post it as a message ("RED FLAG - RACE SUSPENDED"), not a flag.
            open_["5"] = t
        elif "5" in open_ and (flag == "GREEN" or "STANDING START" in text or "ROLLING START" in text):
            windows.append(("5", open_.pop("5"), t))
    end = max((parse_ts(m["date"]) for m in msgs), default=0) + 3600
    windows += [(k, s, end) for k, s in open_.items()]
    return windows


def lap_status(windows, start, end):
    codes = sorted({k for k, s, e in windows if s < end and e > start})
    return "".join(codes) if codes else "1"


def corner_list(circuit, ref_tel):
    """corners.json entries; distance = nearest point on a reference lap (as FastF1)."""
    out = []
    if not circuit or not ref_tel:
        return out
    rx, ry, rd = np.array(ref_tel["x"]), np.array(ref_tel["y"]), np.array(ref_tel["distance"])
    for c in circuit.get("corners", []):
        tp = c.get("trackPosition") or {}
        x, y = float(tp.get("x", 0)) * 0.1, float(tp.get("y", 0)) * 0.1
        i = int(np.argmin((rx - x) ** 2 + (ry - y) ** 2))
        out.append({
            "number": int(c.get("number", 0)),
            "letter": str(c.get("letter", "") or ""),
            "distance": float(rd[i]),
            "angle": float(c.get("angle", 0)),
            "x": x,
            "y": y,
        })
    return out


def lap_telemetry(car, loc, t0, t1, ahead=None):
    """Pre-normalized 500-point telemetry for one lap, same keys as process_session."""
    ct = car["t"]
    inside = (ct > t0) & (ct < t1)
    if inside.sum() < 50:
        return None
    # Samples inside the lap plus exact boundary points at the lap's start and end:
    # continuous channels interpolated there, step channels take the last value.
    t = np.concatenate([[t0], ct[inside], [t1]])
    idx = np.clip(np.searchsorted(ct, t, side="right") - 1, 0, len(ct) - 1)
    sl = {}
    for k, v in car.items():
        if k == "t":
            continue
        sl[k] = np.interp(t, ct, v) if k in ("speed", "throttle", "rpm") else v[idx]

    # Distance by integrating speed over time, as FastF1's add_distance does.
    v = sl["speed"] / 3.6
    dist = np.concatenate([[0.0], np.cumsum((v[1:] + v[:-1]) / 2 * np.diff(t))])
    max_dist = float(dist[-1])
    if max_dist <= 0:
        return None
    common = np.linspace(0, max_dist, NUM_POINTS)

    def pos(axis):
        return np.interp(t, loc["t"], loc[axis]) if len(loc["t"]) else np.zeros(len(t))

    data = {
        "distance": np.round(common, 1).tolist(),
        "speed": np.round(_interp(dist, sl["speed"], common), 1).tolist(),
        "throttle": np.round(_interp(dist, sl["throttle"], common), 1).tolist(),
        "rpm": np.round(_interp(dist, sl["rpm"], common), 0).astype(int).tolist(),
        "gear": _nearest_before(dist, sl["gear"], common).astype(int).tolist(),
        "brake": _nearest_before(dist, sl["brake"], common).astype(int).tolist(),
        "drs": _nearest_before(dist, sl["drs"], common).astype(int).tolist(),
        "time": np.round(_interp(dist, t - t0, common), 4).tolist(),
        "x": np.round(_interp(dist, pos("x") * 0.1, common), 1).tolist() if len(loc["t"]) else [],
        "y": np.round(_interp(dist, pos("y") * 0.1, common), 1).tolist() if len(loc["t"]) else [],
        "z": np.round(_interp(dist, pos("z") * 0.1, common), 2).tolist() if len(loc["t"]) else [],
    }

    if ahead is not None:
        abs_t = t0 + np.interp(common, dist, t - t0)
        speed_ms = np.interp(common, dist, v)
        drv_ahead, dist_ahead = [], []
        for ts, sp in zip(abs_t, speed_ms):
            who, gap = ahead(ts)
            drv_ahead.append(who)
            dist_ahead.append(None if gap is None else round(float(gap * sp), 1))
        data["driver_ahead"] = drv_ahead
        data["dist_ahead"] = dist_ahead
    return data


def load_start_lines():
    p = Path(__file__).resolve().parent / "start_lines.json"
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


def line_offset(loc, line, boundaries):
    """Seconds to add to OpenF1 lap boundaries so laps start on the timing line.

    OpenF1's lap timestamps carry a per-driver constant offset vs the car's own
    position data (0.1–1.8 s in 2026), which would start every lap up to ~150 m
    past the line. Lap durations are exact, so one constant per driver fixes it:
    the median gap between each boundary and the car's actual line crossing
    (closest approach to the line point, interpolated between position samples).
    Returns 0.0 if the crossings can't be found reliably."""
    if not line or not len(loc["t"]):
        return 0.0
    lt, lx, ly = loc["t"], loc["x"] * 0.1, loc["y"] * 0.1  # stored units, like the line
    px, py = line["x"], line["y"]
    offsets = []
    for b in boundaries:
        idx = np.nonzero((lt >= b - 4) & (lt <= b + 2))[0]
        if len(idx) < 3:
            continue
        x0, y0, x1, y1 = lx[idx[:-1]], ly[idx[:-1]], lx[idx[1:]], ly[idx[1:]]
        sx, sy = x1 - x0, y1 - y0
        seg2 = np.maximum(sx * sx + sy * sy, 1e-9)
        u = np.clip(((px - x0) * sx + (py - y0) * sy) / seg2, 0, 1)
        dist = np.hypot(x0 + u * sx - px, y0 + u * sy - py)
        k = int(np.argmin(dist))
        if dist[k] > 25:  # metres: not near the line (pit lane, missing data)
            continue
        t_cross = lt[idx[k]] + u[k] * (lt[idx[k] + 1] - lt[idx[k]])
        offsets.append(t_cross - b)
    if len(offsets) < 3:
        return 0.0
    off = float(np.median(offsets))
    return off if abs(off) < 3 else 0.0


def series(rows, fields):
    """Columnar numpy arrays sorted by time from OpenF1 rows."""
    rows = [r for r in rows if r.get("date")]
    rows.sort(key=lambda r: r["date"])
    out = {"t": np.array([parse_ts(r["date"]) for r in rows])}
    for name, (src, default) in fields.items():
        out[name] = np.array([r.get(src) if r.get(src) is not None else default for r in rows], dtype=float)
    return out


def interval_seconds(v):
    """OpenF1 interval: float seconds, or strings like '+1 LAP' (lapped) -> None."""
    return float(v) if isinstance(v, (int, float)) else None


# ─── Main ─────────────────────────────────────────────────────────────────────

def process(year: int, event_name: str, session_type: str, start_utc: datetime | None = None) -> bool:
    root = Path(os.environ.get("F1_DATA_OUT") or Path(__file__).resolve().parent.parent)
    session = find_session(year, event_name, session_type, start_utc)
    if not session:
        print(f"OpenF1 has no session for {year} {event_name} {session_type}")
        return False
    sk = session["session_key"]
    t_session = parse_ts(session["date_start"])
    race = session_type in RACE_TYPES
    print(f"OpenF1 session {sk}: {session['session_name']} {session['date_start']}")

    laps_raw = get("laps", session_key=sk)
    if not laps_raw:
        print("No laps yet - data not available.")
        return False
    drivers_raw = get("drivers", session_key=sk)
    results = sort_results(get("session_result", session_key=sk))
    stints_raw = get("stints", session_key=sk)
    pits_raw = get("pit", session_key=sk)
    positions_raw = get("position", session_key=sk)
    race_control = get("race_control", session_key=sk)
    weather_raw = get("weather", session_key=sk)
    intervals_raw = get("intervals", session_key=sk) if race else []
    meeting = (get("meetings", meeting_key=session["meeting_key"]) or [{}])[0]
    circuit = None
    if meeting.get("circuit_info_url"):
        try:
            req = urllib.request.Request(meeting["circuit_info_url"], headers={"User-Agent": "f1-data-ingest"})
            with urllib.request.urlopen(req, timeout=60) as r:
                circuit = json.loads(r.read())
        except Exception as e:  # corners are optional
            print(f"  Warning: circuit info: {e}")

    drivers = build_drivers(drivers_raw, results)
    abbr = {int(d["number"]): d["abbreviation"] for d in drivers}

    session_dir = root / str(year) / slugify(event_name) / session_type
    (session_dir / "laps").mkdir(parents=True, exist_ok=True)
    (session_dir / "telemetry").mkdir(parents=True, exist_ok=True)

    laps_by_driver = {}
    for l in laps_raw:
        laps_by_driver.setdefault(l["driver_number"], []).append(l)
    for ls in laps_by_driver.values():
        ls.sort(key=lambda l: l["lap_number"])
    stints_by_driver = {}
    for s in stints_raw:
        stints_by_driver.setdefault(s["driver_number"], []).append(s)
    pit_laps = {(p["driver_number"], p.get("lap_number")) for p in pits_raw}
    pos_series = {}
    for p in sorted(positions_raw, key=lambda p: p["date"]):
        ts, vs = pos_series.setdefault(p["driver_number"], ([], []))
        ts.append(parse_ts(p["date"]))
        vs.append(p["position"])
    windows = status_windows(race_control, laps_by_driver)

    # Car ahead (races): who holds position-1 at time t, and the interval to them.
    ahead_fn_for = {}
    if race and intervals_raw:
        iv_series = {}
        for r in sorted(intervals_raw, key=lambda r: r["date"]):
            ts, vs = iv_series.setdefault(r["driver_number"], ([], []))
            ts.append(parse_ts(r["date"]))
            vs.append(interval_seconds(r.get("interval")))

        def position_at(num, t):
            ts, vs = pos_series.get(num, ([], []))
            return step_lookup(ts, vs, t)

        def make(num):
            def ahead(t):
                p = position_at(num, t)
                if not p or p <= 1:
                    return "", None
                who = next((str(o) for o in pos_series if o != num and position_at(o, t) == p - 1), "")
                ts, vs = iv_series.get(num, ([], []))
                return who, step_lookup(ts, vs, t)
            return ahead
        ahead_fn_for = {num: make(num) for num in laps_by_driver}

    pits_by_driver = {}
    for p in pits_raw:
        if p.get("lap_number"):
            pits_by_driver.setdefault(p["driver_number"], set()).add(p["lap_number"])

    def stint_table(num):
        """OpenF1 stints, with boundaries corrected from pit stops.

        OpenF1's stint lap ranges are sometimes wrong (2026 Singapore: ALO/HAD shown
        switching tyres after lap 1 although they pitted on laps 35/33), while its
        pit records and stint compound order are right. When there is exactly one
        more stint than pit stops, rebuild the ranges from the pit laps (pit lap =
        in-lap, so the next stint starts the lap after)."""
        st = sorted((s for s in stints_by_driver.get(num, []) if s.get("lap_start") is not None),
                    key=lambda s: (s.get("stint_number") or 0, s["lap_start"]))
        pits = sorted(pits_by_driver.get(num, set()))
        if st and len(st) == len(pits) + 1:
            starts = [1] + [p + 1 for p in pits]
            return [dict(s, lap_start=starts[i]) for i, s in enumerate(st)]
        return st

    stint_tables = {num: stint_table(num) for num in laps_by_driver}

    def stint_for(num, lap_no):
        # Latest stint that started on or before this lap; the last stint runs to the
        # flag even when OpenF1's lap_end stops a lap short.
        found = None
        for s in stint_tables.get(num, []):
            if s["lap_start"] <= lap_no:
                found = s
        return found

    analysis_laps, generated = [], set()
    tel_saved, ref_tel, ref_time = 0, None, None
    start_line = load_start_lines().get(slugify(event_name))
    if not start_line:
        print(f"  Warning: no start line for {slugify(event_name)}; laps keep OpenF1's raw boundaries")
    # Pass 1: download each driver's car/position data and measure its line offset.
    car_by, loc_by, line_shifts = {}, {}, {}
    for d in drivers:
        num, code = int(d["number"]), d["abbreviation"]
        dlaps = laps_by_driver.get(num, [])
        if not dlaps:
            continue
        car = series(get("car_data", session_key=sk, driver_number=num), {
            "speed": ("speed", 0), "throttle": ("throttle", 0), "rpm": ("rpm", 0),
            "gear": ("n_gear", 0), "brake": ("brake", 0), "drs": ("drs", 0),
        })
        car["brake"] = (car["brake"] > 0).astype(int)  # OpenF1 0/100 -> FastF1 bool
        car_by[num] = car
        loc_by[num] = series(get("location", session_key=sk, driver_number=num),
                             {"x": ("x", 0), "y": ("y", 0), "z": ("z", 0)})
        # Lap 1 (grid start) and pit-exit laps don't begin on the line.
        boundaries = [parse_ts(l["date_start"]) for l in dlaps
                      if l.get("date_start") and l["lap_number"] > 1 and not l.get("is_pit_out_lap")]
        off = line_offset(loc_by[num], start_line, boundaries)
        if off:
            line_shifts[num] = off
    # Drivers with too few laps to measure (early retirements) take the session median:
    # the offset is near-identical across a session (±0.07 s measured).
    fallback_shift = float(np.median(list(line_shifts.values()))) if line_shifts else 0.0
    shift_of = {num: line_shifts.get(num, fallback_shift) for num in car_by}

    # Lap boundaries on the true line; a lap with no recorded time (red flag)
    # ends where the next one starts.
    bounds = {}
    for num, dlaps in laps_by_driver.items():
        starts = {l["lap_number"]: parse_ts(l["date_start"]) + shift_of.get(num, fallback_shift)
                  for l in dlaps if l.get("date_start")}
        for l in dlaps:
            n, s = l["lap_number"], starts.get(l["lap_number"])
            e = s + l["lap_duration"] if s is not None and l.get("lap_duration") else starts.get(n + 1)
            bounds[(num, n)] = (s, e)

    # Race positions: running order at the line = rank of lap-end crossing times
    # among cars completing that lap (lapped cars complete it later, so they rank
    # behind). Exact, unlike OpenF1's position feed, which is stamped late.
    race_pos = {}
    if race:
        by_lap = {}
        for (num, n), (s, e) in bounds.items():
            if e is not None:
                by_lap.setdefault(n, []).append((e, num))
        for n, rows in by_lap.items():
            for i, (_, num) in enumerate(sorted(rows)):
                race_pos[(num, n)] = i + 1

    # Pass 2: write per-driver files.
    for d in drivers:
        num, code = int(d["number"]), d["abbreviation"]
        dlaps = laps_by_driver.get(num, [])
        if not dlaps:
            continue
        times = [l["lap_duration"] for l in dlaps if l.get("lap_duration")]
        best = min(times) if times else None
        threshold = best * QUICKLAP_THRESHOLD if best else None
        car, loc = car_by[num], loc_by[num]

        out, running_best = [], None
        for l in dlaps:
            n, lt = l["lap_number"], l.get("lap_duration")
            start, end = bounds.get((num, n), (None, None))

            valid = False
            if threshold and lt and lt <= threshold and start is not None and end is not None and len(car["t"]):
                tel = lap_telemetry(car, loc, start, end, ahead_fn_for.get(num))
                if tel:
                    with open(session_dir / "telemetry" / f"{code}_{n}.json", "w") as f:
                        json.dump(tel, f, separators=(",", ":"))
                    valid = True
                    generated.add((code, n))
                    tel_saved += 1
                    if ref_time is None or lt < ref_time:
                        ref_tel, ref_time = tel, lt

            st = stint_for(num, n)
            tyre_life = (st.get("tyre_age_at_start") or 0) + (n - st["lap_start"]) + 1 if st else 0
            is_pb = bool(lt) and (running_best is None or lt < running_best)
            if lt and is_pb:
                running_best = lt
            s1, s2, s3 = l.get("duration_sector_1"), l.get("duration_sector_2"), l.get("duration_sector_3")
            out.append({
                "lap_number": n,
                "lap_time": fmt_lap(lt),
                "lap_time_seconds": round(lt, 3) if lt else None,
                "sector1": fmt_lap(s1),
                "sector2": fmt_lap(s2),
                "sector3": fmt_lap(s3),
                "compound": (st.get("compound") or "UNKNOWN").upper() if st else "UNKNOWN",
                "tyre_life": int(tyre_life),
                "is_personal_best": is_pb,
                "is_fastest": bool(lt and best and abs(lt - best) < 0.001),
                "is_valid": valid,
            })

            analysis_laps.append({
                "driver": code, "lap_number": n,
                "lap_time": round(lt, 3) if lt else None,
                "s1": round(s1, 3) if s1 else None, "s2": round(s2, 3) if s2 else None,
                "s3": round(s3, 3) if s3 else None,
                "compound": (st.get("compound") or "UNKNOWN").upper() if st else "UNKNOWN",
                "tyre_life": int(tyre_life) if st else None,
                "stint": st.get("stint_number") if st else None,
                "position": race_pos.get((num, n)),
                "pit_in": (num, n) in pit_laps,
                "pit_out": bool(l.get("is_pit_out_lap")),
                "session_time": round(end - t_session, 3) if end else None,
                "track_status": lap_status(windows, start, end) if start and end else "",
                "is_accurate": True,
                "deleted": None,
                "has_telemetry": valid,
            })

        with open(session_dir / "laps" / f"{code}.json", "w") as f:
            json.dump(out, f, separators=(",", ":"))

    # drivers.json last: process_session treats drivers.json + telemetry as "cached".
    corners = corner_list(circuit, ref_tel)
    if corners:
        with open(session_dir / "corners.json", "w") as f:
            json.dump(corners, f, separators=(",", ":"))

    results_out = []
    for i, r in enumerate(results):
        # OpenF1 leaves unclassified finishers without a position; FastF1 numbers them
        # on after the classified cars, which is the order sort_results() produces.
        r = dict(r, position=r.get("position") or i + 1)
        code = abbr.get(r["driver_number"])
        if not code:
            continue
        status = ("Did not start" if r.get("dns") else "Disqualified" if r.get("dsq")
                  else "Retired" if r.get("dnf") else "Finished")
        grid = None
        if race:
            ts, vs = pos_series.get(r["driver_number"], ([], []))
            grid = vs[0] if vs and ts[0] <= t_session else None  # pre-start order = grid
        results_out.append({
            "driver": code,
            "position": r.get("position"),
            "grid": grid,
            "status": status,
            "points": r.get("points"),
            "classified": ("W" if r.get("dns") else "D" if r.get("dsq") else "R" if r.get("dnf")
                           else str(r.get("position") or "")),
        })
    # `source` lets build_start_lines.py skip OpenF1-built sessions; the frontend ignores it.
    analysis = {"version": 1, "source": "openf1", "laps": analysis_laps, "results": results_out}
    if line_shifts:
        print(f"  line shifts (s): median {fallback_shift:.3f}, range {min(line_shifts.values()):.3f}.."
              f"{max(line_shifts.values()):.3f}, {len(car_by) - len(line_shifts)} driver(s) on the median")
    payload = json.dumps(analysis, separators=(",", ":"), allow_nan=False)
    (session_dir / "analysis.json").write_text(payload)

    if weather_raw:
        w = sorted(weather_raw, key=lambda r: r["date"])
        num = lambda k, nd=1: [None if r.get(k) is None else round(float(r[k]), nd) for r in w]  # noqa: E731
        weather = {
            "time": [round(parse_ts(r["date"]) - t_session, 1) for r in w],
            "air_temp": num("air_temperature"), "track_temp": num("track_temperature"),
            "humidity": num("humidity"), "pressure": num("pressure"), "wind_speed": num("wind_speed"),
            "wind_direction": [None if r.get("wind_direction") is None else int(r["wind_direction"]) for r in w],
            "rainfall": [None if r.get("rainfall") is None else int(bool(r["rainfall"])) for r in w],
        }
        (session_dir / "weather.json").write_text(json.dumps(weather, separators=(",", ":"), allow_nan=False))

    with open(session_dir / "drivers.json", "w") as f:
        json.dump(drivers, f, separators=(",", ":"))

    print(f"  drivers.json ({len(drivers)}), corners ({len(corners)}), laps ({len(laps_by_driver)} drivers), "
          f"telemetry ({tel_saved} laps), analysis ({len(analysis_laps)} laps), weather ({len(weather_raw)})")
    return tel_saved > 0


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(1)
    ok = process(int(sys.argv[1]), sys.argv[2], sys.argv[3])
    sys.exit(0 if ok else 2)

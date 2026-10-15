#!/usr/bin/env python3
"""
Process a FastF1 session and write:
  drivers.json
  laps/{DRIVER}.json
  telemetry/{DRIVER}_{LAP}.json   (pre-normalized, 500 pts)
  corners.json
  analysis.json                   (every lap: timing, tyres, position, status; results)
  weather.json                    (session weather samples, if any)

Usage:
    python scripts/process_session.py <year> "<event name>" <session_type>

Environment:
    F1_DATA_OUT       output root (default: repo root, i.e. parent of scripts/)
    FASTF1_CACHE_DIR  FastF1 cache dir (default: /tmp/fastf1_cache)
"""

import fastf1
import json
import numpy as np
import os
import re
import sys
import pandas as pd
from pathlib import Path

NUM_POINTS = 500
ANALYSIS_VERSION = 1


def data_root() -> Path:
    """Output root: $F1_DATA_OUT if set, else the repo root."""
    env = os.environ.get("F1_DATA_OUT")
    return Path(env) if env else Path(__file__).parent.parent


def enable_cache() -> None:
    """Enable the FastF1 cache at $FASTF1_CACHE_DIR (default /tmp/fastf1_cache)."""
    cache_dir = Path(os.environ.get("FASTF1_CACHE_DIR") or "/tmp/fastf1_cache")
    cache_dir.mkdir(parents=True, exist_ok=True)
    fastf1.Cache.enable_cache(str(cache_dir))


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def fmt_lap(seconds) -> str:
    if seconds is None or pd.isna(seconds) or seconds <= 0:
        return "N/A"
    mins = int(seconds // 60)
    secs = seconds % 60
    return f"{mins}:{secs:06.3f}"


def _interp(dist, vals, common_dist):
    return np.interp(common_dist, dist, vals.astype(float))


def _nearest_before(dist, vals, common_dist):
    idx = np.clip(np.searchsorted(dist, common_dist, side="right") - 1, 0, len(dist) - 1)
    return vals[idx]


def _driver_ahead(dist, tel, common_dist) -> list:
    """DriverAhead driver-number strings, nearest-before sampled ("" if missing)."""
    if "DriverAhead" not in tel.columns:
        return [""] * len(common_dist)
    raw = _nearest_before(dist, tel["DriverAhead"].values, common_dist)
    return ["" if (v is None or pd.isna(v)) else str(v) for v in raw]


def _dist_ahead(dist, tel, common_dist) -> list:
    """DistanceToDriverAhead (m), interpolated over non-NaN source points only;
    null where the nearest-before source sample is NaN."""
    n = len(common_dist)
    if "DistanceToDriverAhead" not in tel.columns:
        return [None] * n
    src = pd.to_numeric(tel["DistanceToDriverAhead"], errors="coerce").values.astype(float)
    ok = ~np.isnan(src)
    if not ok.any():
        return [None] * n
    vals = np.round(np.interp(common_dist, dist[ok], src[ok]), 1)
    nan_at = np.isnan(_nearest_before(dist, src, common_dist))
    return [None if m else float(v) for v, m in zip(vals, nan_at)]


# ── analysis.json / weather.json (shared with backfill_analysis.py) ──────────

def _secs(td, nd=3):
    """Timedelta -> seconds rounded to nd, or None."""
    if td is None or pd.isna(td):
        return None
    return round(td.total_seconds(), nd)


def _num(v, nd=None):
    """Number -> float rounded to nd (int when nd == 0), or None for NaN/missing."""
    if v is None or pd.isna(v):
        return None
    v = float(v)
    if nd is None:
        return v
    return int(round(v)) if nd == 0 else round(v, nd)


def _int(v):
    return None if v is None or pd.isna(v) else int(v)


def _str(v):
    return "" if v is None or pd.isna(v) else str(v)


def _bool(v):
    return None if v is None or pd.isna(v) else bool(v)


def build_analysis(session, has_telemetry) -> dict:
    """Every lap of every driver plus classification.

    has_telemetry(abbr, lap_number) -> bool says whether telemetry/<abbr>_<lap>.json exists.
    """
    laps = []
    has_deleted = "Deleted" in session.laps.columns
    for _, lap in session.laps.iterrows():
        drv = _str(lap.get("Driver"))
        lap_num = _int(lap.get("LapNumber"))
        compound = lap.get("Compound")
        laps.append({
            "driver": drv,
            "lap_number": lap_num,
            "lap_time": _secs(lap.get("LapTime")),
            "s1": _secs(lap.get("Sector1Time")),
            "s2": _secs(lap.get("Sector2Time")),
            "s3": _secs(lap.get("Sector3Time")),
            "compound": None if compound is None or pd.isna(compound) else str(compound),
            "tyre_life": _int(lap.get("TyreLife")),
            "stint": _int(lap.get("Stint")),
            "position": _int(lap.get("Position")),
            "pit_in": bool(pd.notna(lap.get("PitInTime"))),
            "pit_out": bool(pd.notna(lap.get("PitOutTime"))),
            "session_time": _secs(lap.get("Time")),
            "track_status": _str(lap.get("TrackStatus")),
            "is_accurate": bool(_bool(lap.get("IsAccurate"))),
            "deleted": _bool(lap.get("Deleted")) if has_deleted else None,
            "has_telemetry": bool(lap_num is not None and has_telemetry(drv, lap_num)),
        })

    results = []
    for _, row in session.results.iterrows():
        results.append({
            "driver": _str(row.get("Abbreviation")),
            "position": _int(row.get("Position")),
            "grid": _int(row.get("GridPosition")),
            "status": _str(row.get("Status")),
            "points": _num(row.get("Points")),
            "classified": _str(row.get("ClassifiedPosition")),
        })

    return {"version": ANALYSIS_VERSION, "laps": laps, "results": results}


def build_weather(session):
    """Column arrays of session weather samples, or None if there are none."""
    try:
        w = session.weather_data
    except Exception:
        return None
    if w is None or w.empty:
        return None

    def col(name, nd):
        if name not in w.columns:
            return [None] * len(w)
        return [_num(v, nd) for v in w[name].values]

    rain = [None if pd.isna(v) else int(bool(v)) for v in w["Rainfall"].values] \
        if "Rainfall" in w.columns else [None] * len(w)
    return {
        "time": [_secs(t, 1) for t in w["Time"]],
        "air_temp": col("AirTemp", 1),
        "track_temp": col("TrackTemp", 1),
        "humidity": col("Humidity", 1),
        "pressure": col("Pressure", 1),
        "wind_speed": col("WindSpeed", 1),
        "wind_direction": col("WindDirection", 0),
        "rainfall": rain,
    }


def _write_json(path: Path, obj) -> None:
    """Strict (no NaN), compact JSON; fully serialized before the file is opened."""
    text = json.dumps(obj, separators=(",", ":"), allow_nan=False)
    with open(path, "w") as f:
        f.write(text)


def write_analysis(session, session_dir: Path, has_telemetry) -> None:
    data = build_analysis(session, has_telemetry)
    _write_json(session_dir / "analysis.json", data)
    print(f"  analysis.json ({len(data['laps'])} laps, {len(data['results'])} results)")


def write_weather(session, session_dir: Path) -> bool:
    data = build_weather(session)
    if data is None:
        print("  weather.json skipped (no weather data)")
        return False
    _write_json(session_dir / "weather.json", data)
    print(f"  weather.json ({len(data['time'])} samples)")
    return True


def process_session(year: int, event_name: str, session_type: str):
    root = data_root()
    enable_cache()

    print(f"Loading {year} {event_name} {session_type} ...")
    session = fastf1.get_session(year, event_name, session_type)
    session.load(telemetry=True, laps=True, weather=True)
    print("Session loaded.")

    event_slug = slugify(event_name)
    session_dir = root / str(year) / event_slug / session_type
    session_dir.mkdir(parents=True, exist_ok=True)

    # ── drivers.json ──────────────────────────────────────────────────────────
    results = session.results
    drivers = []
    for _, row in results.iterrows():
        team_color = str(row.get("TeamColor", "FFFFFF"))
        if team_color and not team_color.startswith("#"):
            team_color = f"#{team_color}"
        drivers.append({
            "abbreviation": str(row["Abbreviation"]),
            "full_name": str(row["FullName"]),
            "number": str(int(row["DriverNumber"])) if pd.notna(row.get("DriverNumber")) else "",
            "team": str(row.get("TeamName", "")),
            "team_color": team_color,
        })

    with open(session_dir / "drivers.json", "w") as f:
        json.dump(drivers, f, separators=(",", ":"))
    print(f"  drivers.json ({len(drivers)} drivers)")

    # ── corners.json ──────────────────────────────────────────────────────────
    try:
        ci = session.get_circuit_info()
        corners = []
        for _, row in ci.corners.iterrows():
            corners.append({
                "number": int(row.get("Number", 0)),
                "letter": str(row.get("Letter", "")),
                "distance": float(row.get("Distance", 0)),
                "angle": float(row.get("Angle", 0)) if "Angle" in row else 0,
                "x": float(row.get("X", 0)) * 0.1 if "X" in row else 0,
                "y": float(row.get("Y", 0)) * 0.1 if "Y" in row else 0,
            })
        with open(session_dir / "corners.json", "w") as f:
            json.dump(corners, f, separators=(",", ":"))
        print(f"  corners.json ({len(corners)} corners)")
    except Exception as e:
        print(f"  Warning: corners: {e}")

    # ── laps/{DRIVER}.json + telemetry/{DRIVER}_{LAP}.json ────────────────────
    laps_dir = session_dir / "laps"
    laps_dir.mkdir(exist_ok=True)
    tel_dir = session_dir / "telemetry"
    tel_dir.mkdir(exist_ok=True)

    generated_tel = set()  # (abbr, lap_num) pairs with telemetry files

    laps_saved = 0
    tel_saved = 0

    for driver_info in drivers:
        abbr = driver_info["abbreviation"]
        try:
            driver_laps = session.laps.pick_driver(abbr)
            if driver_laps.empty:
                continue

            valid = driver_laps.pick_quicklaps()
            fastest_time = None
            if not valid.empty:
                fl = valid.pick_fastest()
                if fl is not None:
                    lt = fl["LapTime"]
                    fastest_time = lt.total_seconds() if pd.notna(lt) else None

            # ── telemetry files ──────────────────────────────────────────────
            quick_laps = driver_laps.pick_quicklaps()
            for _, lap in quick_laps.iterrows():
                lap_num = int(lap["LapNumber"]) if pd.notna(lap.get("LapNumber")) else None
                if lap_num is None:
                    continue
                try:
                    tel = lap.get_telemetry().add_distance()
                    if tel is None or tel.empty or len(tel) < 50:
                        continue

                    dist = tel["Distance"].values.astype(float)
                    max_dist = float(dist.max())
                    if max_dist <= 0:
                        continue

                    common_dist = np.linspace(0, max_dist, NUM_POINTS)

                    has_time = "Time" in tel.columns
                    time_s = tel["Time"].dt.total_seconds().values if has_time else np.zeros(len(tel))

                    data = {
                        "distance": np.round(common_dist, 1).tolist(),
                        "speed": np.round(_interp(dist, tel["Speed"].values, common_dist), 1).tolist(),
                        "throttle": np.round(_interp(dist, tel["Throttle"].values, common_dist), 1).tolist(),
                        "rpm": np.round(_interp(dist, tel["RPM"].values, common_dist), 0).astype(int).tolist(),
                        "gear": _nearest_before(dist, tel["nGear"].values if "nGear" in tel.columns else np.zeros(len(tel), dtype=int), common_dist).tolist(),
                        "brake": _nearest_before(dist, tel["Brake"].values.astype(int) if "Brake" in tel.columns else np.zeros(len(tel), dtype=int), common_dist).tolist(),
                        "drs": _nearest_before(dist, tel["DRS"].values.astype(int) if "DRS" in tel.columns else np.zeros(len(tel), dtype=int), common_dist).tolist(),
                        "time": np.round(_interp(dist, time_s, common_dist), 4).tolist(),
                        "x": np.round(_interp(dist, tel["X"].values, common_dist) * 0.1, 1).tolist() if "X" in tel.columns else [],
                        "y": np.round(_interp(dist, tel["Y"].values, common_dist) * 0.1, 1).tolist() if "Y" in tel.columns else [],
                        "z": np.round(_interp(dist, tel["Z"].values.astype(float) * 0.1, common_dist), 2).tolist() if "Z" in tel.columns else [],
                        "driver_ahead": _driver_ahead(dist, tel, common_dist),
                        "dist_ahead": _dist_ahead(dist, tel, common_dist),
                    }

                    with open(tel_dir / f"{abbr}_{lap_num}.json", "w") as f:
                        json.dump(data, f, separators=(",", ":"))

                    generated_tel.add((abbr, lap_num))
                    tel_saved += 1

                except Exception as e:
                    print(f"    Warning: tel {abbr} lap {lap_num}: {e}")

            # ── laps file ────────────────────────────────────────────────────
            out = []
            for _, lap in driver_laps.iterrows():
                lap_num = int(lap["LapNumber"]) if pd.notna(lap.get("LapNumber")) else 0
                lap_time = lap.get("LapTime")
                lt_sec = lap_time.total_seconds() if pd.notna(lap_time) else None

                s1 = lap.get("Sector1Time")
                s2 = lap.get("Sector2Time")
                s3 = lap.get("Sector3Time")

                is_fastest = (
                    lt_sec is not None
                    and fastest_time is not None
                    and abs(lt_sec - fastest_time) < 0.001
                )

                out.append({
                    "lap_number": lap_num,
                    "lap_time": fmt_lap(lt_sec),
                    "lap_time_seconds": round(lt_sec, 3) if lt_sec else None,
                    "sector1": fmt_lap(s1.total_seconds()) if pd.notna(s1) else "N/A",
                    "sector2": fmt_lap(s2.total_seconds()) if pd.notna(s2) else "N/A",
                    "sector3": fmt_lap(s3.total_seconds()) if pd.notna(s3) else "N/A",
                    "compound": str(lap.get("Compound", "UNKNOWN")),
                    "tyre_life": int(lap.get("TyreLife")) if pd.notna(lap.get("TyreLife")) else 0,
                    "is_personal_best": bool(lap.get("IsPersonalBest", False)),
                    "is_fastest": is_fastest,
                    "is_valid": (abbr, lap_num) in generated_tel,
                })

            with open(laps_dir / f"{abbr}.json", "w") as f:
                json.dump(out, f, separators=(",", ":"))
            laps_saved += 1

        except Exception as e:
            print(f"  Warning: {abbr}: {e}")

    print(f"  laps/ ({laps_saved} drivers)")
    print(f"  telemetry/ ({tel_saved} lap files)")

    # ── analysis.json + weather.json ─────────────────────────────────────────
    try:
        write_analysis(session, session_dir, lambda a, n: (a, n) in generated_tel)
    except Exception as e:
        print(f"  Warning: analysis: {e}")
    try:
        write_weather(session, session_dir)
    except Exception as e:
        print(f"  Warning: weather: {e}")
    print(f"\nDone -> {year}/{event_slug}/{session_type}/")


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("Usage: python process_session.py <year> <event_name> <session_type>")
        sys.exit(1)
    process_session(int(sys.argv[1]), sys.argv[2], sys.argv[3])

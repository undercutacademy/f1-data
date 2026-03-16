#!/usr/bin/env python3
"""
Process a FastF1 session and write drivers.json + laps/{DRIVER}.json.

Usage:
    python scripts/process_session.py <year> "<event name>" <session_type>

Examples:
    python scripts/process_session.py 2026 "Australian Grand Prix" R
    python scripts/process_session.py 2026 "Australian Grand Prix" Q
"""

import fastf1
import json
import os
import re
import sys
import pandas as pd
from pathlib import Path


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def fmt_lap(seconds) -> str:
    if seconds is None or pd.isna(seconds) or seconds <= 0:
        return "N/A"
    mins = int(seconds // 60)
    secs = seconds % 60
    return f"{mins}:{secs:06.3f}"


def process_session(year: int, event_name: str, session_type: str):
    root = Path(__file__).parent.parent

    cache_dir = Path("/tmp/fastf1_cache")
    cache_dir.mkdir(exist_ok=True)
    fastf1.Cache.enable_cache(str(cache_dir))

    print(f"Loading {year} {event_name} {session_type} ...")
    session = fastf1.get_session(year, event_name, session_type)
    session.load(telemetry=False, laps=True, weather=False)
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

    # ── laps/{DRIVER}.json ────────────────────────────────────────────────────
    laps_dir = session_dir / "laps"
    laps_dir.mkdir(exist_ok=True)

    saved = 0
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

            out = []
            for _, lap in driver_laps.iterrows():
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
                has_telemetry = (
                    lt_sec is not None
                    and lt_sec > 60
                    and str(lap.get("TrackStatus", "1")) in ("1", "2", "4", "6", "")
                )

                out.append({
                    "lap_number": int(lap["LapNumber"]) if pd.notna(lap.get("LapNumber")) else 0,
                    "lap_time": fmt_lap(lt_sec),
                    "lap_time_seconds": round(lt_sec, 3) if lt_sec else None,
                    "sector1": fmt_lap(s1.total_seconds()) if pd.notna(s1) else "N/A",
                    "sector2": fmt_lap(s2.total_seconds()) if pd.notna(s2) else "N/A",
                    "sector3": fmt_lap(s3.total_seconds()) if pd.notna(s3) else "N/A",
                    "compound": str(lap.get("Compound", "UNKNOWN")),
                    "tyre_life": int(lap.get("TyreLife")) if pd.notna(lap.get("TyreLife")) else 0,
                    "is_personal_best": bool(lap.get("IsPersonalBest", False)),
                    "is_fastest": is_fastest,
                    "is_valid": has_telemetry,
                })

            with open(laps_dir / f"{abbr}.json", "w") as f:
                json.dump(out, f, separators=(",", ":"))
            saved += 1
        except Exception as e:
            print(f"  Warning: {abbr}: {e}")

    print(f"  laps/ ({saved} drivers)")
    print(f"\nDone -> {year}/{event_slug}/{session_type}/")


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("Usage: python process_session.py <year> <event_name> <session_type>")
        sys.exit(1)
    process_session(int(sys.argv[1]), sys.argv[2], sys.argv[3])

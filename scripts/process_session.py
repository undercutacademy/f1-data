#!/usr/bin/env python3
"""
Process a FastF1 session and write:
  drivers.json
  laps/{DRIVER}.json
  telemetry/{DRIVER}_{LAP}.json   (pre-normalized, 500 pts)
  corners.json

Usage:
    python scripts/process_session.py <year> "<event name>" <session_type>
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


def process_session(year: int, event_name: str, session_type: str):
    root = Path(__file__).parent.parent

    cache_dir = Path("/tmp/fastf1_cache")
    cache_dir.mkdir(exist_ok=True)
    fastf1.Cache.enable_cache(str(cache_dir))

    print(f"Loading {year} {event_name} {session_type} ...")
    session = fastf1.get_session(year, event_name, session_type)
    session.load(telemetry=True, laps=True, weather=False)
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
    print(f"\nDone -> {year}/{event_slug}/{session_type}/")


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("Usage: python process_session.py <year> <event_name> <session_type>")
        sys.exit(1)
    process_session(int(sys.argv[1]), sys.argv[2], sys.argv[3])

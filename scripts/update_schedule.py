#!/usr/bin/env python3
"""
Rebuild index.json, {year}/events.json, and {year}/{slug}/sessions.json.

Usage:
    python scripts/update_schedule.py          # all years 2018-current
    python scripts/update_schedule.py 2026     # single year
"""

import fastf1
import json
import re
import sys
import pandas as pd
from datetime import datetime
from pathlib import Path


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


_SESSION_NAMES = {
    "FP1": "Practice 1",
    "FP2": "Practice 2",
    "FP3": "Practice 3",
    "Q": "Qualifying",
    "SQ": "Sprint Qualifying",
    "S": "Sprint",
    "R": "Race",
}
_SESSION_NAME_ALIASES = {
    "Sprint Qualifying": ["Sprint Shootout"],
    "Sprint Shootout": ["Sprint Qualifying"],
}
_CONVENTIONAL_SESSIONS = ["R", "Q", "FP3", "FP2", "FP1"]
_SPRINT_SESSIONS = ["R", "S", "SQ", "Q", "FP1"]


def update_year(year: int, root: Path):
    cache_dir = Path("/tmp/fastf1_cache")
    cache_dir.mkdir(exist_ok=True)
    fastf1.Cache.enable_cache(str(cache_dir))

    print(f"Updating {year}...")
    schedule = fastf1.get_event_schedule(year, include_testing=False)
    today = pd.Timestamp.now(tz="UTC")

    events_out = []
    for _, row in schedule.iterrows():
        first_session_date = row.get("Session1Date")
        if pd.isna(first_session_date):
            first_session_date = row.get("EventDate")
        if pd.notna(first_session_date):
            if hasattr(first_session_date, "tzinfo") and first_session_date.tzinfo is None:
                first_session_date = first_session_date.tz_localize("UTC")
            if first_session_date > today:
                continue

        event_name = str(row["EventName"])
        event_slug = slugify(event_name)
        fmt = str(row.get("EventFormat", "conventional")).lower()
        session_codes = _SPRINT_SESSIONS if "sprint" in fmt else _CONVENTIONAL_SESSIONS

        # Build session date map
        session_date_map = {}
        for i in range(1, 6):
            sname = str(row.get(f"Session{i}", ""))
            sdate = row.get(f"Session{i}Date")
            if sname:
                session_date_map[sname] = sdate if pd.notna(sdate) else None

        sessions_out = []
        for s in session_codes:
            full_name = _SESSION_NAMES.get(s, s)
            sdate = session_date_map.get(full_name) or session_date_map.get(s)
            if sdate is None:
                for alias in _SESSION_NAME_ALIASES.get(full_name, []):
                    sdate = session_date_map.get(alias)
                    if sdate is not None:
                        break
            session_date_str = ""
            if sdate is not None:
                if hasattr(sdate, "tzinfo") and sdate.tzinfo is None:
                    sdate = sdate.tz_localize("UTC")
                session_date_str = str(sdate)[:16]

            # `available` reflects whether processed data is on disk so the
            # frontend only surfaces sessions a user can actually open.
            drivers_file = root / str(year) / event_slug / s / "drivers.json"
            available = False
            if drivers_file.exists():
                try:
                    with open(drivers_file) as df:
                        available = len(json.load(df)) > 0
                except (ValueError, OSError):
                    available = False

            sessions_out.append({
                "type": s,
                "name": full_name,
                "date": session_date_str,
                "available": available,
            })

        event_dir = root / str(year) / event_slug
        event_dir.mkdir(parents=True, exist_ok=True)
        with open(event_dir / "sessions.json", "w") as f:
            json.dump(sessions_out, f, separators=(",", ":"))

        events_out.append({
            "round": int(row["RoundNumber"]),
            "name": event_name,
            "slug": event_slug,
            "country": str(row.get("Country", "")),
            "location": str(row.get("Location", "")),
            "date": str(row["EventDate"])[:10] if pd.notna(row.get("EventDate")) else "",
            "format": fmt,
        })

    # Latest first
    events_out.reverse()

    year_dir = root / str(year)
    year_dir.mkdir(exist_ok=True)
    with open(year_dir / "events.json", "w") as f:
        json.dump(events_out, f, separators=(",", ":"))
    print(f"  {year}/events.json ({len(events_out)} events)")


def update_index(root: Path):
    current_year = datetime.now().year
    years = list(range(current_year, 2017, -1))
    with open(root / "index.json", "w") as f:
        json.dump(years, f, separators=(",", ":"))
    print(f"index.json -> {years[0]} to {years[-1]}")


if __name__ == "__main__":
    root = Path(__file__).parent.parent
    years = [int(sys.argv[1])] if len(sys.argv) > 1 else list(range(2018, datetime.now().year + 1))
    update_index(root)
    for y in years:
        update_year(y, root)
    print("\nDone.")

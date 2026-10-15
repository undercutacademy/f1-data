#!/usr/bin/env python3
"""
Record each circuit's start/finish line position from FastF1-built telemetry.

FastF1 laps begin exactly on the timing line, so the first x/y of its lap
telemetry is the line. OpenF1's lap timestamps are offset by a per-driver
constant (0.1–1.8 s measured in 2026), and openf1_session.py snaps laps back onto
this line. Run on a machine with the full data checkout; commits a tiny JSON.

Usage:
    python scripts/build_start_lines.py            # writes scripts/start_lines.json
"""
import json
import os
import statistics
import sys
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(os.environ.get("F1_DATA_ROOT") or Path(__file__).resolve().parent.parent)
OUT = Path(os.environ.get("START_LINES_OUT") or Path(__file__).resolve().parent / "start_lines.json")
SAMPLES_PER_SESSION = 12


def session_line(session_dir: Path):
    """Median first point of a session's lap telemetry (only FastF1-built sessions:
    those without OpenF1's race-only car-ahead keys or built before it existed)."""
    if (session_dir / "analysis.json").exists():
        try:
            if json.loads((session_dir / "analysis.json").read_text()).get("source") == "openf1":
                return None
        except ValueError:
            return None
    files = sorted((session_dir / "telemetry").glob("*.json"))[:SAMPLES_PER_SESSION]
    xs, ys = [], []
    for f in files:
        try:
            t = json.loads(f.read_text())
        except ValueError:
            continue
        if t.get("x") and t.get("y"):
            xs.append(t["x"][0])
            ys.append(t["y"][0])
    if len(xs) < 5:
        return None
    return statistics.median(xs), statistics.median(ys), len(xs)


def main():
    lines = json.loads(OUT.read_text()) if OUT.exists() else {}
    years = sorted((int(p.name) for p in ROOT.iterdir() if p.is_dir() and p.name.isdigit()), reverse=True)
    for year in years:  # newest first: layouts and coordinate frames change over time
        for event_dir in sorted((ROOT / str(year)).iterdir()):
            if not event_dir.is_dir() or event_dir.name in lines:
                continue
            for stype in ("R", "Q", "FP1", "FP2", "FP3", "S", "SQ"):
                line = session_line(event_dir / stype)
                if line:
                    x, y, n = line
                    lines[event_dir.name] = {"x": round(x, 1), "y": round(y, 1),
                                             "source": f"{year}/{event_dir.name}/{stype}", "laps": n}
                    break
    OUT.write_text(json.dumps(dict(sorted(lines.items())), indent=1) + "\n")
    print(f"{len(lines)} circuits -> {OUT}")


if __name__ == "__main__":
    main()

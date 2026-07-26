#!/usr/bin/env python3
"""
One-command local ingester: fetch new sessions, validate, commit, push, purge.

Designed to run from a home IP since livetiming.formula1.com blocks
datacenter/GitHub-Actions IPs (2026-07-17). Safe to run on a schedule:
exits fast when there is nothing to do, takes a lock so concurrent runs
are impossible, and never rebases/stashes over a working tree.

Usage:
    python scripts/local_ingest.py               # wait up to 90 min for fresh data
    python scripts/local_ingest.py --max-wait 0  # process only what's ready now
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / ".ingest.lock"
LOCK_STALE_HOURS = 8
RECENT_SESSION_HOURS = 6   # only wait-poll for sessions that started this recently
POLL_INTERVAL = 60         # seconds between availability probes


def log(msg: str) -> None:
    line = f"[{datetime.now(timezone.utc):%H:%M:%S}] {msg}"
    print(line, flush=True)
    try:
        with open(ROOT / "ingest.log", "a", encoding="utf-8") as f:
            f.write(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} {msg}\n")
    except OSError:
        pass


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=check
    )


# ---------------------------------------------------------------- lock

def acquire_lock() -> bool:
    try:
        fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        age_h = (time.time() - LOCK.stat().st_mtime) / 3600
        if age_h > LOCK_STALE_HOURS:
            log(f"Stealing stale lock ({age_h:.1f}h old)")
            LOCK.unlink(missing_ok=True)
            return acquire_lock()
        log("Another ingest is running (lock present) - exiting.")
        return False


# ---------------------------------------------------------------- pending work

def parse_date(date_str: str):
    try:
        dt = datetime.fromisoformat(date_str.replace(" ", "T"))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except ValueError:
        return None


def session_cached(year: int, slug: str, stype: str) -> bool:
    d = ROOT / str(year) / slug / stype
    tel = d / "telemetry"
    if not (d / "drivers.json").exists():
        return False
    return tel.is_dir() and any(tel.glob("*.json"))


def pending_sessions():
    """(date, year, event_name, session_type) for past, uncached sessions <14d old."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=14)
    out = []
    for year in (now.year, now.year - 1):
        events_file = ROOT / str(year) / "events.json"
        if not events_file.exists():
            continue
        events = json.loads(events_file.read_text(encoding="utf-8"))
        for ev in events:
            sf = ROOT / str(year) / ev["slug"] / "sessions.json"
            if not sf.exists():
                continue
            for s in json.loads(sf.read_text(encoding="utf-8")):
                dt = parse_date(s.get("date", ""))
                if not dt or dt > now or dt < cutoff:
                    continue
                if not session_cached(year, ev["slug"], s["type"]):
                    out.append((dt, year, ev["name"], s["type"]))
    return sorted(out, reverse=True)


def data_available(year: int, event: str, stype: str) -> bool:
    """TI-style probe: light laps-only load with the cache disabled, so a
    'not yet' answer is never cached."""
    import fastf1
    try:
        with fastf1.Cache.disabled():
            s = fastf1.get_session(year, event, stype)
            s.load(telemetry=False, weather=False, messages=False)
        return not s.laps.empty and s.laps["Driver"].dropna().nunique() > 0
    except Exception as e:
        log(f"Probe: data not available yet ({str(e)[:80]})")
        return False


# ---------------------------------------------------------------- steps

def run_auto_process() -> bool:
    log("Running auto_process...")
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "auto_process.py")], cwd=ROOT
    )
    return r.returncode == 0


def changed_json_files():
    out = git("status", "--porcelain").stdout
    files = []
    for line in out.splitlines():
        p = line[3:].strip().strip('"')
        if p.endswith("/"):
            files += [str(f.relative_to(ROOT)) for f in (ROOT / p).rglob("*.json")]
        elif p.endswith(".json"):
            files.append(p)
    return files


def validate(files) -> bool:
    bad = []
    for p in files:
        try:
            json.loads((ROOT / p).read_text(encoding="utf-8"))
        except Exception as e:
            bad.append((p, str(e)[:60]))
    for p, e in bad:
        log(f"INVALID JSON: {p} - {e}")
    return not bad


def purge_jsdelivr(paths) -> None:
    import urllib.request
    batch = 100
    ok = 0
    for i in range(0, len(paths), batch):
        body = json.dumps({
            "path": [f"/gh/undercutacademy/f1-data@master/{p}" for p in paths[i:i + batch]]
        }).encode()
        req = urllib.request.Request(
            "https://purge.jsdelivr.net/", data=body,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                if resp.status in (200, 202):
                    ok += 1
        except Exception as e:
            log(f"Purge batch failed (non-fatal): {str(e)[:60]}")
    log(f"Purged jsDelivr: {ok}/{(len(paths) + batch - 1) // batch} batches accepted")


def commit_push_purge() -> bool:
    files = changed_json_files()
    if not files and not git("status", "--porcelain").stdout.strip():
        log("Nothing new to commit.")
        return True
    if not validate(files):
        log("ABORTING: invalid JSON in working tree - not committing.")
        return False

    old_remote = git("rev-parse", "origin/master").stdout.strip()
    git("add", "-A")
    msg = f"auto: {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC (local ingest)"
    git("commit", "-m", f"{msg}\n\nCo-Authored-By: local_ingest.py <noreply>")

    for attempt in range(5):
        git("fetch", check=False)
        behind = git("rev-list", "--count", "HEAD..origin/master").stdout.strip()
        if behind != "0":
            log(f"Remote moved ({behind} commits) - merging (never rebase here).")
            m = git("merge", "origin/master", "-m", "Merge remote changes (local ingest)",
                    check=False)
            if m.returncode != 0:
                log(f"MERGE FAILED: {m.stderr[:200]} - resolve manually.")
                return False
        p = git("push", check=False)
        if p.returncode == 0:
            break
        log(f"Push attempt {attempt + 1} failed, retrying in 10s...")
        time.sleep(10)
    else:
        log("Push failed after 5 attempts.")
        return False

    new_head = git("rev-parse", "HEAD").stdout.strip()
    diff = git("diff", "--name-only", old_remote, new_head).stdout.splitlines()
    purge_jsdelivr([p for p in diff if p.endswith(".json")])
    return True


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-wait", type=int, default=90,
                    help="max minutes to wait for a recent session's data (0 = no wait)")
    args = ap.parse_args()

    if not acquire_lock():
        return 0
    try:
        pend = pending_sessions()
        if not pend:
            log("No pending sessions - nothing to do.")
            return 0

        newest = pend[0]
        log(f"Pending: {len(pend)} session(s); newest: {newest[1]} {newest[2]} {newest[3]}")

        started_ago = datetime.now(timezone.utc) - newest[0]
        if args.max_wait > 0 and started_ago < timedelta(hours=RECENT_SESSION_HOURS):
            deadline = time.monotonic() + args.max_wait * 60
            while time.monotonic() < deadline:
                if data_available(newest[1], newest[2], newest[3]):
                    break
                log(f"Waiting for FastF1 data ({args.max_wait} min budget)...")
                time.sleep(POLL_INTERVAL)
            else:
                log("Wait budget exhausted; processing whatever is ready.")

        run_auto_process()  # rc ignored: partial success still worth committing
        return 0 if commit_push_purge() else 1
    finally:
        LOCK.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

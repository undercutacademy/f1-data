#!/usr/bin/env python3
"""
Cloud ingester (GitHub Actions): pull finished sessions from OpenF1, commit, push, purge.

F1's livetiming feed rejects GitHub's IPs, so this uses OpenF1 (openf1_session.py)
instead of FastF1. local_ingest.py on a home PC stays as a delayed fallback.

Designed for a sparse, blob-less checkout of the (very large) data repo: it reads
only the schedule files, asks git (not the disk) whether a session already has
telemetry, and stages new files with `git add --sparse`.

Usage:
    python scripts/cloud_ingest.py                     # everything ready right now
    python scripts/cloud_ingest.py --only 2026 "Dutch Grand Prix" R
    python scripts/cloud_ingest.py --dry-run           # list what it would do
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.dont_write_bytecode = True  # scripts/__pycache__ is tracked in git
sys.path.insert(0, str(Path(__file__).resolve().parent))
import openf1_session as of1  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LOOKBACK_DAYS = 14
# OpenF1's free tier serves a session from 30 min after it ends.
OPENF1_RELEASE = timedelta(minutes=35)
# Testing: push to a throwaway branch instead of master (no CDN purge).
PUSH_BRANCH = os.environ.get("PUSH_BRANCH") or "master"

# Self-chaining (see --next-check): nominal session lengths to estimate when OpenF1
# releases each one, how soon to re-check a session that's due but not out yet
# (overrunning race), and the longest a run may sleep before starting the next
# (the job's own limit is 6 h).
SESSION_HOURS = {"FP1": 1, "FP2": 1, "FP3": 1, "Q": 1, "SQ": 0.75, "S": 1, "R": 2}
RECHECK = timedelta(minutes=10)
MAX_SLEEP = timedelta(hours=5, minutes=30)


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=check)


def in_tree(ref: str, path: str) -> bool:
    # ls-tree reads trees only; `cat-file -e` would lazily download the blob in a
    # blob-less clone, one network round trip per check.
    out = git("ls-tree", "--name-only", ref, "--", path, check=False)
    return out.returncode == 0 and bool(out.stdout.strip())


def has_telemetry(ref: str, session_path: str) -> bool:
    out = git("ls-tree", "--name-only", f"{ref}:{session_path}/telemetry", check=False)
    return out.returncode == 0 and bool(out.stdout.strip())


def session_cached(ref: str, session_path: str) -> bool:
    return in_tree(ref, f"{session_path}/drivers.json") and has_telemetry(ref, session_path)


def parse_utc(s: str):
    try:
        dt = datetime.fromisoformat(s.replace(" ", "T"))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def candidates():
    """(start_utc, year, event_name, slug, type) for recent sessions without telemetry."""
    now = datetime.now(timezone.utc)
    out = []
    for year in sorted({now.year, (now - timedelta(days=LOOKBACK_DAYS)).year}):
        ef = ROOT / str(year) / "events.json"
        if not ef.exists():
            continue
        for ev in json.loads(ef.read_text(encoding="utf-8")):
            sf = ROOT / str(year) / ev["slug"] / "sessions.json"
            if not sf.exists():
                continue
            for s in json.loads(sf.read_text(encoding="utf-8")):
                start = parse_utc(s.get("date", ""))
                if not start or start > now or start < now - timedelta(days=LOOKBACK_DAYS):
                    continue
                path = f"{year}/{ev['slug']}/{s['type']}"
                if not session_cached("HEAD", path):
                    out.append((start, year, ev["name"], ev["slug"], s["type"]))
    return sorted(out, reverse=True)


def next_check_seconds():
    """Seconds until the next run is worth starting, or None to end the chain.

    GitHub's cron is best-effort (a new */15 schedule ran zero times in its first
    hours), so during race weekends each run starts the next one itself, timed
    for when OpenF1 should release the next un-ingested session. Sessions not
    yet started count too, so a chain started by the 6-hourly watchdog carries
    on into the next session."""
    now = datetime.now(timezone.utc)
    waits = []
    for year in sorted({now.year, (now - timedelta(days=1)).year}):
        ef = ROOT / str(year) / "events.json"
        if not ef.exists():
            continue
        for ev in json.loads(ef.read_text(encoding="utf-8")):
            sf = ROOT / str(year) / ev["slug"] / "sessions.json"
            if not sf.exists():
                continue
            for s in json.loads(sf.read_text(encoding="utf-8")):
                start = parse_utc(s.get("date", ""))
                if not start or start < now - timedelta(days=2):
                    continue
                due = start + timedelta(hours=SESSION_HOURS.get(s["type"], 1)) + OPENF1_RELEASE
                if due - now > MAX_SLEEP:
                    continue
                if session_cached("HEAD", f"{year}/{ev['slug']}/{s['type']}"):
                    continue
                waits.append(max(due - now, RECHECK))
    return int(min(waits).total_seconds()) if waits else None


def openf1_ready(year, event, stype, start):
    """The OpenF1 session if its data is released and complete, else None.

    Guards against freezing a half-finished session (the cache check is
    "telemetry exists", so a partial ingest would never be retried): needs the
    release window to have passed and official results to exist; for races, the
    winner's lap count must match the laps OpenF1 has."""
    s = of1.find_session(year, event, stype, start)
    if not s:
        log(f"  {event} {stype}: not in OpenF1 yet")
        return None
    end = datetime.fromisoformat(s["date_end"].replace("Z", "+00:00"))
    if datetime.now(timezone.utc) < end + OPENF1_RELEASE:
        log(f"  {event} {stype}: OpenF1 releases it after {end + OPENF1_RELEASE:%H:%M} UTC")
        return None
    results = of1.get("session_result", session_key=s["session_key"])
    if not results:
        log(f"  {event} {stype}: no official results yet")
        return None
    if stype in of1.RACE_TYPES:
        winner = next((r for r in results if r.get("position") == 1), None)
        if winner and winner.get("number_of_laps"):
            laps = of1.get("laps", session_key=s["session_key"], driver_number=winner["driver_number"])
            if max((l["lap_number"] for l in laps), default=0) < winner["number_of_laps"]:
                log(f"  {event} {stype}: laps incomplete ({len(laps)}/{winner['number_of_laps']})")
                return None
    return s


def mark_available(year, slug, stype):
    """Flip `available` for the processed session (update_schedule.py can't run in
    a sparse checkout: it derives availability from files on disk)."""
    sf = ROOT / str(year) / slug / "sessions.json"
    sessions = json.loads(sf.read_text(encoding="utf-8"))
    for s in sessions:
        if s["type"] == stype:
            s["available"] = True
    sf.write_text(json.dumps(sessions, separators=(",", ":")), encoding="utf-8")


def purge_jsdelivr(paths) -> None:
    for i in range(0, len(paths), 100):
        body = json.dumps({"path": [f"/gh/undercutacademy/f1-data@master/{p}" for p in paths[i:i + 100]]}).encode()
        req = urllib.request.Request("https://purge.jsdelivr.net/", data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=30).read()
        except Exception as e:
            log(f"Purge batch failed (non-fatal): {str(e)[:80]}")
    log(f"Purge requested for {len(paths)} files")


def commit_and_push(done) -> bool:
    """Commit the processed sessions; skip any the fallback ingester pushed meanwhile."""
    git("fetch", "--quiet", "origin", "master")
    keep = []
    for year, event, slug, stype in done:
        path = f"{year}/{slug}/{stype}"
        if session_cached("origin/master", path) and PUSH_BRANCH == "master":
            log(f"  {event} {stype} already pushed by another ingester - dropping ours")
            continue
        keep.append((year, slug, stype, path))
    if not keep:
        return True

    paths = [p for *_, p in keep] + sorted({f"{y}/{s}/sessions.json" for y, s, _, _ in keep})
    git("add", "--sparse", "--", *paths)
    names = ", ".join(f"{y} {s} {t}" for y, s, t, _ in keep)
    git("commit", "-m", f"cloud: {names} (OpenF1)\n\nCo-Authored-By: cloud_ingest.py <noreply>")
    new_files = git("diff", "--name-only", "HEAD~1", "HEAD").stdout.split()

    if PUSH_BRANCH != "master":
        ok = git("push", "--force", "origin", f"HEAD:refs/heads/{PUSH_BRANCH}", check=False).returncode == 0
        log(f"{'Pushed' if ok else 'FAILED to push'} test branch {PUSH_BRANCH}: {names}")
        return ok

    for attempt in range(5):
        r = git("pull", "--rebase", "--quiet", "origin", "master", check=False)
        if r.returncode != 0:
            git("rebase", "--abort", check=False)
            log(f"Rebase failed: {r.stderr.strip()[:200]}")
            return False
        if git("push", "origin", "HEAD:master", check=False).returncode == 0:
            purge_jsdelivr([p for p in new_files if p.endswith(".json")])
            log(f"Pushed: {names}")
            return True
        log(f"Push attempt {attempt + 1} failed, retrying...")
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs=3, metavar=("YEAR", "EVENT", "TYPE"),
                    help="process just this session (ignores the lookback window)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="with --only: rebuild even if the session already has telemetry")
    ap.add_argument("--next-check", action="store_true",
                    help="print seconds until the next run is useful (empty = none soon) and exit")
    args = ap.parse_args()

    if args.next_check:
        wait = next_check_seconds()
        print("" if wait is None else wait)
        return 0

    if args.only:
        year, event, stype = int(args.only[0]), args.only[1], args.only[2]
        slug = of1.slugify(event)
        start = None
        sf = ROOT / str(year) / slug / "sessions.json"
        if sf.exists():
            start = next((parse_utc(s["date"]) for s in json.loads(sf.read_text(encoding="utf-8"))
                          if s["type"] == stype), None)
        if session_cached("HEAD", f"{year}/{slug}/{stype}") and not args.force:
            # Rewriting existing files lets stale CDN mirrors serve old copies.
            log(f"{year} {event} {stype} already has telemetry; use --force to rebuild")
            return 0
        todo = [(start, year, event, slug, stype)]
    else:
        todo = candidates()

    if not todo:
        log("No pending sessions.")
        return 0
    log(f"Pending: {', '.join(f'{e} {t}' for _, _, e, _, t in todo)}")

    done = []
    for start, year, event, slug, stype in todo:
        if not openf1_ready(year, event, stype, start):
            continue
        if args.dry_run:
            log(f"  would process {year} {event} {stype}")
            continue
        log(f"Processing {year} {event} {stype} from OpenF1")
        if of1.process(year, event, stype, start):
            mark_available(year, slug, stype)
            done.append((year, event, slug, stype))
        else:
            log(f"  {event} {stype}: processing produced no telemetry - will retry next run")

    if done and not commit_and_push(done):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

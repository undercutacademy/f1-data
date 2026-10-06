# Future Updates — f1-data

_Ideas, optimizations and planned changes for this project. Lucas dictates these in chat; Claude writes them here._

---

## Backlog

_Nothing captured yet._

- [x] **Weekly automated health check of the F1 page** (added 2026-09-29, live)

  A cloud scheduled task now runs every **Tuesday 08:53 ET** and verifies the
  whole path the SPA actually uses, end to end:

  `index.json` → `<year>/events.json` → `<slug>/sessions.json` →
  `<session>/drivers.json` → `laps/<driver>.json` →
  `telemetry/<ABBR>_<lap>.json` → `corners.json`

  all on `cdn.jsdelivr.net`, falling back to statically.io and githack the
  same way `useTelemetry.ts` does. If jsDelivr alone fails but a mirror
  works, that is reported as a cache problem rather than a data problem.

  It then fetches the **official F1 calendar** from
  `api.jolpi.ca/ergast/f1/<year>/races/` and compares the newest ingested
  round against the most recent race that has actually happened. A 36-hour
  grace window covers the normal FastF1 publishing delay; past that, missing
  data means the local ingest has stalled and the task says so.

  This matters because `events.json` only lists **ingested** events, not the
  season — so the file having 15 entries against a 23-race calendar is normal
  mid-season and must not be read as a failure.

  The task is **cloud-only on purpose**: it needs no access to the laptop, so
  unlike the repo scan it cannot be silently skipped because the machine was
  asleep. It only sends a notification when something is broken, and it logs
  every run to the Notion Weekly Scan Log.

---

## Done

_Move items here once shipped._

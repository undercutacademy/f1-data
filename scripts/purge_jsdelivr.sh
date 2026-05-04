#!/usr/bin/env bash
# Purge jsDelivr's CDN cache for files changed in the most recent commit so
# the website doesn't serve stale data for up to 12h after a push.
#
# Picks up paths via `git diff --name-only HEAD~1 HEAD`.
set -euo pipefail

REPO="undercutacademy/f1-data"
BRANCH="master"
BATCH_SIZE=100  # jsDelivr accepts ~200 paths/request; keep margin

changed=()
while IFS= read -r line; do
  [ -n "$line" ] && changed+=("$line")
done < <(git diff --name-only HEAD~1 HEAD | grep -E '\.json$' || true)

if [ "${#changed[@]}" -eq 0 ]; then
  echo "No JSON files changed; nothing to purge."
  exit 0
fi

echo "Purging ${#changed[@]} path(s) from jsDelivr..."

i=0
while [ "$i" -lt "${#changed[@]}" ]; do
  batch=("${changed[@]:$i:$BATCH_SIZE}")
  payload=$(printf '"/gh/%s@%s/%s",' "$REPO" "$BRANCH" "${batch[@]}" | sed 's/,$//')
  body="{\"path\":[${payload}]}"
  # Don't fail the workflow if the purge API hiccups; log and continue.
  curl -sS -X POST https://purge.jsdelivr.net/ \
    -H 'Content-Type: application/json' \
    -d "$body" -o /tmp/purge-resp.json -w "  batch %{http_code}\n" || true
  i=$((i + BATCH_SIZE))
done

echo "Done."

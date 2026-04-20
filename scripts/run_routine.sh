#!/bin/bash
# Runs inside the Claude Code Routine.
# Requires "Allow unrestricted branch pushes" enabled in Routine settings.
#
# Fault-tolerant pipeline: each fetcher runs independently. A single failure
# (e.g. transient Withings DNS error) no longer aborts the run — the baker
# always executes so the dashboard stays in sync with whatever data is
# available, and whatever files changed get committed. The script exits
# non-zero only if BOTH fetchers failed (nothing useful happened).

echo "=== Health Dashboard Routine ==="

WITHINGS_OK=0
GARMIN_OK=0

echo "→ Fetching Withings measurements..."
if python scripts/fetch_and_build.py; then
  WITHINGS_OK=1
else
  echo "  [warn] Withings fetch failed — keeping existing docs/data.json"
fi

echo "→ Fetching Garmin workouts..."
if python scripts/fetch_garmin.py; then
  GARMIN_OK=1
else
  echo "  [warn] Garmin fetch failed — check GARMIN_EMAIL / GARMIN_PASSWORD"
fi

echo "→ Baking dashboard data + insights into docs/index.html..."
if ! python scripts/build_dashboard.py; then
  echo "  [warn] Dashboard bake failed — continuing to commit step"
fi

echo "→ Committing data..."
git config user.email "health-routine[bot]@users.noreply.github.com"
git config user.name  "health-routine[bot]"
git add docs/data.json docs/garmin.json docs/index.html 2>/dev/null || true
if git diff --cached --quiet; then
  echo "→ No new data. Nothing to commit."
else
  MSG="chore: update health data"
  [ $WITHINGS_OK -eq 0 ] && MSG="$MSG (withings skipped)"
  [ $GARMIN_OK   -eq 0 ] && MSG="$MSG (garmin skipped)"
  git commit -m "$MSG"
  git push origin HEAD:main
fi

if [ $WITHINGS_OK -eq 0 ] && [ $GARMIN_OK -eq 0 ]; then
  echo "→ Both fetchers failed — exiting 1 so the routine is flagged."
  exit 1
fi

echo "→ Done. (withings=$WITHINGS_OK garmin=$GARMIN_OK)"

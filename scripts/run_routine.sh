#!/bin/bash
# Runs inside the Claude Code Routine.
# Requires "Allow unrestricted branch pushes" enabled in Routine settings.

echo "=== Health Dashboard Routine ==="

FETCH_FAILED=0

echo "→ Fetching Withings measurements..."
if ! python scripts/fetch_and_build.py; then
  echo "  [ERROR] Withings fetch failed — continuing (check Withings credentials/token)"
  FETCH_FAILED=1
fi

echo "→ Fetching Garmin workouts..."
if ! python scripts/fetch_garmin.py; then
  echo "  [ERROR] Garmin fetch failed — continuing (check GARMIN_EMAIL / GARMIN_PASSWORD)"
  FETCH_FAILED=1
fi

echo "→ Committing data..."
git config user.email "health-routine[bot]@users.noreply.github.com"
git config user.name  "health-routine[bot]"
git add docs/data.json docs/garmin.json 2>/dev/null || true
if git diff --cached --quiet; then
  echo "→ No new data. Nothing to commit."
  exit $FETCH_FAILED
fi
git commit -m "chore: update Withings and Garmin data"
git push origin HEAD:main
echo "→ Done."
exit $FETCH_FAILED

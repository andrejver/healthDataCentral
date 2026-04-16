#!/bin/bash
# Runs inside the Claude Code Routine.
# Requires "Allow unrestricted branch pushes" enabled in Routine settings.
set -e

echo "=== Health Dashboard Routine ==="

echo "→ Fetching Withings measurements..."
python scripts/fetch_and_build.py

echo "→ Fetching Garmin workouts..."
python scripts/fetch_garmin.py || echo "  [warn] Garmin fetch failed — skipping (check GARMIN_EMAIL / GARMIN_PASSWORD)"

echo "→ Committing data..."
git config user.email "health-routine[bot]@users.noreply.github.com"
git config user.name  "health-routine[bot]"
git add docs/data.json docs/garmin.json 2>/dev/null || true
if git diff --cached --quiet; then
  echo "→ No new data. Nothing to commit."
  exit 0
fi
git commit -m "chore: update health data"
git push origin HEAD:main
echo "→ Done."

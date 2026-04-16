#!/bin/bash
# Runs inside the Claude Code Routine.
# Requires "Allow unrestricted branch pushes" enabled in Routine settings.
set -e

echo "=== Withings Dashboard Routine ==="

echo "→ Fetching measurements..."
python scripts/fetch_and_build.py

echo "→ Committing data..."
git config user.email "withings-routine[bot]@users.noreply.github.com"
git config user.name  "withings-routine[bot]"
git add docs/data.json
if git diff --cached --quiet; then
  echo "→ No new data. Nothing to commit."
  exit 0
fi
git commit -m "chore: update Withings data"
git push origin HEAD:main
echo "→ Done."

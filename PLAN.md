# Dashboard Redesign Plan

## 1. Stack & Integration

**Stack:** Vanilla HTML/CSS/JS, Chart.js (already present), no build step.
This is a constraint, not a compromise — the site is a static GitHub Pages deploy
from `docs/`. Adding a build pipeline would break the Claude Code Routine's simple
`git commit & push` model.

**Approach: full replacement of `docs/index.html`.**
The existing file is ~400 lines and does not have the structural separation needed
for a multi-theme, multi-source dashboard. It is replaced in-place so the deploy
path, CI workflow, and Azure Static Web Apps config all stay unchanged.

`docs/data.json` format is left **100% intact** — the Withings fetch script needs
no changes.

A second data file, `docs/garmin.json`, is added alongside it. The dashboard
treats each file as optional: if `garmin.json` is absent or fails to load, the
Withings body-composition view renders fully and Garmin sections show a
"Connect Garmin to unlock" placeholder.

---

## 2. Garmin Data — Fetch & Merge Strategy

**Library:** [`garminconnect`](https://github.com/cyberjunky/python-garmin-connect)
(unofficial but stable; used widely in self-hosted health dashboards).

**Auth storage:** Garmin uses SSO cookies rather than OAuth tokens. The session
object serialised to JSON (email + OAuth1/OAuth2 tokens that `garminconnect`
manages internally) is stored as a single blob `garmin/session.json` in the same
Azure Blob Storage container used for the Withings token. Credentials (email +
password) are environment variables in the Routine — they are never committed.

**New script: `scripts/fetch_garmin.py`**

```
1. Load session blob from Azure → restore garminconnect session
2. Call get_activities(0, 200) → last 200 activities (≈ 6 months at 8/week)
3. Write rotated session blob back to Azure
4. For each activity extract:
     date (YYYY-MM-DD), activity_type, duration_min, distance_km,
     calories, avg_hr, max_hr, elevation_gain_m, steps (runs/walks only)
5. Group by date (keep only the most-calorie-intense activity per day)
6. Sort ascending, write docs/garmin.json
```

**`run_routine.sh` change:** calls both fetch scripts; commits `data.json` and
`garmin.json` if either changed.

**Merge in the browser:** the dashboard fetches both JSON files with
`Promise.allSettled`. For every date that appears in both, the row is enriched;
dates with only Withings or only Garmin data render normally. No server-side
merge step is needed.

**Proposed `garmin.json` record shape:**
```json
{
  "date": "2025-03-10",
  "type": "running",
  "duration_min": 47,
  "distance_km": 7.8,
  "calories": 510,
  "avg_hr": 155,
  "max_hr": 174,
  "elevation_m": 84
}
```

---

## 3. Trends & Insights (5 chosen)

These are surfaced as a dedicated "Insights" card strip below the hero stats.

### 3.1 Fat vs. Muscle Split
**What:** Of the total weight change in the selected range, how many kg are fat
and how many are muscle?  
**Why:** The most important body-recomposition signal. Losing weight is only good
if fat is going down faster than muscle. A ratio worse than 3:1 fat:muscle on a
cut is a warning.  
**Visual:** Horizontal stacked bar (fat lost / muscle lost) + sentence summary
("87% of weight lost was fat").

### 3.2 Training Load vs. Weight Response
**What:** Weekly bar chart of total workout minutes overlaid with a weekly average
weight line. Requires both data sources.  
**Why:** Shows whether training is driving results or whether weight moves
independently of activity. Useful for identifying which weeks the user was
consistent and whether the scale reflected it.  
**Visual:** Dual-axis combo chart (bars = weekly minutes, line = avg weight).

### 3.3 Body Composition Trajectory
**What:** Linear regression on the last 30 days of fat_pct. Projects the trend
line 30 days forward and prints "At this rate you'll reach X% body fat by DATE."  
**Why:** Turns a chart into a goal-oriented number. Uses only 30-day window to
avoid stale data distorting the projection.  
**Visual:** Continuation of the fat% line chart with a dashed projection segment.

### 3.4 Resting Heart Rate Trend
**What:** 30-day rolling average of `avg_hr` on low-intensity activities (walks,
easy runs defined as avg_hr < 130 bpm) as a cardiovascular fitness proxy.  
**Why:** Resting/easy-effort HR declining over months is the clearest long-term
fitness signal available from Garmin data without VO2max estimates (which require
a specific watch model). Only shown when Garmin data is present.  
**Visual:** Small sparkline card with direction arrow and "↓ 4 bpm vs. 90 days ago".

### 3.5 Weekly Consistency Score
**What:** Percentage of weeks in the selected range that contained ≥ 3 workout
sessions. Shown as a compact heatmap calendar (GitHub contribution style) where
each cell = one day coloured by activity intensity (none / light / moderate / hard).  
**Why:** Frequency beats intensity for long-term health. The calendar gives an
immediate visual sense of training rhythm and streak length.  
**Visual:** Heatmap calendar strip + "X-week streak" badge.

---

## 4. Theme System

**Mechanism:** CSS custom properties scoped to `[data-theme]` on `<html>`.
JS reads/writes `localStorage.getItem('theme')` and sets the attribute on load.
No flash of wrong theme because the `<html>` attribute is set in a `<script>`
tag in `<head>` before paint.

**4 themes:**

| Name | Palette concept | Accent |
|---|---|---|
| `dark` (default) | Deep charcoal, GitHub-inspired | Electric blue `#4f8ef7` |
| `midnight` | Near-black navy, starfield feel | Violet `#a78bfa` |
| `light` | Off-white, clean clinical | Teal `#0d9488` |
| `amber` | Warm dark, paper/sepia tones | Amber `#f59e0b` |

**Switcher UI:** A small palette icon button in the header opens a 4-dot colour
picker popover (no labels, just swatches — minimal chrome on mobile). Selected
theme gets a ring highlight. Dismissed by clicking outside.

**Chart colours:** Each theme exports a JS colour map (`THEME_COLORS[theme]`)
so Chart.js datasets re-render with matching palette when the theme changes.
Charts are destroyed and recreated on theme switch (same as the existing range
filter behaviour).

---

## Summary of File Changes

| File | Change |
|---|---|
| `docs/index.html` | Full rewrite — mobile-first, themes, Garmin, insights |
| `docs/garmin.json` | New generated file (gitignored until first Routine run) |
| `scripts/fetch_garmin.py` | New script — Garmin fetch & session rotation |
| `scripts/run_routine.sh` | Extended to call `fetch_garmin.py` |
| `requirements.txt` | Add `garminconnect` |
| `README.md` | Add Garmin setup section |
| `docs/data.json` | Unchanged |
| `scripts/fetch_and_build.py` | Unchanged |
| `scripts/auth_setup.py` | Unchanged |

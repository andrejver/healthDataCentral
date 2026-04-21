#!/usr/bin/env python3
"""
Bake a single combined data+insights structure into docs/index.html.

Runs at the end of the routine. Queries the Azure Storage Table for activities
(PartitionKey='garmin' and PartitionKey='strava', merged + deduped by date)
and reads docs/data.json for Withings measurements, computes insight objects
for 4 time ranges, and injects the combined payload between

    <!-- DASHBOARD_DATA_START -->   ... <!-- DASHBOARD_DATA_END -->

inside docs/index.html. The front-end reads the embedded JSON instead of
fetching data.json / garmin.json separately.

Env vars (optional — if absent the script falls back to reading garmin.json):
  AZURE_STORAGE_CONNECTION_STRING
  ACTIVITIES_TABLE  (default: activities)
"""
from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass

try:
    from azure.data.tables import TableServiceClient
    TABLE_AVAILABLE = True
except ImportError:
    TABLE_AVAILABLE = False

ROOT        = Path(__file__).parent.parent
HTML_PATH   = ROOT / "docs" / "index.html"
WITHINGS_JS = ROOT / "docs" / "data.json"
GARMIN_JS   = ROOT / "docs" / "garmin.json"
STRAVA_JS   = ROOT / "docs" / "strava.json"

CONN_STR    = os.environ.get("AZURE_STORAGE_CONNECTION_STRING", "")
TABLE_NAME  = os.environ.get("ACTIVITIES_TABLE", "activities")

RANGES = [30, 90, 365, 0]  # 0 = all time

MARK_START = "<!-- DASHBOARD_DATA_START -->"
MARK_END   = "<!-- DASHBOARD_DATA_END -->"


# ── data sources ──────────────────────────────────────────────────────────────

def load_withings_from_table() -> list[dict] | None:
    """Query Azure Table for every Withings row. None on failure."""
    if not (TABLE_AVAILABLE and CONN_STR):
        return None
    try:
        service = TableServiceClient.from_connection_string(CONN_STR)
        table   = service.get_table_client(TABLE_NAME)
        rows    = []
        for e in table.query_entities("PartitionKey eq 'withings'"):
            row = {"date": str(e.get("date", ""))}
            for k in ("weight", "fat_pct", "muscle_kg"):
                if k in e and e[k] is not None:
                    row[k] = float(e[k])
            if row["date"]:
                rows.append(row)
        return sorted(rows, key=lambda r: r["date"])
    except Exception as exc:
        print(f"  [table] Withings query failed: {exc}")
        return None


def load_withings_from_file() -> list[dict]:
    if not WITHINGS_JS.exists():
        return []
    try:
        return json.loads(WITHINGS_JS.read_text())
    except Exception as exc:
        print(f"  [withings] could not parse {WITHINGS_JS.name}: {exc}")
        return []


def load_withings() -> list[dict]:
    rows = load_withings_from_table()
    if rows is None:
        print("  [withings] table unavailable, falling back to data.json")
        return load_withings_from_file()
    return rows


def load_activities_from_table(partition: str) -> list[dict] | None:
    """Query Azure Table for every activity in `partition`. None on failure."""
    if not (TABLE_AVAILABLE and CONN_STR):
        return None
    try:
        service = TableServiceClient.from_connection_string(CONN_STR)
        table   = service.get_table_client(TABLE_NAME)
        rows    = []
        for e in table.query_entities(f"PartitionKey eq '{partition}'"):
            rows.append({
                "date":         str(e.get("date", "")),
                "type":         str(e.get("type", "")),
                "duration_min": float(e.get("duration_min") or 0),
                "distance_km":  float(e.get("distance_km")  or 0),
                "calories":     int(e.get("calories")       or 0),
                "avg_hr":       int(e.get("avg_hr")         or 0),
                "max_hr":       int(e.get("max_hr")         or 0),
                "elevation_m":  int(e.get("elevation_m")    or 0),
                "source":       partition,
            })
        return rows
    except Exception as exc:
        print(f"  [table] {partition} query failed: {exc}")
        return None


def load_activities_from_file(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text())
    except Exception:
        return []


def dedup_garmin(rows: list[dict]) -> list[dict]:
    """Keep one row per date — highest calorie burn — sorted ascending."""
    best: dict[str, dict] = {}
    for r in rows:
        d = r.get("date")
        if not d:
            continue
        if d not in best or (r.get("calories") or 0) > (best[d].get("calories") or 0):
            best[d] = r
    return sorted(best.values(), key=lambda r: r["date"])


# ── insight helpers ──────────────────────────────────────────────────────────

def slice_range(rows: list[dict], days: int) -> list[dict]:
    if not days:
        return rows
    cut = (date.today() - timedelta(days=days)).isoformat()
    return [r for r in rows if r.get("date", "") >= cut]


def _lin_reg(xs: list[float], ys: list[float]) -> tuple[float, float] | None:
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = sum((x - mx) ** 2 for x in xs)
    if not den:
        return None
    slope = num / den
    return slope, my - slope * mx


def _iso_day(s: str) -> int:
    return (datetime.fromisoformat(s).date() - date(1970, 1, 1)).days


def _add_days(s: str, n: int) -> str:
    return (datetime.fromisoformat(s).date() + timedelta(days=n)).isoformat()


def _iso_week(d: str) -> str:
    dt = datetime.fromisoformat(d).date()
    y, w, _ = dt.isocalendar()
    return f"{y}-W{w:02d}"


# ── insights ──────────────────────────────────────────────────────────────────

def insight_weight_stats(rows: list[dict]) -> dict:
    ws = [r for r in rows if r.get("weight") is not None]
    weights = [r["weight"] for r in ws]
    if not ws:
        return {"count": 0}
    first, last = ws[0], ws[-1]
    return {
        "count":       len(ws),
        "first_date":  first["date"],
        "last_date":   last["date"],
        "first":       round(first["weight"], 2),
        "last":        round(last["weight"], 2),
        "delta":       round(last["weight"] - first["weight"], 2),
        "min":         round(min(weights), 2),
        "max":         round(max(weights), 2),
        "avg":         round(sum(weights) / len(weights), 2),
    }


def insight_split(rows: list[dict]) -> dict:
    ws = [r for r in rows
          if r.get("weight") is not None
          and r.get("fat_pct") is not None
          and r.get("muscle_kg") is not None]
    if len(ws) < 2:
        return {"status": "insufficient"}
    first, last = ws[0], ws[-1]
    fat_first = first["weight"] * first["fat_pct"] / 100
    fat_last  = last["weight"]  * last["fat_pct"]  / 100
    fat_delta = fat_first - fat_last       # positive = fat lost
    mus_delta = first["muscle_kg"] - last["muscle_kg"]  # positive = muscle lost
    total = abs(fat_delta) + abs(mus_delta)
    if total < 0.05:
        return {"status": "stable"}
    fat_pct = round(abs(fat_delta) / total * 100)
    return {
        "status":       "ok",
        "fat_delta_kg": round(fat_delta, 2),
        "mus_delta_kg": round(mus_delta, 2),
        "fat_share":    fat_pct,
        "mus_share":    100 - fat_pct,
        "summary":      (
            f"{fat_pct}% of change was fat "
            f"({'▼' if fat_delta >= 0 else '▲'}{abs(fat_delta):.1f} kg), "
            f"{100 - fat_pct}% muscle "
            f"({'▼' if mus_delta >= 0 else '▲'}{abs(mus_delta):.1f} kg)"
        ),
    }


def insight_trajectory(rows: list[dict]) -> dict:
    window = [r for r in rows if r.get("fat_pct") is not None][-30:]
    if len(window) < 5:
        return {"status": "insufficient"}
    xs = [_iso_day(r["date"]) for r in window]
    ys = [r["fat_pct"] for r in window]
    reg = _lin_reg(xs, ys)
    if reg is None:
        return {"status": "flat"}
    slope, intercept = reg
    last_day = xs[-1]
    proj_days = 30
    proj_val  = intercept + slope * (last_day + proj_days)
    proj_date = _add_days(window[-1]["date"], proj_days)
    return {
        "status":        "ok",
        "slope_per_day": round(slope, 4),
        "proj_fat_pct":  round(proj_val, 1),
        "proj_date":     proj_date,
        "direction":     "reach" if slope < 0 else "rise to",
        "summary": (
            f"At this rate you'll "
            f"{'reach' if slope < 0 else 'rise to'} "
            f"{proj_val:.1f}% body fat by {proj_date[5:]}"
        ),
    }


def insight_hr(gdata: list[dict]) -> dict:
    easy = [r for r in gdata if 0 < r.get("avg_hr", 0) < 130]
    if not easy:
        return {"status": "no_data"}
    today = date.today().isoformat()
    cut30 = _add_days(today, -30)
    cut90 = _add_days(today, -90)
    hr30 = [r["avg_hr"] for r in easy if r["date"] >= cut30]
    hr90 = [r["avg_hr"] for r in easy if r["date"] >= cut90]
    avg30 = sum(hr30) / len(hr30) if hr30 else None
    avg90 = sum(hr90) / len(hr90) if hr90 else None
    diff  = (avg30 - avg90) if (avg30 is not None and avg90 is not None) else None
    return {
        "status": "ok",
        "avg_30d": round(avg30, 1) if avg30 is not None else None,
        "avg_90d": round(avg90, 1) if avg90 is not None else None,
        "diff":    round(diff, 1) if diff is not None else None,
    }


def insight_consistency(gdata: list[dict]) -> dict:
    if not gdata:
        return {"status": "no_data"}
    weeks: dict[str, int] = {}
    for r in gdata:
        weeks[_iso_week(r["date"])] = weeks.get(_iso_week(r["date"]), 0) + 1
    consistent = sum(1 for c in weeks.values() if c >= 3)
    score = round(consistent / len(weeks) * 100) if weeks else 0

    active_dates = {r["date"] for r in gdata}
    streak = 0
    cur = date.today()
    while cur.isoformat() in active_dates:
        streak += 1
        cur -= timedelta(days=1)

    if score >= 80:
        qual = "excellent consistency 🔥"
    elif score >= 50:
        qual = "solid routine"
    else:
        qual = "room to improve"

    return {
        "status":  "ok",
        "score":   score,
        "streak":  streak,
        "weeks":   len(weeks),
        "summary": f"{score}% of weeks had ≥3 sessions — {qual}",
    }


def insight_training_load(gdata: list[dict], wdata: list[dict]) -> dict:
    if not gdata:
        return {"status": "no_data"}
    week_min: dict[str, float] = {}
    week_any_date: dict[str, str] = {}
    for r in gdata:
        k = _iso_week(r["date"])
        week_min[k] = week_min.get(k, 0) + (r.get("duration_min") or 0)
        if k not in week_any_date or r["date"] < week_any_date[k]:
            week_any_date[k] = r["date"]
    week_w: dict[str, list[float]] = {}
    for r in wdata:
        if r.get("weight") is None:
            continue
        k = _iso_week(r["date"])
        week_w.setdefault(k, []).append(r["weight"])
    keys = sorted(set(week_min) | set(week_w))
    return {
        "status": "ok",
        "weeks": [
            {
                "week":       k,
                "label":      week_any_date.get(k, k),
                "minutes":    round(week_min.get(k, 0)),
                "avg_weight": round(sum(week_w[k]) / len(week_w[k]), 2) if week_w.get(k) else None,
            }
            for k in keys
        ],
    }


def insight_garmin_stats(gdata: list[dict]) -> dict:
    if not gdata:
        return {"status": "no_data"}
    total_min = sum(r.get("duration_min") or 0 for r in gdata)
    with_dist = [r for r in gdata if (r.get("distance_km") or 0) > 0]
    with_hr   = [r for r in gdata if (r.get("avg_hr") or 0) > 0]
    return {
        "status":    "ok",
        "workouts":  len(gdata),
        "total_min": round(total_min),
        "avg_dist":  round(sum(r["distance_km"] for r in with_dist) / len(with_dist), 2) if with_dist else None,
        "avg_hr":    round(sum(r["avg_hr"]      for r in with_hr)   / len(with_hr))      if with_hr   else None,
    }


# ── per-range aggregation + HTML injection ───────────────────────────────────

def insights_for_range(wdata: list[dict], gdata: list[dict], days: int) -> dict:
    w = slice_range(wdata, days)
    g = slice_range(gdata, days)
    return {
        "withings_count": len(w),
        "garmin_count":   len(g),
        "stats":          insight_weight_stats(w),
        "split":          insight_split(w),
        "trajectory":     insight_trajectory(w),
        "hr":             insight_hr(g),
        "consistency":    insight_consistency(g),
        "training_load":  insight_training_load(g, w),
        "garmin_stats":   insight_garmin_stats(g),
    }


def build_payload(wdata: list[dict], gdata: list[dict]) -> dict:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "withings":     wdata,
        "garmin":       gdata,
        "insights":     {str(d): insights_for_range(wdata, gdata, d) for d in RANGES},
    }


def inject(html: str, payload: dict) -> str:
    block = (
        f'{MARK_START}\n'
        f'<script id="dashboard-data" type="application/json">\n'
        f'{json.dumps(payload, ensure_ascii=False, separators=(",", ":"))}\n'
        f'</script>\n'
        f'{MARK_END}'
    )
    pattern = re.compile(
        re.escape(MARK_START) + r".*?" + re.escape(MARK_END),
        re.DOTALL,
    )
    if pattern.search(html):
        return pattern.sub(lambda _: block, html)
    # First-time insertion: place it before the main inline <script> so the
    # JSON element is parsed before the boot code that reads it.
    anchor = "</main>"
    if anchor in html:
        return html.replace(anchor, f"{anchor}\n\n{block}\n", 1)
    return html.replace("</body>", block + "\n</body>")


def main() -> None:
    print("=== Build Dashboard ===")
    withings = load_withings()
    print(f"  withings: {len(withings)} rows")

    garmin = load_activities_from_table("garmin")
    if garmin is None:
        print("  garmin: table unavailable, falling back to garmin.json")
        garmin = load_activities_from_file(GARMIN_JS)
    print(f"  garmin:   {len(garmin)} rows")

    strava = load_activities_from_table("strava")
    if strava is None:
        print("  strava: table unavailable, falling back to strava.json")
        strava = load_activities_from_file(STRAVA_JS)
    print(f"  strava:   {len(strava)} rows")

    activities = dedup_garmin(garmin + strava)
    print(f"  merged:   {len(activities)} unique-day rows")

    payload = build_payload(withings, activities)

    html = HTML_PATH.read_text(encoding="utf-8")
    HTML_PATH.write_text(inject(html, payload), encoding="utf-8")
    print(f"  baked payload into {HTML_PATH}")
    print("=== Done ===")


if __name__ == "__main__":
    main()


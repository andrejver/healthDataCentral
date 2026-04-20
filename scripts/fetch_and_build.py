"""
Fetch Withings measurements, upsert into the Azure Storage Table, and
write docs/data.json from the table.

The Withings refresh token stays in Azure Blob Storage (rotated on every
run). Measurements are stored in the same Azure Table that holds Garmin
activities (default table: 'activities'):

    PartitionKey = "withings"      RowKey = <YYYY-MM-DD>
    PartitionKey = "meta"          RowKey = "withings_cursor"  (last epoch seen)

Incremental fetch: on each run we ask Withings only for measurements newer
than the cursor timestamp (lastupdate param). The first ever run uses a
two-year lookback.

Required environment variables:
    WITHINGS_CLIENT_ID
    WITHINGS_CLIENT_SECRET
    AZURE_STORAGE_CONNECTION_STRING
    AZURE_STORAGE_CONTAINER    (default: 'withings' — blob container for token)

Optional:
    ACTIVITIES_TABLE           (default: 'activities')
"""

import json
import os
import time
import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv
from azure.storage.blob import BlobServiceClient
from azure.data.tables import TableServiceClient
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CLIENT_ID     = os.environ["WITHINGS_CLIENT_ID"]
CLIENT_SECRET = os.environ["WITHINGS_CLIENT_SECRET"]
CONN_STR      = os.environ["AZURE_STORAGE_CONNECTION_STRING"]
CONTAINER     = os.environ.get("AZURE_STORAGE_CONTAINER", "withings")
TABLE_NAME    = os.environ.get("ACTIVITIES_TABLE", "activities")
BLOB_NAME     = "refresh_token.txt"

TOKEN_URL   = "https://wbsapi.withings.net/v2/oauth2"
MEASURE_URL = "https://wbsapi.withings.net/measure"

MEAS_WEIGHT  = 1    # kg
MEAS_FAT_PCT = 6    # %
MEAS_MUSCLE  = 76   # kg

TWO_YEARS_SECS = 2 * 365 * 24 * 3600
DATA_PATH = Path(__file__).parent.parent / "docs" / "data.json"

# ---------------------------------------------------------------------------
# Blob Storage helpers (refresh token)
# ---------------------------------------------------------------------------

def _blob_client():
    service = BlobServiceClient.from_connection_string(CONN_STR)
    return service.get_blob_client(container=CONTAINER, blob=BLOB_NAME)


def read_refresh_token() -> str:
    return _blob_client().download_blob().readall().decode().strip()


def write_refresh_token(token: str) -> None:
    _blob_client().upload_blob(token.encode(), overwrite=True)

# ---------------------------------------------------------------------------
# Table Storage helpers (measurements + cursor)
# ---------------------------------------------------------------------------

def get_table_client():
    service = TableServiceClient.from_connection_string(CONN_STR)
    table = service.get_table_client(TABLE_NAME)
    try:
        table.create_table()
        print(f"  [table] created table '{TABLE_NAME}'")
    except ResourceExistsError:
        pass
    return table


def get_cursor(table) -> int:
    """Return last_epoch stored for Withings, or 0 if none."""
    try:
        e = table.get_entity(partition_key="meta", row_key="withings_cursor")
        return int(e.get("last_epoch") or 0)
    except ResourceNotFoundError:
        return 0
    except Exception as exc:
        print(f"  [table] could not read cursor: {exc}")
        return 0


def save_cursor(table, last_epoch: int) -> None:
    table.upsert_entity({
        "PartitionKey": "meta",
        "RowKey":       "withings_cursor",
        "last_epoch":   last_epoch,
    })


def upsert_measurements(table, rows: list[dict]) -> None:
    for r in rows:
        entity = {
            "PartitionKey": "withings",
            "RowKey":       r["date"],
            "source":       "withings",
            "date":         r["date"],
        }
        for k in ("weight", "fat_pct", "muscle_kg"):
            if r.get(k) is not None:
                entity[k] = r[k]
        table.upsert_entity(entity)


def load_all_from_table(table) -> list[dict]:
    out = []
    for e in table.query_entities("PartitionKey eq 'withings'"):
        row = {"date": str(e["date"])}
        for k in ("weight", "fat_pct", "muscle_kg"):
            if k in e and e[k] is not None:
                row[k] = float(e[k])
        out.append(row)
    return sorted(out, key=lambda r: r["date"])


# ---------------------------------------------------------------------------
# Token refresh
# ---------------------------------------------------------------------------

def refresh_access_token(refresh_token: str) -> tuple[str, str]:
    """Return (access_token, new_refresh_token). Withings rotates on every use."""
    resp = requests.post(
        TOKEN_URL,
        data={
            "action":        "requesttoken",
            "grant_type":    "refresh_token",
            "client_id":     CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "refresh_token": refresh_token,
        },
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get("status") != 0:
        raise RuntimeError(f"Token refresh failed: {body}")
    tokens = body["body"]
    return tokens["access_token"], tokens["refresh_token"]


# ---------------------------------------------------------------------------
# Measurements
# ---------------------------------------------------------------------------

def fetch_measurements(access_token: str, since_epoch: int) -> tuple[list[dict], int]:
    """Fetch Withings measurement groups newer than `since_epoch`.

    Returns (groups, max_epoch_seen). When `since_epoch` is 0 a two-year
    lookback is used. Otherwise we pass `lastupdate` so the API only returns
    groups that were *modified* after the cursor — cheaper and avoids
    downloading the same rows every run.
    """
    params = {
        "action":    "getmeas",
        "meastypes": f"{MEAS_WEIGHT},{MEAS_FAT_PCT},{MEAS_MUSCLE}",
        "category":  1,
    }
    if since_epoch > 0:
        params["lastupdate"] = since_epoch
    else:
        params["startdate"] = int(time.time()) - TWO_YEARS_SECS

    resp = requests.get(
        MEASURE_URL,
        params=params,
        headers={"Authorization": f"Bearer {access_token}"},
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get("status") != 0:
        raise RuntimeError(f"Measure fetch failed: {body}")
    groups = body["body"]["measuregrps"]
    max_epoch = max((g["date"] for g in groups), default=since_epoch)
    return groups, max_epoch


def _decode(value: int, unit: int) -> float:
    return value * (10 ** unit)


def _ts_to_date(ts: int) -> str:
    return datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")


def parse_groups(groups: list[dict]) -> list[dict]:
    by_date: dict[str, dict] = {}
    for grp in groups:
        date_str = _ts_to_date(grp["date"])
        row = by_date.setdefault(date_str, {"date": date_str})
        for m in grp["measures"]:
            val = _decode(m["value"], m["unit"])
            if m["type"] == MEAS_WEIGHT:
                row["weight"] = round(val, 2)
            elif m["type"] == MEAS_FAT_PCT:
                row["fat_pct"] = round(val, 2)
            elif m["type"] == MEAS_MUSCLE:
                row["muscle_kg"] = round(val, 2)
    return sorted(by_date.values(), key=lambda r: r["date"])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=== Withings Fetch ===")
    print("Reading refresh token from Azure Blob Storage...")
    refresh_token = read_refresh_token()

    print("Refreshing Withings access token...")
    access_token, new_refresh_token = refresh_access_token(refresh_token)
    write_refresh_token(new_refresh_token)
    print("  Token rotated successfully.")

    table = get_table_client()
    cursor = get_cursor(table)
    if cursor:
        print(f"  incremental fetch: lastupdate={cursor} ({_ts_to_date(cursor)})")
    else:
        print("  full fetch: last 2 years")

    groups, max_epoch = fetch_measurements(access_token, cursor)
    new_rows = parse_groups(groups)
    print(f"  fetched {len(groups)} groups → {len(new_rows)} date rows")

    if new_rows:
        upsert_measurements(table, new_rows)
        print(f"  upserted {len(new_rows)} rows into '{TABLE_NAME}'")
    if max_epoch and max_epoch > cursor:
        save_cursor(table, int(max_epoch))
        print(f"  cursor → {max_epoch}")

    all_rows = load_all_from_table(table)
    print(f"  → {len(all_rows)} total Withings rows in table")

    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(json.dumps(all_rows, indent=2))
    print(f"Written: {DATA_PATH}")
    print("=== Done ===")


if __name__ == "__main__":
    main()

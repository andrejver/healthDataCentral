#!/usr/bin/env python3
"""
Fetch Garmin workout data, store in Azure Table Storage, and write docs/garmin.json.

Auth: garminconnect uses OAuth2 tokens stored as a session blob in Azure Blob Storage.
On first run, email + password env vars are used; the resulting token blob is saved
back to Azure.

Table Storage: activities are stored in an Azure Storage Table (default: 'activities').
Each entity uses PartitionKey='garmin' and RowKey=<activityId>, so Strava activities
can coexist in the same table with PartitionKey='strava'.

Incremental fetch: the date of the last stored Garmin activity is saved as a cursor
entity (PartitionKey='meta', RowKey='garmin_cursor'). Subsequent runs call
get_activities_by_date(startdate=cursor_date, enddate=today) to fetch only new ones.

Required env vars (set in Claude Code Routine secrets):
  GARMIN_EMAIL
  GARMIN_PASSWORD
  AZURE_STORAGE_CONNECTION_STRING   (same as fetch_and_build.py)
  AZURE_CONTAINER_NAME              (same as fetch_and_build.py)

Optional:
  GARMIN_SESSION_BLOB   (default: garmin/session.json)
  GARMIN_ACTIVITY_COUNT (default: 200, used only on the initial full fetch)
  ACTIVITIES_TABLE      (default: activities)
"""

import json
import os
import sys
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

# Load .env from repo root if present
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass

# Use Windows system trust store so corporate CA certs are trusted (patches ssl/requests)
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

# Also build a CA bundle for curl_cffi (libcurl), which bypasses Python's ssl module
def _build_system_ca_bundle() -> str | None:
    """Merge certifi's bundle with Windows root CAs; return path to temp PEM file."""
    import base64
    import ssl
    import tempfile
    try:
        import certifi
        pem = Path(certifi.where()).read_bytes()
    except ImportError:
        pem = b""
    try:
        for cert_data, enc_type, _trust in ssl.enum_certificates("ROOT"):
            if enc_type == "x509_asn":
                b64 = base64.b64encode(cert_data).decode("ascii")
                lines = "\n".join(b64[i:i+64] for i in range(0, len(b64), 64))
                pem += f"-----BEGIN CERTIFICATE-----\n{lines}\n-----END CERTIFICATE-----\n".encode()
    except Exception:
        return None
    tmp = tempfile.NamedTemporaryFile(suffix=".pem", delete=False)
    tmp.write(pem)
    tmp.close()
    return tmp.name

_ca_bundle = _build_system_ca_bundle()
if _ca_bundle:
    os.environ.setdefault("CURL_CA_BUNDLE", _ca_bundle)
    os.environ.setdefault("REQUESTS_CA_BUNDLE", _ca_bundle)

# ── dependency guard ──────────────────────────────────────────────────────────
try:
    from garminconnect import Garmin, GarminConnectAuthenticationError
except ImportError:
    print("ERROR: garminconnect not installed. Run: pip install garminconnect")
    sys.exit(1)

try:
    from azure.storage.blob import BlobServiceClient
    BLOB_AVAILABLE = True
except ImportError:
    BLOB_AVAILABLE = False

try:
    from azure.data.tables import TableServiceClient
    from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
    TABLE_AVAILABLE = True
except ImportError:
    TABLE_AVAILABLE = False
    print("WARN: azure-data-tables not installed — table storage disabled. Run: pip install azure-data-tables")

# ── config ────────────────────────────────────────────────────────────────────
EMAIL          = os.environ.get("GARMIN_EMAIL", "")
PASSWORD       = os.environ.get("GARMIN_PASSWORD", "")
CONN_STR       = os.environ.get("AZURE_STORAGE_CONNECTION_STRING", "")
CONTAINER      = os.environ.get("AZURE_CONTAINER_NAME", "healthdata")
SESSION_BLOB   = os.environ.get("GARMIN_SESSION_BLOB", "garmin/session.json")
ACTIVITY_COUNT = int(os.environ.get("GARMIN_ACTIVITY_COUNT", "200"))
TABLE_NAME     = os.environ.get("ACTIVITIES_TABLE", "activities")
OUT_PATH       = Path(__file__).parent.parent / "docs" / "garmin.json"

# ── Azure Blob helpers (session token) ───────────────────────────────────────

def load_session_blob() -> str | None:
    """Download serialised Garmin token string from Azure. Returns str or None."""
    if not (BLOB_AVAILABLE and CONN_STR):
        return None
    try:
        client = BlobServiceClient.from_connection_string(CONN_STR)
        blob   = client.get_container_client(CONTAINER).get_blob_client(SESSION_BLOB)
        data   = blob.download_blob().readall()
        return data.decode("utf-8")
    except Exception as e:
        print(f"  [azure] session blob not found or unreadable: {e}")
        return None


def save_session_blob(token_str: str):
    """Upload serialised Garmin token string to Azure."""
    if not (BLOB_AVAILABLE and CONN_STR):
        return
    try:
        client = BlobServiceClient.from_connection_string(CONN_STR)
        blob   = client.get_container_client(CONTAINER).get_blob_client(SESSION_BLOB)
        blob.upload_blob(token_str.encode("utf-8"), overwrite=True)
        print("  [azure] session blob updated")
    except Exception as e:
        print(f"  [azure] failed to save session blob: {e}")

# ── Azure Table helpers (activity storage) ───────────────────────────────────

def get_table_client():
    """Return an Azure Table client, creating the table if it doesn't exist."""
    if not (TABLE_AVAILABLE and CONN_STR):
        return None
    try:
        service = TableServiceClient.from_connection_string(CONN_STR)
        table = service.get_table_client(TABLE_NAME)
        try:
            table.create_table()
            print(f"  [table] created table '{TABLE_NAME}'")
        except ResourceExistsError:
            pass
        return table
    except Exception as e:
        print(f"  [table] could not connect: {e}")
        return None


def get_cursor(table) -> str | None:
    """Return YYYY-MM-DD date of the most recently stored Garmin activity."""
    try:
        entity = table.get_entity(partition_key="meta", row_key="garmin_cursor")
        return str(entity["last_date"])
    except ResourceNotFoundError:
        return None
    except Exception as e:
        print(f"  [table] could not read cursor: {e}")
        return None


def save_cursor(table, last_date: str) -> None:
    table.upsert_entity({
        "PartitionKey": "meta",
        "RowKey":       "garmin_cursor",
        "last_date":    last_date,
    })


def upsert_activities(table, records: list[dict]) -> None:
    """Upsert activity records into the table."""
    for r in records:
        entity = {
            "PartitionKey":  "garmin",
            "RowKey":        r["activity_id"],
            "source":        "garmin",
            "start_epoch":   r["start_epoch"],
            "date":          r["date"],
            "type":          r["type"],
            "duration_min":  r["duration_min"],
            "distance_km":   r["distance_km"],
            "calories":      r["calories"],
            "avg_hr":        r["avg_hr"],
            "max_hr":        r["max_hr"],
            "elevation_m":   r["elevation_m"],
        }
        table.upsert_entity(entity)


def load_all_from_table(table) -> list[dict]:
    """Return all Garmin activity records from the table as plain dicts."""
    records = []
    for e in table.query_entities("PartitionKey eq 'garmin'"):
        records.append({
            "date":         e["date"],
            "type":         e["type"],
            "duration_min": float(e.get("duration_min", 0)),
            "distance_km":  float(e.get("distance_km", 0)),
            "calories":     int(e.get("calories", 0)),
            "avg_hr":       int(e.get("avg_hr", 0)),
            "max_hr":       int(e.get("max_hr", 0)),
            "elevation_m":  int(e.get("elevation_m", 0)),
        })
    return records

# ── Garmin auth ───────────────────────────────────────────────────────────────

def get_garmin_client() -> Garmin:
    """Return an authenticated Garmin client, restoring session if possible."""
    if not (EMAIL and PASSWORD):
        raise RuntimeError("GARMIN_EMAIL and GARMIN_PASSWORD env vars are required.")

    token_str = load_session_blob()
    if token_str:
        try:
            api = Garmin(EMAIL, PASSWORD)
            api.login(tokenstore=token_str)
            save_session_blob(api.client.dumps())
            print("  [garmin] session restored from Azure blob")
            return api
        except Exception as e:
            print(f"  [garmin] session restore failed ({e}), re-authenticating…")

    # Full login — requires interactive MFA; fail clearly in non-interactive environments
    if not sys.stdin.isatty():
        raise RuntimeError(
            "Garmin session token missing or expired and no interactive terminal "
            "available for MFA. Run the script interactively once to refresh the token."
        )

    def prompt_mfa():
        return input("  [garmin] Enter MFA/2FA code: ").strip()

    api = Garmin(EMAIL, PASSWORD, prompt_mfa=prompt_mfa)
    api.login()
    print("  [garmin] authenticated with email/password")
    save_session_blob(api.client.dumps())
    return api

# ── activity parsing ──────────────────────────────────────────────────────────

TYPE_MAP = {
    "running":          "running",
    "trail_running":    "running",
    "treadmill_running":"running",
    "cycling":          "cycling",
    "road_biking":      "cycling",
    "mountain_biking":  "cycling",
    "indoor_cycling":   "cycling",
    "virtual_ride":     "cycling",
    "walking":          "walking",
    "hiking":           "hiking",
    "strength_training":"strength",
    "fitness_equipment":"strength",
    "swimming":         "swimming",
    "open_water_swimming":"swimming",
    "yoga":             "yoga",
    "elliptical":       "elliptical",
}

def normalise_type(raw: str) -> str:
    return TYPE_MAP.get(raw.lower(), raw.lower())


def parse_activity(act: dict) -> dict | None:
    """Extract a flat record from a Garmin activity dict. Returns None to skip."""
    try:
        start_local = act.get("startTimeLocal") or act.get("startTimeGMT", "")
        date_str = start_local[:10] if start_local else None
        if not date_str:
            return None

        # Epoch from GMT start time for cursor tracking
        start_gmt = act.get("startTimeGMT", start_local)
        try:
            epoch = int(datetime.fromisoformat(
                start_gmt.replace("Z", "+00:00")
            ).replace(tzinfo=timezone.utc).timestamp())
        except Exception:
            epoch = 0

        atype    = normalise_type(act.get("activityType", {}).get("typeKey", "other"))
        dur_s    = act.get("duration") or act.get("movingDuration") or 0
        dur_m    = round(dur_s / 60, 1)
        dist_m   = act.get("distance") or 0
        dist_km  = round(dist_m / 1000, 2) if dist_m else 0
        calories = int(act.get("calories") or 0)
        avg_hr   = int(act.get("averageHR") or 0)
        max_hr   = int(act.get("maxHR") or 0)
        elev_m   = int(act.get("elevationGain") or 0)

        return {
            "activity_id":  str(act.get("activityId", f"{date_str}-{dur_m}")),
            "source":       "garmin",
            "start_epoch":  epoch,
            "date":         date_str,
            "type":         atype,
            "duration_min": dur_m,
            "distance_km":  dist_km,
            "calories":     calories,
            "avg_hr":       avg_hr,
            "max_hr":       max_hr,
            "elevation_m":  elev_m,
        }
    except Exception as e:
        print(f"  [parse] skipping activity: {e}")
        return None


def dedup_by_date(records: list[dict]) -> list[dict]:
    """Keep one record per date: the one with the highest calorie burn."""
    best: dict[str, dict] = {}
    for r in records:
        d = r["date"]
        if d not in best or r["calories"] > best[d]["calories"]:
            best[d] = r
    return sorted(best.values(), key=lambda r: r["date"])

# ── main ──────────────────────────────────────────────────────────────────────

def main():
    print("=== Garmin Fetch ===")

    api = get_garmin_client()
    table = get_table_client()

    cursor_date = get_cursor(table) if table else None
    today = date.today().isoformat()

    if cursor_date:
        # Incremental: fetch from day after cursor up to today
        start_date = (date.fromisoformat(cursor_date) + timedelta(days=1)).isoformat()
        print(f"  incremental fetch: {start_date} → {today}")
        raw_activities = api.get_activities_by_date(start_date, today)
    else:
        print(f"  full fetch: last {ACTIVITY_COUNT} activities")
        raw_activities = api.get_activities(0, ACTIVITY_COUNT)

    print(f"  received {len(raw_activities)} activities")

    # Rotate session token after successful API call
    try:
        save_session_blob(api.client.dumps())
    except Exception:
        pass

    new_records = [r for a in raw_activities if (r := parse_activity(a))]

    if new_records and table:
        upsert_activities(table, new_records)
        max_date = max(r["date"] for r in new_records)
        save_cursor(table, max_date)
        print(f"  upserted {len(new_records)} records to table '{TABLE_NAME}'")

    # Build JSON from full table contents (always up to date)
    if table:
        all_records = load_all_from_table(table)
    else:
        all_records = new_records

    output = dedup_by_date(all_records)
    print(f"  → {len(output)} unique days in garmin.json")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(output, f, indent=2)

    print(f"  wrote {OUT_PATH}")
    print("=== Done ===")


if __name__ == "__main__":
    main()


import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Load .env from repo root if present
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass

# Use Windows system trust store so corporate CA certs are trusted (patches ssl/requests)
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

# Also build a CA bundle for curl_cffi (libcurl), which bypasses Python's ssl module
def _build_system_ca_bundle() -> str | None:
    """Merge certifi's bundle with Windows root CAs; return path to temp PEM file."""
    import base64
    import ssl
    import tempfile
    try:
        import certifi
        pem = Path(certifi.where()).read_bytes()
    except ImportError:
        pem = b""
    try:
        for cert_data, enc_type, _trust in ssl.enum_certificates("ROOT"):
            if enc_type == "x509_asn":
                b64 = base64.b64encode(cert_data).decode("ascii")
                lines = "\n".join(b64[i:i+64] for i in range(0, len(b64), 64))
                pem += f"-----BEGIN CERTIFICATE-----\n{lines}\n-----END CERTIFICATE-----\n".encode()
    except Exception:
        return None
    tmp = tempfile.NamedTemporaryFile(suffix=".pem", delete=False)
    tmp.write(pem)
    tmp.close()
    return tmp.name

_ca_bundle = _build_system_ca_bundle()
if _ca_bundle:
    os.environ.setdefault("CURL_CA_BUNDLE", _ca_bundle)
    os.environ.setdefault("REQUESTS_CA_BUNDLE", _ca_bundle)

# ── dependency guard ──────────────────────────────────────────────────────────
try:
    from garminconnect import Garmin, GarminConnectAuthenticationError
except ImportError:
    print("ERROR: garminconnect not installed. Run: pip install garminconnect")
    sys.exit(1)

try:
    from azure.storage.blob import BlobServiceClient
    AZURE_AVAILABLE = True
except ImportError:
    AZURE_AVAILABLE = False

# ── config ────────────────────────────────────────────────────────────────────
EMAIL       = os.environ.get("GARMIN_EMAIL", "")
PASSWORD    = os.environ.get("GARMIN_PASSWORD", "")
CONN_STR    = os.environ.get("AZURE_STORAGE_CONNECTION_STRING", "")
CONTAINER   = os.environ.get("AZURE_CONTAINER_NAME", "healthdata")
SESSION_BLOB = os.environ.get("GARMIN_SESSION_BLOB", "garmin/session.json")
ACTIVITY_COUNT = int(os.environ.get("GARMIN_ACTIVITY_COUNT", "200"))
OUT_PATH    = Path(__file__).parent.parent / "docs" / "garmin.json"

# ── Azure helpers ─────────────────────────────────────────────────────────────

def load_session_blob() -> str | None:
    """Download serialised Garmin token string from Azure. Returns str or None."""
    if not (AZURE_AVAILABLE and CONN_STR):
        return None
    try:
        client = BlobServiceClient.from_connection_string(CONN_STR)
        blob   = client.get_container_client(CONTAINER).get_blob_client(SESSION_BLOB)
        data   = blob.download_blob().readall()
        return data.decode("utf-8")
    except Exception as e:
        print(f"  [azure] session blob not found or unreadable: {e}")
        return None


def save_session_blob(token_str: str):
    """Upload serialised Garmin token string to Azure."""
    if not (AZURE_AVAILABLE and CONN_STR):
        return
    try:
        client = BlobServiceClient.from_connection_string(CONN_STR)
        blob   = client.get_container_client(CONTAINER).get_blob_client(SESSION_BLOB)
        blob.upload_blob(token_str.encode("utf-8"), overwrite=True)
        print("  [azure] session blob updated")
    except Exception as e:
        print(f"  [azure] failed to save session blob: {e}")

# ── Garmin auth ───────────────────────────────────────────────────────────────

def get_garmin_client() -> Garmin:
    """Return an authenticated Garmin client, restoring session if possible."""
    if not (EMAIL and PASSWORD):
        raise RuntimeError("GARMIN_EMAIL and GARMIN_PASSWORD env vars are required.")

    token_str = load_session_blob()
    if token_str:
        try:
            api = Garmin(EMAIL, PASSWORD)
            api.login(tokenstore=token_str)
            # Persist refreshed token
            save_session_blob(api.client.dumps())
            print("  [garmin] session restored from Azure blob")
            return api
        except Exception as e:
            print(f"  [garmin] session restore failed ({e}), re-authenticating…")

    # Full login — requires interactive MFA; fail clearly in non-interactive environments
    if not sys.stdin.isatty():
        raise RuntimeError(
            "Garmin session token missing or expired and no interactive terminal "
            "available for MFA. Run the script interactively once to refresh the token."
        )

    def prompt_mfa():
        return input("  [garmin] Enter MFA/2FA code: ").strip()

    api = Garmin(EMAIL, PASSWORD, prompt_mfa=prompt_mfa)
    api.login()
    print("  [garmin] authenticated with email/password")
    save_session_blob(api.client.dumps())
    return api

# ── activity parsing ──────────────────────────────────────────────────────────

TYPE_MAP = {
    "running":          "running",
    "trail_running":    "running",
    "treadmill_running":"running",
    "cycling":          "cycling",
    "road_biking":      "cycling",
    "mountain_biking":  "cycling",
    "indoor_cycling":   "cycling",
    "virtual_ride":     "cycling",
    "walking":          "walking",
    "hiking":           "hiking",
    "strength_training":"strength",
    "fitness_equipment":"strength",
    "swimming":         "swimming",
    "open_water_swimming":"swimming",
    "yoga":             "yoga",
    "elliptical":       "elliptical",
}

def normalise_type(raw: str) -> str:
    return TYPE_MAP.get(raw.lower(), raw.lower())


def parse_activity(act: dict) -> dict | None:
    """Extract a flat record from a Garmin activity dict. Returns None to skip."""
    try:
        start = act.get("startTimeLocal") or act.get("startTimeGMT", "")
        date  = start[:10] if start else None
        if not date:
            return None

        atype = normalise_type(act.get("activityType", {}).get("typeKey", "other"))
        dur_s = act.get("duration") or act.get("movingDuration") or 0
        dur_m = round(dur_s / 60, 1)

        dist_m = act.get("distance") or 0
        dist_km = round(dist_m / 1000, 2) if dist_m else 0

        calories  = int(act.get("calories") or 0)
        avg_hr    = int(act.get("averageHR") or 0)
        max_hr    = int(act.get("maxHR") or 0)
        elev_m    = int(act.get("elevationGain") or 0)

        return {
            "date":        date,
            "type":        atype,
            "duration_min": dur_m,
            "distance_km": dist_km,
            "calories":    calories,
            "avg_hr":      avg_hr,
            "max_hr":      max_hr,
            "elevation_m": elev_m,
        }
    except Exception as e:
        print(f"  [parse] skipping activity: {e}")
        return None


def dedup_by_date(records: list[dict]) -> list[dict]:
    """Keep one record per date: the one with the highest calorie burn."""
    best: dict[str, dict] = {}
    for r in records:
        d = r["date"]
        if d not in best or r["calories"] > best[d]["calories"]:
            best[d] = r
    return sorted(best.values(), key=lambda r: r["date"])

# ── main ──────────────────────────────────────────────────────────────────────

def main():
    print("=== Garmin Fetch ===")

    api = get_garmin_client()

    print(f"  fetching last {ACTIVITY_COUNT} activities…")
    raw_activities = api.get_activities(0, ACTIVITY_COUNT)
    print(f"  received {len(raw_activities)} activities")

    # Rotate session token after successful API call
    try:
        save_session_blob(api.client.dumps())
    except Exception:
        pass

    records = [r for a in raw_activities if (r := parse_activity(a))]
    records = dedup_by_date(records)
    print(f"  → {len(records)} unique days after dedup")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(records, f, indent=2)

    print(f"  wrote {OUT_PATH}")
    print("=== Done ===")


if __name__ == "__main__":
    main()

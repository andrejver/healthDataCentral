#!/usr/bin/env python3
"""
Fetch Strava activity data, store in Azure Table Storage, and write docs/strava.json.

Auth: Strava uses OAuth2. The refresh token is stored in Azure Blob Storage.
On first run, bootstrap by setting STRAVA_REFRESH_TOKEN in the environment;
subsequent runs read/write the token from Azure automatically.

Table Storage: activities are stored in an Azure Storage Table (default: 'activities').
Each entity uses PartitionKey=<source> and RowKey=<activity_id>, so Garmin
activities can be stored in the same table with PartitionKey='garmin'.

Incremental fetch: the epoch of the last stored activity is saved as a cursor
entity (PartitionKey='meta', RowKey='strava_cursor'). Subsequent runs pass it
as the `after` parameter to the Strava API so only new activities are fetched.

Required env vars:
  STRAVA_CLIENT_ID
  STRAVA_CLIENT_SECRET
  AZURE_STORAGE_CONNECTION_STRING   (same as fetch_and_build.py)
  AZURE_CONTAINER_NAME              (same as fetch_and_build.py)

Optional:
  STRAVA_REFRESH_TOKEN    (bootstrap only — not needed once blob exists)
  STRAVA_REFRESH_BLOB     (default: strava/refresh_token.txt)
  STRAVA_ACTIVITY_COUNT   (default: 200, used only on the initial full fetch)
  ACTIVITIES_TABLE        (default: activities)
"""

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

# Use Windows system trust store so corporate CA certs are trusted
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

# Build a CA bundle for curl/requests when behind a corporate proxy
def _build_system_ca_bundle() -> str | None:
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

# ── dependency guard ────────────────────────────────────────────
try:
    import requests
except ImportError:
    print("ERROR: requests not installed. Run: pip install requests")
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

# ── config ─────────────────────────────────────────────────────────────────────
CLIENT_ID      = os.environ.get("STRAVA_CLIENT_ID", "")
CLIENT_SECRET  = os.environ.get("STRAVA_CLIENT_SECRET", "")
CONN_STR       = os.environ.get("AZURE_STORAGE_CONNECTION_STRING", "")
CONTAINER      = os.environ.get("AZURE_CONTAINER_NAME", "healthdata")
REFRESH_BLOB   = os.environ.get("STRAVA_REFRESH_BLOB", "strava/refresh_token.txt")
ACTIVITY_COUNT = int(os.environ.get("STRAVA_ACTIVITY_COUNT", "200"))
TABLE_NAME     = os.environ.get("ACTIVITIES_TABLE", "activities")
OUT_PATH       = Path(__file__).parent.parent / "docs" / "strava.json"

TOKEN_URL      = "https://www.strava.com/oauth/token"
ACTIVITIES_URL = "https://www.strava.com/api/v3/athlete/activities"

# ── Azure Blob helpers (refresh token) ────────────────────────────────────────

def load_refresh_token() -> str | None:
    """Load Strava refresh token from Azure blob, or bootstrap from env var."""
    if BLOB_AVAILABLE and CONN_STR:
        try:
            client = BlobServiceClient.from_connection_string(CONN_STR)
            blob = client.get_container_client(CONTAINER).get_blob_client(REFRESH_BLOB)
            return blob.download_blob().readall().decode("utf-8").strip()
        except Exception as e:
            print(f"  [azure] refresh token blob not found: {e}")
    token = os.environ.get("STRAVA_REFRESH_TOKEN", "").strip()
    if token:
        print("  [strava] using STRAVA_REFRESH_TOKEN env var (bootstrap)")
        return token
    return None


def save_refresh_token(token: str) -> None:
    if not (BLOB_AVAILABLE and CONN_STR):
        return
    try:
        client = BlobServiceClient.from_connection_string(CONN_STR)
        blob = client.get_container_client(CONTAINER).get_blob_client(REFRESH_BLOB)
        blob.upload_blob(token.encode("utf-8"), overwrite=True)
        print("  [azure] refresh token blob updated")
    except Exception as e:
        print(f"  [azure] failed to save refresh token: {e}")

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


def get_cursor(table) -> int | None:
    """Return Unix epoch of the most recently stored Strava activity."""
    try:
        entity = table.get_entity(partition_key="meta", row_key="strava_cursor")
        return int(entity["last_epoch"])
    except ResourceNotFoundError:
        return None
    except Exception as e:
        print(f"  [table] could not read cursor: {e}")
        return None


def save_cursor(table, epoch: int) -> None:
    table.upsert_entity({
        "PartitionKey": "meta",
        "RowKey":       "strava_cursor",
        "last_epoch":   epoch,
    })


def upsert_activities(table, records: list[dict]) -> None:
    """Upsert activity records into the table."""
    for r in records:
        entity = {
            "PartitionKey":  "strava",
            "RowKey":        r["activity_id"],
            "source":        "strava",
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
    """Return all Strava activity records from the table as plain dicts."""
    records = []
    for e in table.query_entities("PartitionKey eq 'strava'"):
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

# ── Strava auth ────────────────────────────────────────────────────────────────

def get_access_token() -> str:
    """Exchange refresh token for a fresh access token; rotate refresh token."""
    if not CLIENT_ID or not CLIENT_SECRET:
        raise RuntimeError("STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET env vars are required.")
    refresh_token = load_refresh_token()
    if not refresh_token:
        raise RuntimeError(
            "No Strava refresh token found. Set STRAVA_REFRESH_TOKEN env var on first run.\n"
            "Obtain one via the Strava OAuth authorisation flow:\n"
            f"  https://www.strava.com/oauth/authorize?client_id={CLIENT_ID}"
            "&response_type=code&redirect_uri=http://localhost"
            "&approval_prompt=force&scope=activity:read_all"
        )
    resp = requests.post(TOKEN_URL, data={
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type":    "refresh_token",
        "refresh_token": refresh_token,
    }, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    save_refresh_token(data.get("refresh_token", refresh_token))
    return data["access_token"]

# ── activity parsing ────────────────────────────────────────────────────────────────

TYPE_MAP = {
    "run":               "running",
    "trailrun":          "running",
    "virtualrun":        "running",
    "ride":              "cycling",
    "mountainbikeride":  "cycling",
    "virtualride":       "cycling",
    "gravelride":        "cycling",
    "ebikeride":         "cycling",
    "walk":              "walking",
    "hike":              "hiking",
    "weighttraining":    "strength",
    "workout":           "strength",
    "swim":              "swimming",
    "yoga":              "yoga",
    "elliptical":        "elliptical",
}

def normalise_type(raw: str) -> str:
    return TYPE_MAP.get(raw.lower(), raw.lower())


def parse_activity(act: dict) -> dict | None:
    try:
        start_local = act.get("start_date_local", "")
        date = start_local[:10] if start_local else None
        if not date:
            return None

        # Epoch from UTC start_date for cursor tracking
        start_utc = act.get("start_date", start_local)
        try:
            epoch = int(datetime.fromisoformat(
                start_utc.replace("Z", "+00:00")
            ).timestamp())
        except Exception:
            epoch = 0

        atype    = normalise_type(act.get("sport_type") or act.get("type") or "other")
        dur_m    = round((act.get("moving_time") or 0) / 60, 1)
        dist_m   = act.get("distance") or 0
        dist_km  = round(dist_m / 1000, 2) if dist_m else 0
        calories = int(act.get("calories") or round((act.get("kilojoules") or 0) * 0.239))
        avg_hr   = int(act.get("average_heartrate") or 0)
        max_hr   = int(act.get("max_heartrate") or 0)
        elev_m   = int(act.get("total_elevation_gain") or 0)

        return {
            "activity_id":  str(act["id"]),
            "source":       "strava",
            "start_epoch":  epoch,
            "date":         date,
            "type":         atype,
            "duration_min": dur_m,
            "distance_km":  dist_km,
            "calories":     calories,
            "avg_hr":       avg_hr,
            "max_hr":       max_hr,
            "elevation_m":  elev_m,
        }
    except Exception as e:
        print(f"  [parse] skipping activity {act.get('id')}: {e}")
        return None


def dedup_by_date(records: list[dict]) -> list[dict]:
    """Keep one record per date: the one with the highest calorie burn."""
    best: dict[str, dict] = {}
    for r in records:
        d = r["date"]
        if d not in best or r["calories"] > best[d]["calories"]:
            best[d] = r
    return sorted(best.values(), key=lambda r: r["date"])

# ── main ───────────────────────────────────────────────────────────────────────

def main():
    print("=== Strava Fetch ===")

    access_token = get_access_token()
    headers = {"Authorization": f"Bearer {access_token}"}

    table = get_table_client()

    # Incremental: only fetch activities newer than the stored cursor
    after_epoch = get_cursor(table) if table else None
    if after_epoch:
        print(f"  incremental fetch: activities after epoch {after_epoch} "
              f"({datetime.fromtimestamp(after_epoch, tz=timezone.utc).date()})")
    else:
        print(f"  full fetch: last {ACTIVITY_COUNT} activities")

    # Fetch from Strava API
    raw_activities: list[dict] = []
    page = 1
    max_to_fetch = None if after_epoch else ACTIVITY_COUNT
    while True:
        per_page = 200
        if max_to_fetch is not None:
            per_page = min(200, max_to_fetch - len(raw_activities))
            if per_page <= 0:
                break

        params: dict = {"per_page": per_page, "page": page}
        if after_epoch:
            params["after"] = after_epoch

        print(f"  fetching page {page} ({per_page} per page)…")
        resp = requests.get(ACTIVITIES_URL, headers=headers, params=params, timeout=30)
        if resp.status_code == 401:
            raise RuntimeError(
                "Strava API returned 401 Unauthorized. Your token likely lacks the "
                "'activity:read_all' scope.\n"
                "Re-authorise at:\n"
                f"  https://www.strava.com/oauth/authorize?client_id={CLIENT_ID}"
                "&response_type=code&redirect_uri=http://localhost"
                "&approval_prompt=force&scope=activity:read_all\n"
                "Then exchange the code for a refresh token and set STRAVA_REFRESH_TOKEN."
            )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        raw_activities.extend(batch)
        if len(batch) < per_page:
            break
        page += 1

    print(f"  received {len(raw_activities)} new activities")

    new_records = [r for a in raw_activities if (r := parse_activity(a))]

    if new_records and table:
        upsert_activities(table, new_records)
        max_epoch = max(r["start_epoch"] for r in new_records)
        save_cursor(table, max_epoch)
        print(f"  upserted {len(new_records)} records to table '{TABLE_NAME}'")

    # Build JSON from full table contents (always up to date)
    if table:
        all_records = load_all_from_table(table)
    else:
        all_records = new_records

    output = dedup_by_date(all_records)
    print(f"  → {len(output)} unique days in strava.json")

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
from pathlib import Path

# Load .env from repo root if present
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass

# Use Windows system trust store so corporate CA certs are trusted
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

# Build a CA bundle for curl/requests when behind a corporate proxy
def _build_system_ca_bundle() -> str | None:
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
    import requests
except ImportError:
    print("ERROR: requests not installed. Run: pip install requests")
    sys.exit(1)

try:
    from azure.storage.blob import BlobServiceClient
    AZURE_AVAILABLE = True
except ImportError:
    AZURE_AVAILABLE = False

# ── config ────────────────────────────────────────────────────────────────────
CLIENT_ID     = os.environ.get("STRAVA_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("STRAVA_CLIENT_SECRET", "")
CONN_STR      = os.environ.get("AZURE_STORAGE_CONNECTION_STRING", "")
CONTAINER     = os.environ.get("AZURE_CONTAINER_NAME", "healthdata")
REFRESH_BLOB  = os.environ.get("STRAVA_REFRESH_BLOB", "strava/refresh_token.txt")
ACTIVITY_COUNT = int(os.environ.get("STRAVA_ACTIVITY_COUNT", "200"))
OUT_PATH      = Path(__file__).parent.parent / "docs" / "strava.json"

TOKEN_URL       = "https://www.strava.com/oauth/token"
ACTIVITIES_URL  = "https://www.strava.com/api/v3/athlete/activities"

# ── Azure helpers ─────────────────────────────────────────────────────────────

def load_refresh_token() -> str | None:
    """Load Strava refresh token from Azure blob, or bootstrap from env var."""
    if AZURE_AVAILABLE and CONN_STR:
        try:
            client = BlobServiceClient.from_connection_string(CONN_STR)
            blob = client.get_container_client(CONTAINER).get_blob_client(REFRESH_BLOB)
            data = blob.download_blob().readall()
            return data.decode("utf-8").strip()
        except Exception as e:
            print(f"  [azure] refresh token blob not found: {e}")

    # Fall back to env var for first-time bootstrap
    token = os.environ.get("STRAVA_REFRESH_TOKEN", "").strip()
    if token:
        print("  [strava] using STRAVA_REFRESH_TOKEN env var (bootstrap)")
        return token
    return None


def save_refresh_token(token: str) -> None:
    """Persist Strava refresh token to Azure blob."""
    if not (AZURE_AVAILABLE and CONN_STR):
        return
    try:
        client = BlobServiceClient.from_connection_string(CONN_STR)
        blob = client.get_container_client(CONTAINER).get_blob_client(REFRESH_BLOB)
        blob.upload_blob(token.encode("utf-8"), overwrite=True)
        print("  [azure] refresh token blob updated")
    except Exception as e:
        print(f"  [azure] failed to save refresh token: {e}")

# ── Strava auth ───────────────────────────────────────────────────────────────

def get_access_token() -> str:
    """Exchange refresh token for a fresh access token; rotate refresh token."""
    if not CLIENT_ID or not CLIENT_SECRET:
        raise RuntimeError("STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET env vars are required.")

    refresh_token = load_refresh_token()
    if not refresh_token:
        raise RuntimeError(
            "No Strava refresh token found. Set STRAVA_REFRESH_TOKEN env var on first run.\n"
            "Obtain one via the Strava OAuth authorisation flow:\n"
            "  https://www.strava.com/oauth/authorize?client_id=<YOUR_CLIENT_ID>"
            "&response_type=code&redirect_uri=http://localhost"
            "&approval_prompt=force&scope=activity:read_all"
        )

    resp = requests.post(TOKEN_URL, data={
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type":    "refresh_token",
        "refresh_token": refresh_token,
    }, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    new_refresh = data.get("refresh_token", refresh_token)
    save_refresh_token(new_refresh)

    return data["access_token"]

# ── activity parsing ──────────────────────────────────────────────────────────

TYPE_MAP = {
    "run":               "running",
    "trailrun":          "running",
    "virtualrun":        "running",
    "ride":              "cycling",
    "mountainbikeride":  "cycling",
    "virtualride":       "cycling",
    "gravelride":        "cycling",
    "ebikeride":         "cycling",
    "walk":              "walking",
    "hike":              "hiking",
    "weighttraining":    "strength",
    "workout":           "strength",
    "swim":              "swimming",
    "yoga":              "yoga",
    "elliptical":        "elliptical",
}

def normalise_type(raw: str) -> str:
    return TYPE_MAP.get(raw.lower(), raw.lower())


def parse_activity(act: dict) -> dict | None:
    try:
        start = act.get("start_date_local", "")
        date = start[:10] if start else None
        if not date:
            return None

        atype = normalise_type(act.get("sport_type") or act.get("type") or "other")
        dur_m = round((act.get("moving_time") or 0) / 60, 1)

        dist_m = act.get("distance") or 0
        dist_km = round(dist_m / 1000, 2) if dist_m else 0

        # Strava list endpoint returns `calories`; fall back to kilojoules→kcal
        calories = int(act.get("calories") or round((act.get("kilojoules") or 0) * 0.239))
        avg_hr = int(act.get("average_heartrate") or 0)
        max_hr = int(act.get("max_heartrate") or 0)
        elev_m = int(act.get("total_elevation_gain") or 0)

        return {
            "date":         date,
            "type":         atype,
            "duration_min": dur_m,
            "distance_km":  dist_km,
            "calories":     calories,
            "avg_hr":       avg_hr,
            "max_hr":       max_hr,
            "elevation_m":  elev_m,
        }
    except Exception as e:
        print(f"  [parse] skipping activity {act.get('id')}: {e}")
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
    print("=== Strava Fetch ===")

    access_token = get_access_token()
    headers = {"Authorization": f"Bearer {access_token}"}

    # Fetch up to ACTIVITY_COUNT activities (max 200 per page; paginate if needed)
    raw_activities: list[dict] = []
    page = 1
    remaining = ACTIVITY_COUNT
    while remaining > 0:
        per_page = min(remaining, 200)
        print(f"  fetching page {page} ({per_page} activities)…")
        resp = requests.get(
            ACTIVITIES_URL,
            headers=headers,
            params={"per_page": per_page, "page": page},
            timeout=30,
        )
        if resp.status_code == 401:
            raise RuntimeError(
                "Strava API returned 401 Unauthorized. Your token likely lacks the "
                "'activity:read_all' scope.\n"
                "Re-authorise at:\n"
                f"  https://www.strava.com/oauth/authorize?client_id={CLIENT_ID}"
                "&response_type=code&redirect_uri=http://localhost"
                "&approval_prompt=force&scope=activity:read_all\n"
                "Then exchange the code for a refresh token and set STRAVA_REFRESH_TOKEN."
            )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        raw_activities.extend(batch)
        remaining -= len(batch)
        if len(batch) < per_page:
            break
        page += 1

    print(f"  received {len(raw_activities)} activities")

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

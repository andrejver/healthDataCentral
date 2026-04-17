#!/usr/bin/env python3
"""
Fetch Garmin workout data and write docs/garmin.json.

Auth: garminconnect uses OAuth1/2 tokens stored as a session blob in Azure Blob
Storage (same container as the Withings token). On first run, email + password
env vars are used; the resulting token blob is saved back to Azure.

Required env vars (set in Claude Code Routine secrets):
  GARMIN_EMAIL
  GARMIN_PASSWORD
  AZURE_STORAGE_CONNECTION_STRING   (same as fetch_and_build.py)
  AZURE_CONTAINER_NAME              (same as fetch_and_build.py)

Optional:
  GARMIN_SESSION_BLOB   (default: garmin/session.json)
  GARMIN_ACTIVITY_COUNT (default: 200)
"""

import json
import os
import sys
import time
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
    from garminconnect import Garmin, GarminConnectAuthenticationError, GarminConnectConnectionError
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

    def prompt_mfa():
        return input("  [garmin] Enter MFA/2FA code: ").strip()

    api = Garmin(EMAIL, PASSWORD, prompt_mfa=prompt_mfa)

    token_str = load_session_blob()
    if token_str:
        try:
            api.login(tokenstore=token_str)
            print("  [garmin] session restored from Azure blob")
            return api
        except Exception as e:
            print(f"  [garmin] session restore failed ({e}), re-authenticating…")

    # Full login
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

# ── retry helper ─────────────────────────────────────────────────────────────

def fetch_activities_with_retry(api: Garmin, count: int, max_attempts: int = 4) -> list:
    """Call api.get_activities with exponential backoff on transient errors."""
    delay = 5
    for attempt in range(1, max_attempts + 1):
        try:
            return api.get_activities(0, count)
        except GarminConnectConnectionError as e:
            if attempt == max_attempts:
                raise
            print(f"  [garmin] transient error (attempt {attempt}/{max_attempts}): {e}")
            print(f"  [garmin] retrying in {delay}s…")
            time.sleep(delay)
            delay *= 2
    return []  # unreachable


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    print("=== Garmin Fetch ===")

    api = get_garmin_client()

    print(f"  fetching last {ACTIVITY_COUNT} activities…")
    raw_activities = fetch_activities_with_retry(api, ACTIVITY_COUNT)
    print(f"  received {len(raw_activities)} activities")

    # Rotate session token after successful API call
    try:
        save_session_blob(api.get_token_dict())
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

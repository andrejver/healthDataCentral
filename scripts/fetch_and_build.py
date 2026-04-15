"""
Fetch Withings measurements and write docs/data.json.

Designed to run as a Claude Code scheduled Routine on Anthropic-managed
cloud infrastructure.

The Withings refresh token is stored in Azure Key Vault as the secret
"withings-refresh-token". On every run the token is rotated (Withings
invalidates the old one on each refresh) and the new value is written
back to Key Vault — nothing sensitive is committed to the repository.

Required environment variables (set in the Routine's environment config):
    WITHINGS_CLIENT_ID
    WITHINGS_CLIENT_SECRET
    AZURE_KEYVAULT_URL       e.g. https://my-vault.vault.azure.net/
    AZURE_TENANT_ID
    AZURE_CLIENT_ID
    AZURE_CLIENT_SECRET
"""

import json
import os
import time
import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv
from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CLIENT_ID = os.environ["WITHINGS_CLIENT_ID"]
CLIENT_SECRET = os.environ["WITHINGS_CLIENT_SECRET"]
KEYVAULT_URL = os.environ["AZURE_KEYVAULT_URL"]
SECRET_NAME = "withings-refresh-token"

TOKEN_URL = "https://wbsapi.withings.net/v2/oauth2"
MEASURE_URL = "https://wbsapi.withings.net/measure"

MEAS_WEIGHT = 1    # kg
MEAS_FAT_PCT = 6   # %
MEAS_MUSCLE = 76   # kg

TWO_YEARS_SECS = 2 * 365 * 24 * 3600
DATA_PATH = Path(__file__).parent.parent / "docs" / "data.json"

# ---------------------------------------------------------------------------
# Azure Key Vault helpers
# ---------------------------------------------------------------------------

def _kv_client() -> SecretClient:
    # DefaultAzureCredential picks up AZURE_TENANT_ID / AZURE_CLIENT_ID /
    # AZURE_CLIENT_SECRET automatically, both locally and in the Routine.
    return SecretClient(vault_url=KEYVAULT_URL, credential=DefaultAzureCredential())


def read_refresh_token() -> str:
    client = _kv_client()
    return client.get_secret(SECRET_NAME).value


def write_refresh_token(token: str) -> None:
    client = _kv_client()
    client.set_secret(SECRET_NAME, token)

# ---------------------------------------------------------------------------
# Token refresh
# ---------------------------------------------------------------------------

def refresh_access_token(refresh_token: str) -> tuple[str, str]:
    """Return (access_token, new_refresh_token). Withings rotates on every use."""
    resp = requests.post(
        TOKEN_URL,
        data={
            "action": "requesttoken",
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
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

def fetch_measurements(access_token: str) -> list[dict]:
    startdate = int(time.time()) - TWO_YEARS_SECS
    resp = requests.get(
        MEASURE_URL,
        params={
            "action": "getmeas",
            "meastypes": f"{MEAS_WEIGHT},{MEAS_FAT_PCT},{MEAS_MUSCLE}",
            "category": 1,
            "startdate": startdate,
        },
        headers={"Authorization": f"Bearer {access_token}"},
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get("status") != 0:
        raise RuntimeError(f"Measure fetch failed: {body}")
    return body["body"]["measuregrps"]


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
    print("Reading refresh token from Azure Key Vault...")
    refresh_token = read_refresh_token()

    print("Refreshing Withings access token...")
    access_token, new_refresh_token = refresh_access_token(refresh_token)

    print("Writing rotated refresh token back to Key Vault...")
    write_refresh_token(new_refresh_token)
    print("  Token rotated successfully.")

    print("Fetching measurements...")
    groups = fetch_measurements(access_token)
    records = parse_groups(groups)
    print(f"  {len(records)} date entries fetched.")

    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(json.dumps(records, indent=2))
    print(f"Written: {DATA_PATH}")


if __name__ == "__main__":
    main()

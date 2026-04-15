"""
Fetch Withings measurements and write docs/data.json.

Runs in GitHub Actions. Expects the following environment variables /
GitHub secrets to be set:
    WITHINGS_CLIENT_ID
    WITHINGS_CLIENT_SECRET
    WITHINGS_REFRESH_TOKEN
    GH_PAT            — fine-grained PAT with Secrets:write + Contents:write
    GITHUB_REPOSITORY — provided automatically by Actions (owner/repo)
"""

import base64
import json
import os
import time
from pathlib import Path

import requests
from nacl import encoding, public

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CLIENT_ID = os.environ["WITHINGS_CLIENT_ID"]
CLIENT_SECRET = os.environ["WITHINGS_CLIENT_SECRET"]
REFRESH_TOKEN = os.environ["WITHINGS_REFRESH_TOKEN"]
GH_PAT = os.environ["GH_PAT"]
GITHUB_REPO = os.environ["GITHUB_REPOSITORY"]  # "owner/repo"

TOKEN_URL = "https://wbsapi.withings.net/v2/oauth2"
MEASURE_URL = "https://wbsapi.withings.net/measure"

# Withings measure type IDs
MEAS_WEIGHT = 1      # kg
MEAS_FAT_PCT = 6     # %
MEAS_MUSCLE = 76     # kg

TWO_YEARS_SECS = 2 * 365 * 24 * 3600
DATA_PATH = Path(__file__).parent.parent / "docs" / "data.json"

# ---------------------------------------------------------------------------
# Token refresh — Withings rotates the refresh token on every call
# ---------------------------------------------------------------------------

def refresh_access_token(refresh_token: str) -> tuple[str, str]:
    """Return (access_token, new_refresh_token)."""
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
# Update the GitHub Actions secret so the next run uses the rotated token
# ---------------------------------------------------------------------------

def _get_repo_public_key(headers: dict) -> tuple[str, str]:
    url = f"https://api.github.com/repos/{GITHUB_REPO}/actions/secrets/public-key"
    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    data = resp.json()
    return data["key_id"], data["key"]


def _encrypt_secret(public_key_b64: str, secret_value: str) -> str:
    pub_key = public.PublicKey(
        base64.b64decode(public_key_b64), encoding.RawEncoder
    )
    sealed = public.SealedBox(pub_key).encrypt(secret_value.encode())
    return base64.b64encode(sealed).decode()


def update_github_secret(new_refresh_token: str) -> None:
    headers = {
        "Authorization": f"Bearer {GH_PAT}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    key_id, pub_key = _get_repo_public_key(headers)
    encrypted = _encrypt_secret(pub_key, new_refresh_token)

    url = (
        f"https://api.github.com/repos/{GITHUB_REPO}"
        "/actions/secrets/WITHINGS_REFRESH_TOKEN"
    )
    resp = requests.put(
        url,
        headers=headers,
        json={"encrypted_value": encrypted, "key_id": key_id},
    )
    resp.raise_for_status()
    print("WITHINGS_REFRESH_TOKEN secret updated successfully.")


# ---------------------------------------------------------------------------
# Measurements fetch
# ---------------------------------------------------------------------------

def fetch_measurements(access_token: str) -> list[dict]:
    startdate = int(time.time()) - TWO_YEARS_SECS
    params = {
        "action": "getmeas",
        "meastypes": f"{MEAS_WEIGHT},{MEAS_FAT_PCT},{MEAS_MUSCLE}",
        "category": 1,
        "startdate": startdate,
    }
    headers = {"Authorization": f"Bearer {access_token}"}
    resp = requests.get(MEASURE_URL, params=params, headers=headers)
    resp.raise_for_status()
    body = resp.json()
    if body.get("status") != 0:
        raise RuntimeError(f"Measure fetch failed: {body}")
    return body["body"]["measuregrps"]


def _decode_value(value: int, unit: int) -> float:
    return value * (10 ** unit)


def parse_groups(groups: list[dict]) -> list[dict]:
    """Convert raw measure groups to clean dicts, one per date."""
    by_date: dict[str, dict] = {}

    for grp in groups:
        date_str = _ts_to_date(grp["date"])
        row = by_date.setdefault(date_str, {"date": date_str})
        for m in grp["measures"]:
            val = _decode_value(m["value"], m["unit"])
            if m["type"] == MEAS_WEIGHT:
                row["weight"] = round(val, 2)
            elif m["type"] == MEAS_FAT_PCT:
                row["fat_pct"] = round(val, 2)
            elif m["type"] == MEAS_MUSCLE:
                row["muscle_kg"] = round(val, 2)

    # Sort ascending by date
    return sorted(by_date.values(), key=lambda r: r["date"])


def _ts_to_date(ts: int) -> str:
    import datetime
    return datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("Refreshing Withings access token...")
    access_token, new_refresh_token = refresh_access_token(REFRESH_TOKEN)

    print("Updating rotated refresh token in GitHub secrets...")
    update_github_secret(new_refresh_token)

    print("Fetching measurements...")
    groups = fetch_measurements(access_token)
    records = parse_groups(groups)
    print(f"  {len(records)} date entries fetched.")

    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(json.dumps(records, indent=2))
    print(f"Written to {DATA_PATH}")


if __name__ == "__main__":
    main()

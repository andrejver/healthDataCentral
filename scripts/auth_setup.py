"""
One-time local OAuth setup for Withings API.

Run this script once to obtain a refresh token and seed it into
Azure Key Vault. After that the Claude Code Routine takes over,
rotating the token automatically on every run.

Usage:
    pip install -r requirements.txt
    # Fill in .env (see README for required keys)
    python scripts/auth_setup.py
"""

import os
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
from dotenv import load_dotenv
from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient

load_dotenv()

CLIENT_ID = os.environ["CLIENT_ID"]
CLIENT_SECRET = os.environ["CLIENT_SECRET"]
KEYVAULT_URL = os.environ["AZURE_KEYVAULT_URL"]
SECRET_NAME = "withings-refresh-token"

REDIRECT_URI = "http://localhost:8080"
AUTH_URL = "https://account.withings.com/oauth2_user/authorize2"
TOKEN_URL = "https://wbsapi.withings.net/v2/oauth2"

_auth_code: str | None = None


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global _auth_code
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        if "code" in params:
            _auth_code = params["code"][0]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<h2>Authorised! You can close this tab.</h2>")
        else:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"<h2>Missing code parameter.</h2>")

    def log_message(self, *_):
        pass


def get_auth_code() -> str:
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": "user.metrics",
        "state": "withings_auth",
    }
    url = AUTH_URL + "?" + urllib.parse.urlencode(params)
    print(f"Opening browser for authorisation:\n  {url}\n")
    webbrowser.open(url)

    server = HTTPServer(("localhost", 8080), _CallbackHandler)
    print("Waiting for callback on http://localhost:8080 ...")
    server.handle_request()
    if _auth_code is None:
        raise RuntimeError("Did not receive an authorisation code.")
    return _auth_code


def exchange_code(code: str) -> dict:
    resp = requests.post(
        TOKEN_URL,
        data={
            "action": "requesttoken",
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "code": code,
            "redirect_uri": REDIRECT_URI,
        },
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get("status") != 0:
        raise RuntimeError(f"Token exchange failed: {body}")
    return body["body"]


def seed_key_vault(refresh_token: str) -> None:
    # Uses AZURE_TENANT_ID / AZURE_CLIENT_ID / AZURE_CLIENT_SECRET from .env,
    # or falls back to `az login` credentials if those aren't set.
    client = SecretClient(vault_url=KEYVAULT_URL, credential=DefaultAzureCredential())
    client.set_secret(SECRET_NAME, refresh_token)
    print(f"Refresh token stored in Key Vault as '{SECRET_NAME}'.")


def main():
    code = get_auth_code()
    print("\nExchanging authorisation code for tokens...")
    tokens = exchange_code(code)

    refresh_token = tokens["refresh_token"]

    print("Seeding refresh token into Azure Key Vault...")
    seed_key_vault(refresh_token)

    print("\n" + "=" * 60)
    print("SUCCESS — token is in Key Vault. Repo is clean.")
    print("Set up the Claude Code Routine (see README) and you're done.")
    print("=" * 60)
    print(f"\nAccess token (valid ~3 hours, for manual testing):")
    print(f"  {tokens['access_token']}\n")


if __name__ == "__main__":
    main()

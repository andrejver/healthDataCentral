# Health Dashboard

A personal health dashboard combining **Withings** body-composition data
(weight, body fat %, muscle mass) with **Garmin** workout data (runs, rides,
strength sessions) into a single mobile-first static site with 4 colour themes
and trend insights.

Data is refreshed daily by a **Claude Code scheduled Routine** running on
Anthropic-managed cloud infrastructure. All tokens are stored in
**Azure Blob Storage** — nothing sensitive ever touches the repository.

## Architecture

```
Your machine (once)
  auth_setup.py  →  Withings OAuth flow  →  seeds token into Azure Blob Storage

Claude Code Routine (daily, Anthropic cloud)
  fetch_and_build.py
    ├─ reads  Azure Blob Storage → withings/refresh_token.txt
    ├─ refreshes Withings access token (rotated on every call)
    ├─ writes new token back → Azure Blob Storage
    ├─ fetches measurements (weight / fat % / muscle mass)
    └─ writes docs/data.json

  fetch_garmin.py
    ├─ reads  Azure Blob Storage → garmin/session.json  (OAuth1/2 token blob)
    ├─ authenticates with Garmin Connect (restores session or full login)
    ├─ writes updated session blob back → Azure Blob Storage
    ├─ fetches last 200 activities, deduplicates by date
    └─ writes docs/garmin.json

  git commit & push  →  docs/data.json + docs/garmin.json

GitHub Pages  →  serves docs/index.html + docs/data.json + docs/garmin.json
```

The dashboard loads both files with `Promise.allSettled` — if `garmin.json` is
absent or fails, the Withings view renders fully and Garmin sections show a
"Connect Garmin" placeholder.

---

## Setup

### 1. Clone and install

```bash
git clone https://github.com/<you>/healthDataCentral.git
cd healthDataCentral
pip install -r requirements.txt
```

`requirements.txt` includes: `requests`, `python-dotenv`, `azure-storage-blob`,
`garminconnect`.

---

### 2. Create a Withings developer app

1. Go to <https://developer.withings.com/> → create an app.
2. Set the callback URL to `http://localhost:8080`.
3. Note your **Client ID** and **Client Secret**.

---

### 3. Create an Azure Storage account

```bash
# Create a resource group (skip if you have one already)
az group create --name health-rg --location westeurope

az storage account create \
  --name <your-storage-name> \
  --resource-group health-rg \
  --sku Standard_LRS \
  --allow-blob-public-access false
```

```bash
# Get the connection string
az storage account show-connection-string \
  --name <your-storage-name> \
  --resource-group health-rg \
  --query connectionString \
  --output tsv
```

Copy the output — it starts with `DefaultEndpointsProtocol=https;...`.

Alternatively: **Azure Portal → Storage account → Access keys → Connection string**.

---

### 4. Create `.env` (local only — never committed)

```
# Withings
CLIENT_ID=<withings-client-id>
CLIENT_SECRET=<withings-client-secret>

# Azure (shared by both fetch scripts)
AZURE_STORAGE_CONNECTION_STRING=<connection-string-from-step-3>
AZURE_CONTAINER_NAME=healthdata

# Garmin (only needed for fetch_garmin.py)
GARMIN_EMAIL=<your-garmin-email>
GARMIN_PASSWORD=<your-garmin-password>
```

> **Tip:** `AZURE_CONTAINER_NAME` defaults to `healthdata`. The Withings token
> lives at `withings/refresh_token.txt` and the Garmin session at
> `garmin/session.json` inside that container.

---

### 5. Run the one-time Withings auth flow

```bash
python scripts/auth_setup.py
```

Your browser opens to Withings. After authorising, the script exchanges the
code for tokens, creates the Azure container if needed, and uploads the refresh
token. The repository stays clean — no token files in git.

---

### 6. Seed the Garmin session (first-time)

`fetch_garmin.py` handles auth automatically on first run — it logs in with
email + password and saves the resulting session blob to Azure. Just make sure
`GARMIN_EMAIL` and `GARMIN_PASSWORD` are set:

```bash
python scripts/fetch_garmin.py
```

On success you'll see `docs/garmin.json` created. On subsequent runs the saved
session is restored from Azure (no password re-entry needed) and rotated after
each successful API call.

> **Garmin 2FA:** If your Garmin account has MFA enabled, the `garminconnect`
> library will prompt for the one-time code interactively on the first login.
> This only happens once locally — the saved session takes over afterward.

---

### 7. Enable GitHub Pages

In **Settings → Pages**, set source to **Deploy from a branch**, branch `main`,
folder `/docs`.

---

### 8. Create the Claude Code Routine

Go to **claude.ai/code/routines** → **New routine** and configure:

| Field | Value |
|---|---|
| **Name** | `health-dashboard-refresh` |
| **Schedule** | Daily (pick a time after midnight in your timezone) |
| **Repository** | this repo — **enable "Allow unrestricted branch pushes"** |
| **Setup script** | `pip install requests python-dotenv azure-storage-blob garminconnect` |
| **Prompt** | `Run bash scripts/run_routine.sh` |

> **"Allow unrestricted branch pushes"** lets the Routine commit directly to
> `main` instead of opening a PR. This is required for the daily data update.

#### Environment variables

Set these in the Routine's **Environment** tab:

| Variable | Value |
|---|---|
| `WITHINGS_CLIENT_ID` | your Withings client ID |
| `WITHINGS_CLIENT_SECRET` | your Withings client secret |
| `AZURE_STORAGE_CONNECTION_STRING` | connection string from step 3 |
| `AZURE_CONTAINER_NAME` | `healthdata` |
| `GARMIN_EMAIL` | your Garmin Connect email |
| `GARMIN_PASSWORD` | your Garmin Connect password |

> `GARMIN_EMAIL` and `GARMIN_PASSWORD` are only used on the very first Routine
> run (or if the Azure session blob is lost). After that the saved session is
> reused and rotated automatically.

---

### 9. Updating the Routine prompt

If you ever need to change what the Routine does (e.g. add a new script, change
the schedule logic, or test a change):

1. Go to **claude.ai/code/routines** → find `health-dashboard-refresh` → **Edit**.
2. Change the **Prompt** field. The current prompt is simply:
   ```
   Run bash scripts/run_routine.sh
   ```
   You can extend it, for example:
   ```
   Run bash scripts/run_routine.sh
   If either fetch script fails, print a clear error but do not abort — let the other script run.
   ```
3. To change the **schedule**, update the **Schedule** dropdown (daily, hourly, or a cron expression).
4. To add new **environment variables**, open the **Environment** tab and add the key/value pairs.
5. Click **Save** — the next scheduled run picks up the new config immediately.
6. Hit **Run now** to test the updated config without waiting for the next schedule tick.

> Changes to `scripts/run_routine.sh` in the repository are picked up
> automatically on the next Routine run — you don't need to edit the Routine
> itself for script-level changes, only for prompt/env/schedule changes.

---

### 10. Trigger the Routine manually first

In **claude.ai/code/routines**, hit **Run now** to verify both Withings and
Garmin fetches succeed. Check the run log — Garmin failures are non-fatal
(the Routine prints a warning and continues with Withings-only data).

---

## Themes

The dashboard ships with 4 themes switchable via the 🎨 button in the header:

| Theme | Feel | Accent colour |
|---|---|---|
| **Dark** (default) | Deep charcoal, GitHub-inspired | Electric blue `#4f8ef7` |
| **Midnight** | Near-black navy, starfield | Violet `#a78bfa` |
| **Light** | Off-white, clean clinical | Teal `#0d9488` |
| **Amber** | Warm dark, paper/sepia | Amber `#f59e0b` |

Theme preference is saved to `localStorage` and applied before first paint
(no flash of wrong theme).

---

## Files

```
healthDataCentral/
├── scripts/
│   ├── auth_setup.py          # one-time local Withings OAuth + Azure seed
│   ├── fetch_and_build.py     # Withings fetch, token rotation, writes data.json
│   ├── fetch_garmin.py        # Garmin fetch, session rotation, writes garmin.json
│   └── run_routine.sh         # called by the Routine — runs both fetches + git push
├── docs/
│   ├── index.html             # dashboard (Chart.js, 4 themes, mobile-first)
│   ├── data.json              # generated by fetch_and_build.py — do not edit
│   └── garmin.json            # generated by fetch_garmin.py — do not edit
├── requirements.txt           # requests, python-dotenv, azure-storage-blob, garminconnect
├── PLAN.md                    # design decisions and architecture notes
└── README.md
```

---

## Troubleshooting

**Garmin fetch fails with `GarminConnectAuthenticationError`**
- Your saved session expired (rare — sessions last months). Delete
  `garmin/session.json` from Azure Blob Storage and run `fetch_garmin.py`
  locally once to re-seed it, then re-upload via the Routine.

**Garmin 2FA prompt blocks the Routine**
- Garmin's MFA challenge requires interactive input, which the Routine can't
  provide. Disable MFA on your Garmin account, or complete the initial login
  locally (which saves the session) before relying on the Routine.

**Dashboard shows "Connect Garmin" placeholder**
- `garmin.json` is missing or empty. Check the last Routine run log for a
  `[warn] Garmin fetch failed` message and resolve the underlying error.

**Withings token expired**
- Re-run `python scripts/auth_setup.py` locally to issue a fresh token pair
  and upload to Azure.

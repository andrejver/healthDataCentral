# Withings Dashboard

A personal health dashboard that pulls weight, body fat, and muscle mass data
from the Withings API and displays it as a static GitHub Pages site.

Data is refreshed daily via a GitHub Actions workflow. The Withings refresh
token rotates on every use; the workflow automatically writes the new token
back to the `WITHINGS_REFRESH_TOKEN` GitHub secret so it never goes stale.

## Setup

### 1. Clone and install

```bash
git clone https://github.com/<you>/withings-dashboard.git
cd withings-dashboard
pip install -r requirements.txt
```

### 2. Create a Withings developer app

1. Go to <https://developer.withings.com/> and create an account / app.
2. Set the callback URL to `http://localhost:8080`.
3. Note your **Client ID** and **Client Secret**.

### 3. Create `.env` for local auth

```
CLIENT_ID=your_withings_client_id
CLIENT_SECRET=your_withings_client_secret
```

### 4. Run the one-time auth flow

```bash
python scripts/auth_setup.py
```

Your browser will open to Withings. After authorising, the script prints a
**refresh token** — copy it.

### 5. Add GitHub secrets

In your repo go to **Settings → Secrets and variables → Actions** and add:

| Secret | Value |
|---|---|
| `WITHINGS_CLIENT_ID` | Your Withings client ID |
| `WITHINGS_CLIENT_SECRET` | Your Withings client secret |
| `WITHINGS_REFRESH_TOKEN` | The refresh token from step 4 |
| `GH_PAT` | A fine-grained PAT with **Secrets: write** + **Contents: write** scope on this repo |

### 6. Enable GitHub Pages

In **Settings → Pages**, set source to **Deploy from a branch**, branch
`main`, folder `/docs`.

### 7. Trigger the workflow

Go to **Actions → Update Withings data → Run workflow** to do a first run.
Once `docs/data.json` is committed, the Pages site will show your data.

## How it works

```
GitHub Actions (daily cron)
  └─ fetch_and_build.py
        ├─ Refreshes access token (Withings rotates refresh token each time)
        ├─ Writes new refresh token back to WITHINGS_REFRESH_TOKEN secret
        ├─ Fetches all measurements (weight / fat % / muscle mass)
        └─ Writes docs/data.json

docs/index.html (static, Chart.js via CDN)
  ├─ Reads data.json at page load
  ├─ Header stats: current weight, 7-day + 30-day deltas
  ├─ Weight chart with 7-day moving average overlay
  ├─ Body fat % chart
  ├─ Muscle mass chart
  └─ Date range selector: 30d / 90d / 1y / all
```

## Files

```
withings-dashboard/
├── .github/workflows/update.yml   # daily CI job
├── scripts/
│   ├── auth_setup.py              # one-time local OAuth flow
│   └── fetch_and_build.py        # runs in CI
├── docs/
│   ├── index.html                 # dashboard
│   └── data.json                  # generated — do not edit manually
├── requirements.txt
├── .gitignore
└── README.md
```

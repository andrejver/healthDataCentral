# Withings Dashboard

A personal health dashboard that pulls weight, body fat, and muscle mass data
from the Withings API and displays it as a static GitHub Pages site.

Data is refreshed daily by a **Claude Code scheduled Routine** running on
Anthropic-managed cloud infrastructure — nothing runs on your local machine
after the one-time setup, and no GitHub Actions are involved.

The Withings refresh token rotates on every use. The routine writes the new
token back to `tokens/refresh_token.txt` and commits it, so it stays
current automatically.

> **Important:** Keep this repository **private**. The `tokens/` directory
> contains a live Withings OAuth refresh token.

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

### 3. Create `.env`

```
CLIENT_ID=your_withings_client_id
CLIENT_SECRET=your_withings_client_secret
```

### 4. Run the one-time auth flow

```bash
python scripts/auth_setup.py
```

Your browser will open to Withings. After authorising, the script saves the
refresh token to `tokens/refresh_token.txt`. Commit and push that file:

```bash
git add tokens/refresh_token.txt
git commit -m "chore: seed Withings refresh token"
git push
```

### 5. Enable GitHub Pages

In **Settings → Pages**, set source to **Deploy from a branch**, branch
`main`, folder `/docs`.

### 6. Create the Claude Code Routine

Go to **claude.ai/code/routines** → **New routine** and configure:

| Field | Value |
|---|---|
| **Name** | `withings-dashboard-refresh` |
| **Schedule** | Daily (or your preferred frequency) |
| **Repository** | this repo, with **unrestricted branch pushes** enabled |
| **Environment variables** | `WITHINGS_CLIENT_ID`, `WITHINGS_CLIENT_SECRET` |
| **Setup script** | `pip install -r requirements.txt` |
| **Prompt** | see below |

**Routine prompt:**

```
Run `python scripts/fetch_and_build.py` to fetch the latest Withings
measurements and update docs/data.json.

Then commit any changed files (tokens/refresh_token.txt and docs/data.json)
directly to the main branch with the message "chore: update Withings data".
Push the commit.
```

### 7. Trigger the routine manually first

In claude.ai/code/routines, hit **Run now** to verify everything works.
Once `docs/data.json` is committed the Pages site will show your data.

## How it works

```
Your machine (once)
  auth_setup.py  →  saves tokens/refresh_token.txt  →  commit & push

Claude Code Routine (daily, Anthropic cloud)
  fetch_and_build.py
    ├─ reads  tokens/refresh_token.txt
    ├─ refreshes access token (Withings rotates the refresh token)
    ├─ writes new refresh token → tokens/refresh_token.txt
    ├─ fetches measurements (weight / fat % / muscle mass)
    └─ writes docs/data.json
  git commit & push (tokens/refresh_token.txt + docs/data.json)

GitHub Pages
  serves docs/index.html + docs/data.json  →  your dashboard
```

## Files

```
withings-dashboard/
├── scripts/
│   ├── auth_setup.py          # one-time local OAuth flow
│   └── fetch_and_build.py     # runs in Claude Code Routine
├── docs/
│   ├── index.html             # dashboard (Chart.js, no build step)
│   └── data.json              # generated — do not edit manually
├── tokens/
│   └── refresh_token.txt      # kept up-to-date by the routine
├── requirements.txt
├── .gitignore
└── README.md
```

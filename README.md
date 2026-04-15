# Withings Dashboard

A personal health dashboard that pulls weight, body fat, and muscle mass data
from the Withings API and displays it as a static GitHub Pages site.

Data is refreshed daily by a **Claude Code scheduled Routine** running on
Anthropic-managed cloud infrastructure. The Withings refresh token is stored
in **Azure Blob Storage** — nothing sensitive ever touches the repository.

## Architecture

```
Your machine (once)
  auth_setup.py  →  OAuth flow  →  seeds token into Azure Blob Storage

Claude Code Routine (daily, Anthropic cloud)
  fetch_and_build.py
    ├─ reads  Azure Blob Storage → withings/refresh_token.txt
    ├─ refreshes access token (Withings rotates the refresh token)
    ├─ writes new token back → Azure Blob Storage
    ├─ fetches measurements (weight / fat % / muscle mass)
    └─ writes docs/data.json
  git commit & push  →  docs/data.json only

GitHub Pages  →  serves docs/index.html + docs/data.json
```

---

## Setup

### 1. Clone and install

```bash
git clone https://github.com/<you>/withings-dashboard.git
cd withings-dashboard
pip install -r requirements.txt
```

### 2. Create a Withings developer app

1. Go to <https://developer.withings.com/> → create an app.
2. Set the callback URL to `http://localhost:8080`.
3. Note your **Client ID** and **Client Secret**.

### 3. Create an Azure Storage account

```bash
# Create a resource group (skip if you have one already)
az group create --name withings-rg --location westeurope

# Register the provider if not already done
az provider register --namespace Microsoft.Storage

# Create the storage account (name must be globally unique, 3-24 lowercase chars)
az storage account create \
  --name <your-storage-name> \
  --resource-group withings-rg \
  --sku Standard_LRS \
  --allow-blob-public-access false
```

### 4. Get the connection string

```bash
az storage account show-connection-string \
  --name <your-storage-name> \
  --resource-group withings-rg \
  --query connectionString \
  --output tsv
```

Copy the output — it starts with `DefaultEndpointsProtocol=https;...`.

Alternatively: **Azure Portal → Storage account → Access keys → Connection string** (either key1 or key2).

### 5. Create `.env` (local use only, never committed)

```
CLIENT_ID=<withings-client-id>
CLIENT_SECRET=<withings-client-secret>
AZURE_STORAGE_CONNECTION_STRING=<connection-string-from-step-4>
AZURE_STORAGE_CONTAINER=withings
```

### 6. Run the one-time auth flow

```bash
python scripts/auth_setup.py
```

Your browser opens to Withings. After authorising, the script exchanges
the code for tokens, creates the `withings` container if needed, and
uploads the refresh token as `refresh_token.txt`. The repository stays
clean — no token files, no secrets in git.

### 7. Enable GitHub Pages

In **Settings → Pages**, set source to **Deploy from a branch**, branch
`main`, folder `/docs`.

### 8. Create the Claude Code Routine

Go to **claude.ai/code/routines** → **New routine** and configure:

| Field | Value |
|---|---|
| **Name** | `withings-dashboard-refresh` |
| **Schedule** | Daily |
| **Repository** | this repo, with **unrestricted branch pushes** enabled |
| **Setup script** | `pip install -r requirements.txt` |

**Environment variables** (set in the Routine's environment config):

| Variable | Value |
|---|---|
| `WITHINGS_CLIENT_ID` | your Withings client ID |
| `WITHINGS_CLIENT_SECRET` | your Withings client secret |
| `AZURE_STORAGE_CONNECTION_STRING` | connection string from step 4 |
| `AZURE_STORAGE_CONTAINER` | `withings` |

**Routine prompt:**

```
Run `python scripts/fetch_and_build.py` to fetch the latest Withings
measurements and update docs/data.json.

Then commit docs/data.json to the main branch with the message
"chore: update Withings data" and push.
```

### 9. Trigger the routine manually first

In claude.ai/code/routines, hit **Run now** to verify everything works.

---

## Files

```
withings-dashboard/
├── scripts/
│   ├── auth_setup.py          # one-time local OAuth + Blob Storage seed
│   └── fetch_and_build.py     # runs in Claude Code Routine
├── docs/
│   ├── index.html             # dashboard (Chart.js, no build step)
│   └── data.json              # generated — do not edit manually
├── requirements.txt
├── .gitignore
└── README.md
```

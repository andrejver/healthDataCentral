# Withings Dashboard

A personal health dashboard that pulls weight, body fat, and muscle mass data
from the Withings API and displays it as a static GitHub Pages site.

Data is refreshed daily by a **Claude Code scheduled Routine** running on
Anthropic-managed cloud infrastructure. The Withings refresh token is stored
in **Azure Key Vault** — nothing sensitive ever touches the repository.

## Architecture

```
Your machine (once)
  auth_setup.py  →  OAuth flow  →  seeds token into Azure Key Vault

Claude Code Routine (daily, Anthropic cloud)
  fetch_and_build.py
    ├─ reads  Azure Key Vault → withings-refresh-token
    ├─ refreshes access token (Withings rotates the refresh token)
    ├─ writes new token back → Azure Key Vault
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

### 3. Create an Azure Key Vault

In the Azure Portal or CLI:

```bash
# Create a resource group (skip if you have one)
az group create --name withings-rg --location westeurope

# Create the vault (name must be globally unique)
az keyvault create \
  --name <your-vault-name> \
  --resource-group withings-rg \
  --location westeurope
```

Note the **Vault URI** shown at the end — it looks like
`https://<your-vault-name>.vault.azure.net/`.

### 4. Create a service principal

This is the identity the Routine (and local scripts) will use to access
the vault.

```bash
az ad sp create-for-rbac --name withings-dashboard-sp --skip-assignment
```

Save the output — you'll need `appId` (client ID), `password` (client
secret), and `tenant` (tenant ID).

### 5. Grant the service principal access to the vault

```bash
az keyvault set-policy \
  --name <your-vault-name> \
  --spn <appId-from-step-4> \
  --secret-permissions get set
```

### 6. Create `.env` (local use only, never committed)

```
CLIENT_ID=<withings-client-id>
CLIENT_SECRET=<withings-client-secret>
AZURE_KEYVAULT_URL=https://<your-vault-name>.vault.azure.net/
AZURE_TENANT_ID=<tenant-from-step-4>
AZURE_CLIENT_ID=<appId-from-step-4>
AZURE_CLIENT_SECRET=<password-from-step-4>
```

### 7. Run the one-time auth flow

```bash
python scripts/auth_setup.py
```

Your browser opens to Withings. After authorising, the script exchanges
the code for tokens and writes the refresh token directly into Key Vault.
The repository stays clean — no token files, no secrets in git.

### 8. Enable GitHub Pages

In **Settings → Pages**, set source to **Deploy from a branch**, branch
`main`, folder `/docs`.

### 9. Create the Claude Code Routine

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
| `AZURE_KEYVAULT_URL` | `https://<your-vault-name>.vault.azure.net/` |
| `AZURE_TENANT_ID` | from step 4 |
| `AZURE_CLIENT_ID` | `appId` from step 4 |
| `AZURE_CLIENT_SECRET` | `password` from step 4 |

**Routine prompt:**

```
Run `python scripts/fetch_and_build.py` to fetch the latest Withings
measurements and update docs/data.json.

Then commit docs/data.json to the main branch with the message
"chore: update Withings data" and push.
```

### 10. Trigger the routine manually first

In claude.ai/code/routines, hit **Run now** to verify everything works.

---

## Files

```
withings-dashboard/
├── scripts/
│   ├── auth_setup.py          # one-time local OAuth + Key Vault seed
│   └── fetch_and_build.py     # runs in Claude Code Routine
├── docs/
│   ├── index.html             # dashboard (Chart.js, no build step)
│   └── data.json              # generated — do not edit manually
├── requirements.txt
├── .gitignore
└── README.md
```

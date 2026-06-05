# FeatureRequests

Dashboard and automation tooling for **Purview for AI** feature intake across:
- CRM Feature Requests / Evidence
- ADO Technical Feedback (UAT)
- Engineering ADO linkage

## What this project contains

- **Dashboard pages** (HTML): feature requests, technical feedback, roadmap, actions, admin.
- **Refresh pipeline** (`refresh_dashboard.py`): pulls latest CRM/ADO data into dashboard pages.
- **Local API server** (`refresh_server.py`): powers refresh/sync/promote/link actions from the UI.
- **Automation scripts**: create/link/sync/update feature records and evidence across systems.

## Prerequisites

1. Python 3.10+
2. Azure CLI (`az`) authenticated to required tenants/subscriptions
3. Python packages used by scripts:
   - `openpyxl`
   - `requests`

## Install

### Mac

```bash
git clone <repo-url>
cd FeatureRequests
python3 -m venv .venv
source .venv/bin/activate
pip install openpyxl requests
```

### Windows

```powershell
git clone <repo-url>
cd FeatureRequests
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install openpyxl requests
```

If you prefer not to use a virtual environment, install the packages into your current Python environment instead:

```bash
pip install openpyxl requests
```

## Authentication

Most scripts use Azure CLI tokens:

```bash
az login
```

Optional tenant pinning:

```bash
export AZURE_TENANT_ID="<tenant-guid>"
```

## Quick start

From this folder:

```bash
cd FeatureRequests
python refresh_server.py
```

Server starts on `http://localhost:8765` and enables:
- page refresh (`/refresh`)
- ADO↔CRM sync (`/sync`)
- promote/link/update operations

## Main scripts

### Refresh / server
- `refresh_server.py` — local HTTP control plane for dashboard actions.
- `refresh_dashboard.py` — refresh all or specific pages.
  - Example:
    ```bash
    python refresh_dashboard.py
    python refresh_dashboard.py --page features
    ```
- `purview-auto-refresh.py` / `.sh` / `.ps1` — scheduled/automated refresh wrappers.

### CRM / ADO automation
- `create_fr_from_uat.py --ado-id <id>`  
  Create CRM Feature Request from UAT and sync evidence.
- `link_uat_to_fr.py --ado-id <id> --fr-guid <guid>`  
  Link UAT item to existing CRM Feature Request.
- `sync_ado_to_crm.py [--feature MFR-M365-xxxxx] [--dry-run]`  
  Sync child evidence from ADO into CRM.
- `promote_to_feature.py --feature-id <quickfeature-guid> [--existing-feature <fr-guid>]`  
  Promote field-submitted feature to CRM Feature Request.
- `update_uat_state.py --id <ado-id> [--state ...] [--substate ...] [--fr-guid ...]`  
  Update ADO state/substate and optionally mapped CRM status.
- `update_crm_status.py --fr-guid <guid> --status <code>`  
  Directly set CRM feature status code.
- `link_eng_to_fr.py --ado-id <id> --fr-guid <guid>`  
  Link engineering ADO item to CRM FR (`ems_tfsid` / `ems_tfsidurl`).

## Configuration

`purview-admin-config.json` controls product and area path defaults used by refresh logic:
- `productId`, `productName`
- `areaPath` / `areaPaths`
- `uatServiceName` / `uatServiceNames`

## Packaging for sharing

Use:

```bash
python package_sanitized.py
```

This creates a sanitized zip excluding local caches/logs and clearing embedded page datasets.

## Notes

- Generated/local files (cache, logs, `__pycache__`) should not be committed.
- `link_eng_to_fr.py` currently references `os.getenv(...)` but is missing `import os`.

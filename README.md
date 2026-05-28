# Purview for AI — Customer Adoption Dashboard

A live, interactive dashboard to monitor **Microsoft Purview for AI** (DSPM for AI) customer usage, policy adoption, and engagement metrics.

![Dashboard Preview](docs/preview.png)

## Features

- 📊 **Interactive Charts** — Click-to-filter on all charts (sessions, policies, segments, regions, licenses)
- 🔍 **Zoom & Pan** — Mouse wheel zoom + drag-to-pan on trend charts
- 🔄 **Auto-Refresh** — Configurable refresh intervals (30s / 1m / 5m)
- 📈 **12-Month Data Window** — Full year of telemetry data
- 🏢 **All Customers** — 9,000+ tenants with DSPM for AI policies
- 🎯 **KPI Cards** — Animated counters for key metrics
- 📋 **Sortable Table** — Sort by Sessions, E5 PAU, Policies, Segment, Region
- 🖱️ **Detail Panel** — Click any customer row for deep-dive (profile, policies, engagement)
- ⌨️ **Keyboard Shortcuts** — Esc to clear filters, Ctrl+R to refresh

## Quick Start

### Prerequisites

- Python 3.10+
- Azure CLI (`az login` required for Kusto authentication)
- Access to `cxedataplatformcluster.westus2.kusto.windows.net` (CxE Data Kusto cluster)

### Setup

```bash
# Clone the repo
git clone https://github.com/microsoft/PurviewforAICxETeam.git
cd PurviewforAICxETeam

# Install dependencies
cd backend
pip install -r requirements.txt

# Authenticate to Azure (required for Kusto access)
az login

# Start the server
python app.py
```

### Access the Dashboard

Open http://127.0.0.1:8050/ in your browser.

## Architecture

```
├── backend/
│   ├── app.py              # FastAPI backend (Kusto queries + API)
│   ├── requirements.txt    # Python dependencies
│   └── startup.sh          # Azure App Service startup script
├── purview-for-ai-dashboard.html   # Main interactive dashboard (self-contained)
└── README.md
```

### Backend API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /api/health` | Health check |
| `GET /api/summary` | Global KPIs (tenants, policies) |
| `GET /api/customers?top=500` | Customer table (sorted by sessions) |
| `GET /api/customer/{tenant_id}` | Single customer deep-dive |
| `GET /api/top-sessions?top=15` | Top customers by AI Hub sessions |
| `GET /api/policies` | Policy distribution |
| `GET /api/ui-components` | AI Hub UI engagement breakdown |
| `GET /api/licenses` | License distribution |
| `GET /api/profile` | Customer profile (segment, region, industry) |
| `GET /api/trends?days=365` | Tenant adoption trend |
| `GET /api/cache/clear` | Clear server-side cache |

### Data Sources (Kusto Tables)

- `DimTenant` — Customer profile & license data
- `Purview_FactAIHubEngagedAdmin` — DSPM for AI policy activity
- `Purview_FactAIHubEngagedUsers` — AI Hub user sessions
- `FactTenantPAUBySKU` — E5 PAU (Paid Available Units) per tenant

## Configuration

- **Cache TTL**: 5 minutes (server-side, configurable in `app.py`)
- **Data Window**: 12 months (`ago(365d)` in all queries)
- **Max Customers**: 5,000 per request (default 500)
- **Port**: 8050 (configurable in `app.py`)

## Team

Microsoft Purview for AI — CxE Team

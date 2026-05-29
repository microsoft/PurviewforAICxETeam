# Purview for AI — Power BI Dashboard

A Power BI Project (PBIP) that connects directly to the CxEData Kusto cluster and visualizes DSPM for AI customer adoption metrics.

## 📊 Dashboard Pages

| Page | Content |
|------|---------|
| **Overview** | KPI cards (Total Tenants, Sessions, M365 Copilot Seats, E5 PAU, AI Apps), Monthly Trends line chart, Top 15 Customers bar chart, Segment/Region/Industry donut charts |
| **Policies & AI Hub** | Active Policies bar chart, AI Hub Page Engagement bar chart, License Distribution clustered bar |
| **Customer Details** | Full sortable/filterable table with TenantName, TenantId, Sessions, E5 PAU, M365 Copilot, AI Apps, Agents, Policies, Segment, Region, Account Manager, CxE Contact (ATS), S500, CAT |

## 🚀 How to Open & Publish

### Prerequisites
- **Power BI Desktop** (June 2023 or later — PBIP format support required)
- **Azure CLI** logged in (`az login`) OR Power BI Kusto connector credentials
- Access to `cxedataplatformcluster.westus2.kusto.windows.net` / `cxedata` database

### Steps

1. **Open the project:**
   - Open Power BI Desktop
   - File → Open → Browse → select `PurviewForAI.pbip`

2. **Authenticate to Kusto:**
   - When prompted, sign in with your Microsoft (Azure AD) credentials
   - The data source is: `https://cxedataplatformcluster.westus2.kusto.windows.net`
   - Database: `cxedata`

3. **Refresh data:**
   - Click "Refresh" in the Home ribbon to pull latest data from Kusto
   - Initial load may take 1-2 minutes (12 months of data)

4. **Publish to Power BI Service:**
   - File → Publish → Publish to Power BI
   - Select your workspace (e.g., "Purview for AI Team")
   - Set up scheduled refresh (recommended: daily) via Gateway or cloud credentials

### Configuring Scheduled Refresh
1. In Power BI Service, go to Dataset Settings
2. Under "Data source credentials", configure Azure Data Explorer (Kusto) with OAuth2
3. Under "Scheduled refresh", set frequency (daily recommended)

## 📁 Project Structure

```
PurviewForAI.pbip                          # Root project file
PurviewForAI.SemanticModel/
  definition.pbism                          # Semantic model metadata
  definition/
    model.bim                               # Data model (tables, measures, Kusto queries)
PurviewForAI.Report/
  definition.pbir                           # Report metadata
  definition/
    report.json                             # Report pages & visual layout
```

## 📐 Data Model

### Tables & Sources (Kusto KQL)

| Table | Source | Description |
|-------|--------|-------------|
| **Customers** | Multi-join query | Main fact table: Sessions, Policies, E5 PAU, M365 Copilot, AI Apps/Agents, Segment, Region, Account Team |
| **Policies** | `Purview_FactAIHubEngagedAdmin` | Distinct DSPM for AI policy types & tenant counts |
| **UIComponents** | `Purview_FactAIHubEngagedUsers` | AI Hub page engagement (AllUp sessions by Page) |
| **Licenses** | `FactTenantPAUBySKU` | License SKU distribution (Copilot, E5, E3, MIP) |
| **Trends** | `Purview_FactAIHubEngagedUsers` | Monthly sessions & tenant counts (12 months) |
| **AIAppsAgents** | `Purview_FactAIHubEngagedUsers` | AI Apps & Agents interaction details by tenant |

### Key DAX Measures
- `Total Tenants` — Count of active DSPM tenants
- `Total Sessions` — Sum of AI Hub sessions
- `Total E5 PAU` — Sum of E5 paid active usage
- `Total M365 Copilot Seats` — Sum of Copilot paid seats
- `Total AI Apps` / `Total AI Agents` — Sum of AI workload sessions
- `Copilot Adoption %` — % of tenants with M365 Copilot
- `S500 Count` / `CAT Count` — Strategic customer metrics
- `Avg Sessions per Tenant` — Average engagement

## ⚠️ Excluded Tenants
The following internal/test tenants are excluded from all queries:
- ZAVA, ZAVA - PRIVATE, ONE MTC - PROD, P4AISDKFEB2025, XDR NINJAS

## 🔧 Customization

### Modify KQL Queries
Edit `PurviewForAI.SemanticModel/definition/model.bim` → find the `partitions` section for each table → update the KQL inside the M expression.

### Add New Measures
Add to the `measures` array in the Customers table definition in `model.bim`.

### Change Data Window
Find `ago(365d)` in the KQL queries and change to desired duration (e.g., `ago(90d)` for 3 months).

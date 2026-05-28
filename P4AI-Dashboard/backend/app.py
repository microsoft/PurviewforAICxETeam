"""
DSPM for AI Dashboard - FastAPI Backend
Queries Kusto cluster for live data and serves JSON to the dashboard.
Uses Azure CLI credentials for authentication.
"""

import json
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Optional

from azure.identity import AzureCliCredential
from azure.kusto.data import KustoClient, KustoConnectionStringBuilder
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="Purview for AI - Customer Adoption Dashboard API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

CLUSTER_URI = "https://cxedataplatformcluster.westus2.kusto.windows.net"
DATABASE = "cxedata"

_client: Optional[KustoClient] = None
_cache: dict = {}
_cache_expiry: dict = {}
CACHE_TTL_SECONDS = 300  # 5 minutes


def get_client() -> KustoClient:
    global _client
    if _client is None:
        credential = AzureCliCredential()
        kcsb = KustoConnectionStringBuilder.with_azure_token_credential(
            CLUSTER_URI, credential
        )
        _client = KustoClient(kcsb)
    return _client


def query_kusto(query: str) -> list[dict]:
    """Execute a KQL query and return results as list of dicts."""
    cache_key = query.strip()
    now = datetime.utcnow()

    if cache_key in _cache and _cache_expiry.get(cache_key, now) > now:
        return _cache[cache_key]

    client = get_client()
    response = client.execute(DATABASE, query)
    primary = response.primary_results[0]
    columns = [col.column_name for col in primary.columns]
    rows = []
    for row in primary:
        rows.append({col: row[col] for col in columns})

    _cache[cache_key] = rows
    _cache_expiry[cache_key] = now + timedelta(seconds=CACHE_TTL_SECONDS)
    return rows


@app.get("/api/health")
def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}


@app.get("/api/summary")
def get_summary():
    """Global KPIs: total tenants, total policy types (last 1 year)."""
    query = """
    let data = Purview_FactAIHubEngagedAdmin
        | where SnapshotDate >= ago(365d)
        | where Feature has "DSPM for AI";
    let totalTenants = data | summarize dcount(TenantId);
    let totalPolicies = data | summarize dcount(Feature);
    let latestDate = toscalar(data | summarize max(SnapshotDate));
    print TotalTenants=toscalar(totalTenants), TotalPolicies=toscalar(totalPolicies), SnapshotDate=latestDate
    """
    rows = query_kusto(query)
    result = rows[0] if rows else {}
    result["refreshed_at"] = datetime.utcnow().isoformat()
    return result


@app.get("/api/customers")
def get_customers(top: int = Query(default=500, le=5000)):
    """Top N customers by admin engagement with full details."""
    query = f"""
    let policyData = Purview_FactAIHubEngagedAdmin
        | where SnapshotDate >= ago(365d)
        | where Feature has "DSPM for AI"
        | summarize Policies=dcount(Feature), PolicyList=make_set(Feature) by TenantId;
    let sessions = Purview_FactAIHubEngagedUsers
        | where SnapshotDate >= ago(365d)
        | where Workload == "AiHub"
        | summarize Sessions=sum(AllUp) by TenantId;
    let e5pau = FactTenantPAUBySKU
        | where SnapshotDate == toscalar(FactTenantPAUBySKU | summarize max(SnapshotDate))
        | where SkuName has "E5"
        | summarize E5PAU=sum(EXO) by TenantId;
    let tenants = DimTenant
        | summarize arg_max(SnapshotDate, *) by TenantId
        | project TenantId, TenantName, CustomerSegmentGroup, RegionName, CountryName, IndustryName,
                  HasM365Copilot, HasMIP, HasDLP, IsDSPCAT;
    policyData
    | join kind=leftouter sessions on TenantId
    | join kind=leftouter e5pau on TenantId
    | join kind=leftouter tenants on TenantId
    | extend Sessions1 = iff(isnull(Sessions), tolong(0), Sessions)
    | extend E5PAU1 = iff(isnull(E5PAU), tolong(0), E5PAU)
    | project TenantId, Name=iff(isempty(TenantName), TenantId, TenantName),
              Policies, PolicyList, Sessions=Sessions1, E5PAU=E5PAU1,
              Segment=iff(isempty(CustomerSegmentGroup), "Unknown", CustomerSegmentGroup),
              Region=iff(isempty(RegionName), "Unknown", RegionName),
              Country=iff(isempty(CountryName), "Unknown", CountryName),
              Industry=iff(isempty(IndustryName), "Unknown", IndustryName),
              HasCopilot=HasM365Copilot, HasMIP=HasMIP, HasDLP=HasDLP, IsCAT=IsDSPCAT
    | order by Sessions desc
    | take {min(top, 5000)}
    """
    rows = query_kusto(query)
    return {"customers": rows, "count": len(rows), "refreshed_at": datetime.utcnow().isoformat()}


@app.get("/api/customer/{tenant_id}")
def get_customer_detail(tenant_id: str):
    """Detailed data for a single customer."""
    profile_query = f"""
    DimTenant
    | where TenantId == "{tenant_id}"
    | summarize arg_max(SnapshotDate, *) by TenantId
    | project TenantId, TenantName, CustomerSegmentGroup, SubSegmentName, RegionName, CountryName,
              IndustryName, HasM365Copilot, HasMIP, HasDLP, IsDSPCAT,
              HasMIPPremium, HasEndPointDLP
    """
    policies_query = f"""
    let latestDate = toscalar(Purview_FactAIHubEngagedAdmin | summarize max(SnapshotDate));
    Purview_FactAIHubEngagedAdmin
    | where SnapshotDate == latestDate
    | where TenantId == "{tenant_id}"
    | where Feature has "DSPM for AI"
    | project Feature
    | order by Feature asc
    """
    engagement_query = f"""
    let latestDate = toscalar(Purview_FactAIHubEngagedUsers | summarize max(SnapshotDate));
    Purview_FactAIHubEngagedUsers
    | where SnapshotDate == latestDate
    | where TenantId == "{tenant_id}"
    | where Workload == "AiHub"
    | summarize Sessions=sum(AllUp) by Page
    | order by Sessions desc
    """
    profile = query_kusto(profile_query)
    policies = query_kusto(policies_query)
    engagement = query_kusto(engagement_query)

    return {
        "profile": profile[0] if profile else {},
        "policies": policies,
        "engagement": engagement,
        "refreshed_at": datetime.utcnow().isoformat(),
    }


@app.get("/api/top-sessions")
def get_top_sessions(top: int = Query(default=15, le=50)):
    """Top customers by AI Hub AllUp sessions (last 1 year)."""
    query = f"""
    Purview_FactAIHubEngagedUsers
    | where SnapshotDate >= ago(365d)
    | where Workload == "AiHub"
    | summarize TotalSessions=sum(AllUp) by TenantId
    | join kind=leftouter (
        DimTenant | summarize arg_max(SnapshotDate, *) by TenantId | project TenantId, TenantName
    ) on TenantId
    | project TenantId, Name=coalesce(TenantName, TenantId), TotalSessions
    | order by TotalSessions desc
    | take {top}
    """
    rows = query_kusto(query)
    return {"customers": rows, "count": len(rows), "refreshed_at": datetime.utcnow().isoformat()}


@app.get("/api/policies")
def get_policies():
    """DSPM for AI active policy distribution (last 1 year)."""
    query = """
    Purview_FactAIHubEngagedAdmin
    | where SnapshotDate >= ago(365d)
    | where Feature has "DSPM for AI"
    | summarize Tenants=dcount(TenantId) by Feature
    | order by Tenants desc
    """
    rows = query_kusto(query)
    return {"policies": rows, "count": len(rows), "refreshed_at": datetime.utcnow().isoformat()}


@app.get("/api/ui-components")
def get_ui_components():
    """AI Hub UI component engagement breakdown (last 1 year)."""
    query = """
    Purview_FactAIHubEngagedUsers
    | where SnapshotDate >= ago(365d)
    | where Workload == "AiHub"
    | where Element != "AllUp" or Page != "AllUp"
    | summarize Sessions=sum(AllUp), Tenants=dcount(TenantId) by Page, ElementType
    | where Page != "AllUp"
    | summarize Sessions=sum(Sessions), Tenants=sum(Tenants) by Page
    | order by Sessions desc
    | take 12
    """
    rows = query_kusto(query)
    return {"components": rows, "count": len(rows), "refreshed_at": datetime.utcnow().isoformat()}


@app.get("/api/licenses")
def get_licenses():
    """License distribution across DSPM for AI tenants."""
    query = """
    let dspmTenants = Purview_FactAIHubEngagedAdmin
    | where Feature has "DSPM for AI"
    | distinct TenantId;
    DimTenant
    | where TenantId in (dspmTenants)
    | summarize arg_max(SnapshotDate, *) by TenantId
    | summarize
        HasCopilot=countif(HasM365Copilot == 1),
        HasMIP=countif(HasMIP == 1),
        HasDLP=countif(HasDLP == 1),
        HasMIPPremium=countif(HasMIPPremium == 1),
        HasEndPointDLP=countif(HasEndPointDLP == 1),
        HasTeamsDLP=countif(HasTeamsDLP == 1),
        HasCommunicationCompliance=countif(HasCommunicationCompliance == 1),
        Total=count()
    """
    rows = query_kusto(query)
    return {"licenses": rows[0] if rows else {}, "refreshed_at": datetime.utcnow().isoformat()}


@app.get("/api/profile")
def get_profile_breakdown():
    """Customer profile breakdown (segment, region, industry)."""
    seg_query = """
    let dspmTenants = Purview_FactAIHubEngagedAdmin | where Feature has "DSPM for AI" | distinct TenantId;
    DimTenant | where TenantId in (dspmTenants)
    | summarize arg_max(SnapshotDate, *) by TenantId
    | summarize Tenants=count() by CustomerSegmentGroup
    | order by Tenants desc
    """
    region_query = """
    let dspmTenants = Purview_FactAIHubEngagedAdmin | where Feature has "DSPM for AI" | distinct TenantId;
    DimTenant | where TenantId in (dspmTenants)
    | summarize arg_max(SnapshotDate, *) by TenantId
    | summarize Tenants=count() by RegionName
    | order by Tenants desc
    | take 15
    """
    industry_query = """
    let dspmTenants = Purview_FactAIHubEngagedAdmin | where Feature has "DSPM for AI" | distinct TenantId;
    DimTenant | where TenantId in (dspmTenants)
    | summarize arg_max(SnapshotDate, *) by TenantId
    | summarize Tenants=count() by IndustryName
    | order by Tenants desc
    | take 10
    """
    segments = query_kusto(seg_query)
    regions = query_kusto(region_query)
    industries = query_kusto(industry_query)
    return {
        "segments": segments,
        "regions": regions,
        "industries": industries,
        "refreshed_at": datetime.utcnow().isoformat(),
    }


@app.get("/api/trends")
def get_trends(days: int = Query(default=365, le=365)):
    """Tenant adoption trend over the last N days (default: 1 year)."""
    query = f"""
    Purview_FactAIHubEngagedAdmin
    | where Feature has "DSPM for AI"
    | where SnapshotDate >= ago({days}d)
    | summarize Tenants=dcount(TenantId) by SnapshotDate
    | order by SnapshotDate asc
    """
    rows = query_kusto(query)
    return {"trends": rows, "refreshed_at": datetime.utcnow().isoformat()}


@app.get("/api/cache/clear")
def clear_cache():
    """Force-clear the server-side cache."""
    _cache.clear()
    _cache_expiry.clear()
    return {"status": "cache_cleared", "timestamp": datetime.utcnow().isoformat()}


# Serve the dashboard HTML
@app.get("/")
def serve_dashboard():
    return FileResponse("../purview-for-ai-dashboard.html", media_type="text/html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8050)

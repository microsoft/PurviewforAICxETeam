#!/usr/bin/env python3
"""
Sync ADO evidence items to CRM Feature Evidence records for Purview for AI.

For each CRM feature linked to ADO (via UAT ID), this script:
1. Fetches ADO child items (evidence/feedback)
2. Checks CRM for existing evidence to avoid duplicates
3. Creates missing Feature Evidence records in CRM

Usage:
    python sync_ado_to_crm.py                    # Sync all linked features
    python sync_ado_to_crm.py --feature MFR-M365-38863  # Sync one feature
    python sync_ado_to_crm.py --dry-run           # Preview without creating
"""

import json
import subprocess
import sys
import re
import urllib.request
import urllib.parse
import urllib.error
import argparse
from datetime import datetime
import os


# ── Constants ──────────────────────────────────────────────────────────────────

CRM_BASE = "https://m365crm.crm.dynamics.com/api/data/v9.2"
ADO_BASE = "https://dev.azure.com/unifiedactiontracker/Technical%20Feedback/_apis"
PURVIEW_AI_PRODUCT_ID = "eee1947c-0ea7-ef11-8a69-6045bdee9a10"
AZURE_TENANT_ID = os.getenv("AZURE_TENANT_ID", "").strip()

# CRM option set mappings
REQUESTED_BY_CUSTOMER = 172430000
BLOCKING_ADOPTION = 172430002
BLOCKING_RETENTION = 500350000
PRIORITY_MAP_ADO_TO_CRM = {
    1: 172430000,   # P0-Must Fix ASAP
    2: 172430001,   # P1-Required for Rollout
    3: 172430002,   # P2-Nice to have
}
PRIORITY_DEFAULT = 500350000  # Not Identified Yet

# Feature-to-UAT mapping (CRM feature GUID → UAT ADO work item ID)
# Extracted from CRM ems_onelist field
# Feature ID → ADO work item ID (no hardcoded GUIDs — resolved live from CRM)
FEATURE_UAT_LINKS = {
    "MFR-M365-38863": 664383,
    "MFR-M365-38661": 727034,
    "MFR-M365-38399": 697453,
    "MFR-M365-36265": 609245,
    "MFR-M365-36268": 645717,
    "MFR-M365-36354": 631568,
    "MFR-M365-36355": 614396,
    "MFR-M365-36356": 528684,
    "MFR-M365-36420": 646707,
    "MFR-M365-36228": 589021,
    "MFR-M365-36230": 602390,
    "MFR-M365-35419": 591094,
    "MFR-M365-36963": 686428,
    "MFR-M365-36225": 631873,
    "MFR-M365-37290": 724670,
    "MFR-M365-36873": 574387,
    "MFR-M365-38343": 721144,
}


# ── Auth ───────────────────────────────────────────────────────────────────────

def resolve_feature_guid(crm_token, feature_id):
    """Look up the current CRM GUID for a feature ID (e.g. MFR-M365-38863)."""
    safe_id = feature_id.replace("'", "''")
    data = crm_get(crm_token, "ems_productfeatures",
                   {"$filter": f"ems_featureid eq '{safe_id}'",
                    "$select": "ems_productfeatureid,ems_featureid"})
    if data and data.get("value"):
        return data["value"][0]["ems_productfeatureid"]
    return None


def get_token(resource):
    """Get OAuth token via Azure CLI."""
    cmd = ["az", "account", "get-access-token", "--resource", resource, "--query", "accessToken", "-o", "tsv"]
    if AZURE_TENANT_ID:
        cmd.extend(["--tenant", AZURE_TENANT_ID])
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"❌ Failed to get token for {resource}: {result.stderr.strip()}")
        sys.exit(1)
    return result.stdout.strip()


# ── API Helpers ────────────────────────────────────────────────────────────────

def crm_get(token, path, params=None):
    """GET request to CRM API."""
    url = f"{CRM_BASE}/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "OData-MaxVersion": "4.0",
        "OData-Version": "4.0",
    })
    try:
        resp = urllib.request.urlopen(req)
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        print(f"  ⚠️  CRM GET error {e.code}: {body[:300]}")
        return None


def crm_post(token, path, data):
    """POST request to CRM API (create record)."""
    url = f"{CRM_BASE}/{path}"
    body = json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "OData-MaxVersion": "4.0",
        "OData-Version": "4.0",
        "Prefer": "return=representation",
    })
    try:
        resp = urllib.request.urlopen(req)
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        print(f"  ❌ CRM POST error {e.code}: {body[:500]}")
        return None


def crm_patch(token, path, data):
    """PATCH request to CRM API (update record)."""
    url = f"{CRM_BASE}/{path}"
    body = json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, method="PATCH", headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "OData-MaxVersion": "4.0",
        "OData-Version": "4.0",
    })
    try:
        urllib.request.urlopen(req)
        return True
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        print(f"  ❌ CRM PATCH error {e.code}: {body[:300]}")
        return False


def ado_get(token, path):
    """GET request to ADO API."""
    url = f"{ADO_BASE}/{path}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    })
    try:
        resp = urllib.request.urlopen(req)
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        print(f"  ⚠️  ADO GET error {e.code}: {body[:300]}")
        return None


def ado_post(token, path, data):
    """POST request to ADO API."""
    url = f"{ADO_BASE}/{path}"
    body = json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    try:
        resp = urllib.request.urlopen(req)
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        print(f"  ⚠️  ADO POST error {e.code}: {body[:300]}")
        return None


# ── Core Logic ─────────────────────────────────────────────────────────────────

def strip_html(text):
    """Remove HTML tags from text."""
    if not text:
        return ""
    return re.sub(r'<[^>]+>', '', text).strip()


def fetch_ado_children(ado_token, parent_id):
    """Fetch child evidence items for an ADO work item."""
    # Get relations
    data = ado_get(ado_token, f"/wit/workitems/{parent_id}?$expand=relations&api-version=7.0")
    if not data:
        return [], 1  # default priority

    parent_priority_num = data.get("fields", {}).get("Microsoft.VSTS.Common.Priority")
    customer_priority = data.get("fields", {}).get("Custom.CustomerPriorityNew", "")
    # Prefer Custom.CustomerPriorityNew if set, fall back to Microsoft.VSTS.Common.Priority
    if customer_priority:
        # Values like "0 – Must Fix ASAP", "1 – Required For Rollout", "2 – Nice to have"
        if customer_priority.startswith("0"):
            parent_priority = 1  # maps to P0
        elif customer_priority.startswith("1"):
            parent_priority = 2  # maps to P1
        elif customer_priority.startswith("2"):
            parent_priority = 3  # maps to P2
        else:
            parent_priority = 3
    elif parent_priority_num:
        parent_priority = parent_priority_num
    else:
        parent_priority = 3  # default

    children_ids = []
    for rel in data.get("relations", []):
        if "Hierarchy-Forward" in rel.get("rel", ""):
            wid = int(rel["url"].split("/")[-1])
            children_ids.append(wid)

    if not children_ids:
        return [], parent_priority

    # Batch fetch children
    batch_data = ado_post(ado_token, "/wit/workitemsbatch?api-version=7.0", {
        "ids": children_ids,
        "fields": [
            "Custom.Account", "Custom.Industry", "Custom.TPID",
            "Custom.TFTOptyName", "Custom.Blockertype",
            "Custom.TFTOpportunityIntent", "Custom.TFTOpportunityStage",
            "Custom.SalesPlay", "Custom.AreaField", "Custom.CustomerImpact",
        ]
    })

    children = []
    if batch_data and "value" in batch_data:
        for item in batch_data["value"]:
            f = item.get("fields", {})
            account = f.get("Custom.Account", "")
            if not account:
                continue
            children.append({
                "id": item["id"],
                "account": account,
                "industry": f.get("Custom.Industry", ""),
                "tpid": f.get("Custom.TPID", ""),
                "blocker": f.get("Custom.Blockertype", ""),
                "impact": strip_html(f.get("Custom.CustomerImpact", "")),
                "intent": f.get("Custom.TFTOpportunityIntent", ""),
                "stage": f.get("Custom.TFTOpportunityStage", ""),
                "salesPlay": f.get("Custom.SalesPlay", ""),
                "region": f.get("Custom.AreaField", ""),
                "optyName": f.get("Custom.TFTOptyName", ""),
            })

    return children, parent_priority


def search_crm_account(crm_token, customer_name):
    """Search for a CRM account by name. Returns (accountid, name) or None."""
    clean_name = customer_name.strip().replace("'", "''")
    cust_upper = clean_name.upper()

    # Extract significant words (exclude common suffixes)
    stop_words = {"THE", "INC", "LTD", "LLC", "CORP", "CORPORATION", "COMPANY",
                  "GROUP", "TECHNOLOGIES", "TECHNOLOGY", "INTERNATIONAL", "GLOBAL",
                  "LIMITED", "SERVICES", "AND", "OF", "FOR"}
    words = [w for w in clean_name.split() if len(w) > 2 and w.upper() not in stop_words]

    def score_match(acct_name):
        """Score how well a CRM account matches the ADO customer name."""
        au = acct_name.upper()
        # Exact match
        if au == cust_upper:
            return 1000
        # Full name contained in account (or vice versa)
        if cust_upper in au:
            return 500
        if au in cust_upper:
            return 400
        # Word-level scoring: prefer word-boundary matches over substring matches
        score = 0
        for w in words:
            wu = w.upper()
            if wu in au:
                # Check if it appears as a whole word (word boundary)
                import re as _re
                if _re.search(r'\b' + _re.escape(wu) + r'\b', au):
                    score += 20  # whole word match
                elif len(wu) >= 5:
                    score += 5   # substring match only for longer words
                # Short words as substrings are likely false positives — skip
        return score

    def _best_from(results):
        """Return (accountid, name) of best match from results, or None."""
        if not results:
            return None
        scored = [(a, score_match(a["name"])) for a in results]
        scored.sort(key=lambda x: x[1], reverse=True)
        if scored[0][1] >= 20:
            return scored[0][0]["accountid"], scored[0][0]["name"]
        return None

    # Strategy 1: startswith on the first significant word (avoids false positives
    # from short names like "CACI" matching "EDUCACION" via contains())
    first_word = words[0] if words else clean_name.split()[0] if clean_name else ""
    if first_word:
        safe_first = first_word.replace("'", "''")
        data = crm_get(crm_token, "accounts", {
            "$filter": f"startswith(name,'{safe_first}')",
            "$select": "name,accountid",
            "$top": "15",
        })
        if data and data.get("value"):
            hit = _best_from(data["value"])
            if hit:
                return hit

    # Strategy 2: Try full name with contains()
    data = crm_get(crm_token, "accounts", {
        "$filter": f"contains(name,'{clean_name}')",
        "$select": "name,accountid",
        "$top": "10",
    })
    if data and data.get("value"):
        hit = _best_from(data["value"])
        if hit:
            return hit

    # Strategy 3: Try each significant word with contains() (longest first)
    for word in sorted(words, key=len, reverse=True)[:2]:
        if len(word) < 4:
            continue
        safe_word = word.replace("'", "''")
        data = crm_get(crm_token, "accounts", {
            "$filter": f"contains(name,'{safe_word}')",
            "$select": "name,accountid",
            "$top": "20",
        })
        if data and data.get("value"):
            hit = _best_from(data["value"])
            if hit:
                return hit

    return None, None


def find_or_create_customer_product(crm_token, account_id, account_name, tpid=None, dry_run=False):
    """Find or create a CustomerProduct record for Purview for AI + account."""
    # Search for existing
    data = crm_get(crm_token, "ems_customerproducts", {
        "$filter": f"_ems_selectedproduct_value eq {PURVIEW_AI_PRODUCT_ID} and _ems_productcustomer_value eq {account_id}",
        "$select": "ems_customerproductid,ems_name",
        "$top": "1",
    })
    if data and data.get("value"):
        cp = data["value"][0]
        return cp["ems_customerproductid"], cp["ems_name"], False

    # Create new
    cp_name = f"Purview for AI - {account_name}"
    if dry_run:
        return None, cp_name, True

    new_cp = crm_post(crm_token, "ems_customerproducts", {
        "ems_name": cp_name,
        "ems_productcustomer@odata.bind": f"/accounts({account_id})",
        "ems_selectedproduct@odata.bind": f"/products({PURVIEW_AI_PRODUCT_ID})",
    })
    if new_cp:
        return new_cp["ems_customerproductid"], cp_name, True

    # If creation failed (likely TenantID missing), set TenantID from TPID and retry
    if tpid:
        print(f"     🔧 Setting TenantID on account from TPID ({tpid}), retrying...")
        crm_patch(crm_token, f"accounts({account_id})", {"ems_tenantidakacontextid": str(tpid)})
        new_cp = crm_post(crm_token, "ems_customerproducts", {
            "ems_name": cp_name,
            "ems_productcustomer@odata.bind": f"/accounts({account_id})",
            "ems_selectedproduct@odata.bind": f"/products({PURVIEW_AI_PRODUCT_ID})",
        })
        if new_cp:
            return new_cp["ems_customerproductid"], cp_name, True

    return None, cp_name, True


def get_existing_evidence(crm_token, feature_guid):
    """Get existing evidence records for a feature to check duplicates."""
    data = crm_get(crm_token, "ems_featureevidences", {
        "$filter": f"_ems_lookupfeatureevidenceid_value eq {feature_guid}",
        "$select": "ems_evidencename,_ems_featurecustomerid_value,ems_featureevidenceid",
    })
    if data and data.get("value"):
        return data["value"]
    return []


def get_feature_name(crm_token, feature_guid):
    """Get the feature name from CRM."""
    data = crm_get(crm_token, f"ems_productfeatures({feature_guid})", {
        "$select": "ems_featurename",
    })
    if data:
        return data.get("ems_featurename", "Unknown Feature")
    return "Unknown Feature"


def sync_feature(crm_token, ado_token, feature_guid, feature_id, uat_id, dry_run=False):
    """Sync ADO evidence to CRM for a single feature."""
    print(f"\n{'='*70}")
    print(f"📋 Feature: {feature_id} (UAT: {uat_id})")
    print(f"   CRM GUID: {feature_guid}")

    # Get feature name
    feature_name = get_feature_name(crm_token, feature_guid)
    print(f"   Name: {feature_name[:80]}")

    # Fetch ADO children
    print(f"\n  🔍 Fetching ADO evidence for work item {uat_id}...")
    children, ado_priority = fetch_ado_children(ado_token, uat_id)
    print(f"     Found {len(children)} evidence items (ADO priority: P{ado_priority})")

    if not children:
        print("  ℹ️  No evidence items to sync.")
        return {"created": 0, "skipped": 0, "errors": 0}

    # Get existing CRM evidence
    print(f"  🔍 Checking existing CRM evidence...")
    existing = get_existing_evidence(crm_token, feature_guid)
    existing_customer_ids = {e.get("_ems_featurecustomerid_value") for e in existing if e.get("_ems_featurecustomerid_value")}
    existing_names = {e.get("ems_evidencename", "").lower() for e in existing}
    print(f"     Found {len(existing)} existing evidence records")

    # Map CRM priority
    crm_priority = PRIORITY_MAP_ADO_TO_CRM.get(ado_priority, PRIORITY_DEFAULT)

    stats = {"created": 0, "skipped": 0, "errors": 0}

    # ── Pre-resolve & deduplicate customers ──────────────────────────────
    # Multiple ADO entries may have similar but not identical names
    # (e.g. "CACI", "CACI INC", "CACI International") that resolve to
    # different CRM accounts.  We group by a normalised key and keep only
    # one representative per group so that a single CRM account is used.
    import re as _re

    def _normalise_account_key(name):
        """Return a lowercase root key for grouping similar accounts."""
        stop = {"THE", "INC", "LTD", "LLC", "CORP", "CORPORATION", "COMPANY",
                "GROUP", "TECHNOLOGIES", "TECHNOLOGY", "INTERNATIONAL", "GLOBAL",
                "LIMITED", "SERVICES", "AND", "OF", "FOR", "CO"}
        parts = _re.split(r'[\s,.\-/]+', name.strip().upper())
        sig = [p for p in parts if p and p not in stop]
        # Use the first two significant words as the key (or just the first)
        return " ".join(sig[:2]).lower() if sig else name.strip().lower()

    print(f"\n  🔍 Resolving ADO customers to CRM accounts...")
    resolved = []          # (child, account_id, crm_account_name)
    resolve_cache = {}     # ado_customer_name → (account_id, crm_name) | None
    for child in children:
        cname = child["account"]
        if cname not in resolve_cache:
            resolve_cache[cname] = search_crm_account(crm_token, cname)
        aid, aname = resolve_cache[cname]
        if not aid:
            print(f"     ⚠️  Could not find CRM account for '{cname}' — skipping")
            stats["errors"] += 1
            continue
        resolved.append((child, aid, aname))

    # Group by normalised key and pick one account per group
    groups = {}   # norm_key → (account_id, crm_name, [children])
    for child, aid, aname in resolved:
        key = _normalise_account_key(aname)
        if key not in groups:
            groups[key] = (aid, aname, [child])
        else:
            # Keep the existing representative account, just add the child
            groups[key][2].append(child)

    # Log deduplication
    for key, (aid, aname, grp_children) in groups.items():
        if len(grp_children) > 1:
            orig_names = list(dict.fromkeys(c["account"] for c in grp_children))
            print(f"     🔗 Merged {len(grp_children)} entries ({', '.join(orig_names)}) → {aname}")

    for key, (account_id, crm_account_name, grp_children) in groups.items():
        # Use the first child in the group as the representative evidence
        child = grp_children[0]
        customer_name = child["account"]
        print(f"\n  👤 Customer: {crm_account_name}" +
              (f" (from ADO: {customer_name})" if customer_name != crm_account_name else ""))
        print(f"     ✓ Matched CRM account: {crm_account_name} ({account_id})")

        # Check for duplicate against existing CRM evidence
        if account_id in existing_customer_ids:
            print(f"     ⏭️  Already has evidence for this customer — skipping")
            stats["skipped"] += 1
            continue

        # Also check by name pattern
        evidence_name = f"{feature_name} | Purview for AI - {crm_account_name}"
        if evidence_name.lower() in existing_names:
            print(f"     ⏭️  Evidence with matching name exists — skipping")
            stats["skipped"] += 1
            continue

        # Find or create CustomerProduct
        cp_id, cp_name, cp_created = find_or_create_customer_product(
            crm_token, account_id, crm_account_name, tpid=child.get("tpid"), dry_run=dry_run
        )
        if cp_created:
            print(f"     {'🆕 Would create' if dry_run else '🆕 Created'} CustomerProduct: {cp_name}")
        else:
            print(f"     ✓ Found CustomerProduct: {cp_name}")

        # Map blocking field
        blocker = child.get("blocker", "")
        crm_blocking = BLOCKING_ADOPTION if "Adoption" in blocker else BLOCKING_RETENTION

        # Build description from customer scenario (use representative child,
        # but note all merged ADO evidence IDs)
        description_parts = []
        if child.get("impact"):
            description_parts.append(f"Customer Scenario:\n{child['impact']}")
        if child.get("optyName"):
            description_parts.append(f"\nOpportunity: {child['optyName']}")
        if child.get("intent"):
            description_parts.append(f"Intent: {child['intent']}")
        if child.get("stage"):
            description_parts.append(f"Stage: {child['stage']}")
        if child.get("salesPlay"):
            description_parts.append(f"Sales Play: {child['salesPlay']}")
        if child.get("region"):
            description_parts.append(f"Region: {child['region']}")
        all_ev_ids = ", ".join(f"#{c['id']}" for c in grp_children)
        description_parts.append(f"\n[Synced from ADO #{uat_id} / Evidence {all_ev_ids}]")
        description = "\n".join(description_parts)

        if dry_run:
            print(f"     🔵 Would create evidence: {evidence_name[:80]}")
            print(f"        Priority: ADO P{ado_priority} → CRM {crm_priority}")
            print(f"        Blocking: {blocker or 'N/A'} → {'Adoption' if crm_blocking == BLOCKING_ADOPTION else 'Retention'}")
            stats["created"] += 1
            existing_customer_ids.add(account_id)
            continue

        if not cp_id:
            print(f"     ⚠️  No CustomerProduct (account may lack TenantID) — creating evidence without it")

        # Create Feature Evidence record
        evidence_data = {
            "ems_evidencename": evidence_name[:200],
            "ems_evdescirption": description[:10000],
            "ems_requestedby": REQUESTED_BY_CUSTOMER,
            "ems_evblocking": crm_blocking,
            "ems_featurerequestpriority": crm_priority,
            "ems_lookupfeatureevidenceid@odata.bind": f"/ems_productfeatures({feature_guid})",
            "ems_featurecustomerid@odata.bind": f"/accounts({account_id})",
        }
        if cp_id:
            evidence_data["ems_bycustomerproduct@odata.bind"] = f"/ems_customerproducts({cp_id})"

        result = crm_post(crm_token, "ems_featureevidences", evidence_data)
        if result:
            ev_id = result.get("ems_featurereqid", result.get("ems_featureevidenceid", "?"))
            print(f"     ✅ Created evidence: {ev_id}")
            stats["created"] += 1
            # Add to existing set to prevent duplicates within same run
            existing_customer_ids.add(account_id)
        else:
            print(f"     ❌ Failed to create evidence")
            stats["errors"] += 1

    return stats


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Sync ADO evidence to CRM for Purview for AI features")
    parser.add_argument("--feature", help="Sync a specific feature by ID (e.g., MFR-M365-38863)")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without creating records")
    parser.add_argument("--list", action="store_true", help="List all linked features")
    args = parser.parse_args()

    if args.list:
        print("Linked features (CRM → ADO):")
        for fid, uat in FEATURE_UAT_LINKS.items():
            print(f"  {fid:20} → ADO #{uat}")
        return

    print("🔐 Authenticating...")
    crm_token = get_token("https://m365crm.crm.dynamics.com")
    ado_token = get_token("499b84ac-1321-427f-aa17-267ca6975798")
    print("✅ Authenticated to CRM and ADO")

    if args.dry_run:
        print("\n⚠️  DRY RUN MODE — no records will be created\n")

    # Determine which features to sync
    if args.feature:
        if args.feature in FEATURE_UAT_LINKS:
            to_sync = {args.feature: FEATURE_UAT_LINKS[args.feature]}
        else:
            # Dynamically look up the feature from CRM (supports newly created FRs)
            safe_id = args.feature.replace("'", "''")
            data = crm_get(crm_token, "ems_productfeatures",
                           {"$filter": f"ems_featureid eq '{safe_id}'",
                            "$select": "ems_productfeatureid,ems_featureid,ems_onelist"})
            if data and data.get("value"):
                uat_id_str = data["value"][0].get("ems_onelist", "")
                if uat_id_str and uat_id_str.isdigit():
                    to_sync = {args.feature: int(uat_id_str)}
                    print(f"  ℹ️  Dynamically resolved {args.feature} → ADO #{uat_id_str}")
                else:
                    print(f"❌ Feature '{args.feature}' exists but has no linked ADO ID (ems_onelist).")
                    sys.exit(1)
            else:
                print(f"❌ Feature '{args.feature}' not found in CRM or linked features.")
                print("   Use --list to see available features.")
                sys.exit(1)
    else:
        to_sync = FEATURE_UAT_LINKS

    # Resolve CRM GUIDs dynamically
    print(f"\n📊 Resolving {len(to_sync)} feature(s) from CRM...")
    targets = {}
    for fid, uat in to_sync.items():
        guid = resolve_feature_guid(crm_token, fid)
        if guid:
            targets[guid] = {"featureId": fid, "uatId": uat}
        else:
            print(f"  ⚠️  {fid} not found in CRM — skipping")

    if not targets:
        print("❌ No features resolved. Nothing to sync.")
        sys.exit(1)

    print(f"   ✓ Resolved {len(targets)} feature(s)\n")

    total_stats = {"created": 0, "skipped": 0, "errors": 0}
    for guid, info in targets.items():
        stats = sync_feature(crm_token, ado_token, guid, info["featureId"], info["uatId"], args.dry_run)
        for k in total_stats:
            total_stats[k] += stats[k]

    print(f"\n{'='*70}")
    print(f"📊 SUMMARY")
    print(f"   {'Would create' if args.dry_run else 'Created'}: {total_stats['created']} evidence records")
    print(f"   Skipped (duplicates): {total_stats['skipped']}")
    print(f"   Errors: {total_stats['errors']}")
    print(f"{'='*70}")


if __name__ == "__main__":
    main()

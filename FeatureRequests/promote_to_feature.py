#!/usr/bin/env python3
"""Promote a Field Submitted Feature to a CRM Feature Request with evidence.

Usage:
    python promote_to_feature.py --feature-id <m365_quickfeaturecreateid>
    python promote_to_feature.py --feature-id <id> --existing-feature <ems_productfeatureid>
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
import urllib.error
import os

CRM_BASE = "https://m365crm.crm.dynamics.com/api/data/v9.0"
PURVIEW_AI_PRODUCT_ID = "eee1947c-0ea7-ef11-8a69-6045bdee9a10"
REQUESTED_BY_CUSTOMER = 172430000
AZURE_TENANT_ID = os.getenv("AZURE_TENANT_ID", "").strip()

# Priority codes are identical between quick features and feature requests
# Blocking codes: 172430002=Adoption, 500350000=Retention, 172430000=Deployment,
#   172430001=Purchase, 172430003=None

# ── Auth ──────────────────────────────────────────────────────────────────────

def get_token():
    cmd = ["az", "account", "get-access-token", "--resource", "https://m365crm.crm.dynamics.com", "--query", "accessToken", "-o", "tsv"]
    if AZURE_TENANT_ID:
        cmd.extend(["--tenant", AZURE_TENANT_ID])
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"❌ Auth failed: {result.stderr.strip()}")
        sys.exit(1)
    return result.stdout.strip()


# ── API Helpers ───────────────────────────────────────────────────────────────

def crm_get(token, path, params=None, extra_headers=None):
    url = f"{CRM_BASE}/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    hdrs = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "OData-MaxVersion": "4.0",
        "OData-Version": "4.0",
    }
    if extra_headers:
        hdrs.update(extra_headers)
    req = urllib.request.Request(url, headers=hdrs)
    try:
        resp = urllib.request.urlopen(req)
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        print(f"  ⚠️  CRM GET error {e.code}: {body[:300]}")
        return None


def crm_post(token, path, data):
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


# ── Core Logic ────────────────────────────────────────────────────────────────

def fetch_quick_feature(token, feature_id):
    """Fetch the field submitted feature with annotations for customer name."""
    data = crm_get(token, f"m365_quickfeaturecreates({feature_id})", {
        "$select": "m365_name,m365_description,m365_priority,m365_blocking,"
                   "m365_opportunitysize,m365_quickfeaturecreateid,"
                   "_m365_customerlookup_value",
    }, extra_headers={"Prefer": "odata.include-annotations=OData.Community.Display.V1.FormattedValue"})
    if not data:
        return None
    return {
        "id": data.get("m365_quickfeaturecreateid"),
        "name": (data.get("m365_name") or "")[:200],
        "description": (data.get("m365_description") or "")[:10000],
        "priority": data.get("m365_priority"),
        "blocking": data.get("m365_blocking"),
        "opp_size": data.get("m365_opportunitysize"),
        "account_id": data.get("_m365_customerlookup_value"),
        "account_name": data.get(
            "_m365_customerlookup_value@OData.Community.Display.V1.FormattedValue", ""
        ),
    }


def check_duplicate_feature(token, feature_name):
    """Check if a feature request with the same name already exists."""
    safe_name = feature_name.replace("'", "''")
    data = crm_get(token, "ems_productfeatures", {
        "$filter": f"ems_featurename eq '{safe_name}' and "
                   f"_ems_productname_value eq {PURVIEW_AI_PRODUCT_ID}",
        "$select": "ems_productfeatureid,ems_featureid,ems_featurename",
        "$top": "1",
    })
    if data and data.get("value"):
        return data["value"][0]
    return None


def find_or_create_customer_product(token, account_id, account_name):
    """Find or create a CustomerProduct record for Purview for AI + account."""
    data = crm_get(token, "ems_customerproducts", {
        "$filter": f"_ems_selectedproduct_value eq {PURVIEW_AI_PRODUCT_ID} "
                   f"and _ems_productcustomer_value eq {account_id}",
        "$select": "ems_customerproductid,ems_name",
        "$top": "1",
    })
    if data and data.get("value"):
        cp = data["value"][0]
        return cp["ems_customerproductid"]

    cp_name = f"Purview for AI - {account_name}"[:200]
    new_cp = crm_post(token, "ems_customerproducts", {
        "ems_name": cp_name,
        "ems_productcustomer@odata.bind": f"/accounts({account_id})",
        "ems_selectedproduct@odata.bind": f"/products({PURVIEW_AI_PRODUCT_ID})",
    })
    if new_cp:
        return new_cp["ems_customerproductid"]
    return None


def create_feature_request(token, qf):
    """Create a new CRM Feature Request from a quick feature."""
    payload = {
        "ems_featurename": qf["name"],
        "ems_featurdescription": qf["description"],
        "ems_ProductName@odata.bind": f"/products({PURVIEW_AI_PRODUCT_ID})",
        "ems_featurestatus": 172430000,  # Evidence Gathering
    }
    if qf["priority"]:
        payload["ems_priorityrating"] = qf["priority"]

    result = crm_post(token, "ems_productfeatures", payload)
    if result:
        return {
            "guid": result.get("ems_productfeatureid"),
            "feature_id": result.get("ems_featureid", ""),
            "name": result.get("ems_featurename", qf["name"]),
        }
    return None


def create_evidence(token, feature_guid, qf, cp_id):
    """Create an evidence record linking the customer to the feature request."""
    evidence_name = f"Purview for AI - {qf['account_name']}"[:200]
    payload = {
        "ems_evidencename": evidence_name,
        "ems_evdescirption": qf["description"],
        "ems_requestedby": REQUESTED_BY_CUSTOMER,
        "ems_lookupfeatureevidenceid@odata.bind": f"/ems_productfeatures({feature_guid})",
        "ems_featurecustomerid@odata.bind": f"/accounts({qf['account_id']})",
    }
    if qf["priority"]:
        payload["ems_featurerequestpriority"] = qf["priority"]
    if qf["blocking"]:
        payload["ems_evblocking"] = qf["blocking"]
    if cp_id:
        payload["ems_bycustomerproduct@odata.bind"] = f"/ems_customerproducts({cp_id})"

    result = crm_post(token, "ems_featureevidences", payload)
    if result:
        return result.get("ems_featureevidenceid") or result.get("ems_featurereqid")
    return None


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Promote field feature to CRM feature request")
    parser.add_argument("--feature-id", required=True, help="m365_quickfeaturecreateid GUID")
    parser.add_argument("--existing-feature", default=None,
                        help="Add evidence to existing ems_productfeatureid instead of creating new")
    args = parser.parse_args()

    print(f"🚀 Promote to Feature Request")
    print(f"   Field Feature ID: {args.feature_id}")
    if args.existing_feature:
        print(f"   Target: existing feature {args.existing_feature}")
    print()

    token = get_token()

    # Step 1: Fetch the quick feature record
    print("📋 Fetching field submitted feature...")
    qf = fetch_quick_feature(token, args.feature_id)
    if not qf:
        print("❌ Could not fetch field submitted feature — aborting")
        sys.exit(1)

    print(f"  ✓ {qf['name']}")
    print(f"    Customer: {qf['account_name'] or 'None'}")
    print(f"    Priority: {qf['priority']}, Blocking: {qf['blocking']}")

    # Step 2: Validate prerequisites
    if not qf["account_id"]:
        print("❌ No customer account linked to this feature — cannot create evidence")
        sys.exit(1)

    # Step 3: Determine target feature request
    feature_guid = None
    feature_id_str = None

    if args.existing_feature:
        # Add evidence to an existing feature
        feature_guid = args.existing_feature
        fr_data = crm_get(token, f"ems_productfeatures({feature_guid})", {
            "$select": "ems_featurename,ems_featureid"
        })
        if not fr_data:
            print(f"❌ Existing feature {feature_guid} not found — aborting")
            sys.exit(1)
        feature_id_str = fr_data.get("ems_featureid", "")
        print(f"\n📌 Adding evidence to existing: {feature_id_str} — {fr_data.get('ems_featurename', '')}")
    else:
        # Check for duplicates
        dup = check_duplicate_feature(token, qf["name"])
        if dup:
            print(f"\n⚠️  A feature request with this name already exists:")
            print(f"   {dup.get('ems_featureid')} — {dup.get('ems_featurename')}")
            print(f"   Adding evidence to existing feature instead of creating a duplicate.")
            feature_guid = dup["ems_productfeatureid"]
            feature_id_str = dup.get("ems_featureid", "")
        else:
            # Create new feature request
            print("\n🆕 Creating Feature Request...")
            fr = create_feature_request(token, qf)
            if not fr:
                print("❌ Failed to create Feature Request — aborting")
                sys.exit(1)
            feature_guid = fr["guid"]
            feature_id_str = fr["feature_id"]
            print(f"  ✅ Created: {feature_id_str} (GUID: {feature_guid})")

    # Step 4: Find or create CustomerProduct
    print("\n🔗 Resolving CustomerProduct...")
    cp_id = find_or_create_customer_product(token, qf["account_id"], qf["account_name"])
    if cp_id:
        print(f"  ✓ CustomerProduct resolved")
    else:
        print(f"  ⚠️  Could not resolve CustomerProduct — evidence will be created without it")

    # Step 5: Check for existing evidence (dedup)
    existing_ev = crm_get(token, "ems_featureevidences", {
        "$filter": f"_ems_lookupfeatureevidenceid_value eq {feature_guid} "
                   f"and _ems_featurecustomerid_value eq {qf['account_id']}",
        "$select": "ems_featureevidenceid,ems_evidencename",
        "$top": "1",
    })
    if existing_ev and existing_ev.get("value"):
        ev = existing_ev["value"][0]
        print(f"\n⚠️  Evidence already exists for this customer on this feature:")
        print(f"   {ev.get('ems_evidencename', '')}")
        print(f"   Skipping evidence creation to avoid duplicates.")
        print(f"\n✅ Done — feature: {feature_id_str}")
        # Output JSON for the server to parse
        print(f"\n__RESULT__:{json.dumps({'ok': True, 'feature_id': feature_id_str, 'feature_guid': feature_guid, 'evidence_existed': True})}")
        return

    # Step 6: Create evidence
    print("\n📝 Creating evidence record...")
    ev_id = create_evidence(token, feature_guid, qf, cp_id)
    if ev_id:
        print(f"  ✅ Evidence created: {ev_id}")
    else:
        print(f"  ❌ Failed to create evidence record")
        print(f"  ⚠️  Feature Request {feature_id_str} was created but has no evidence.")
        print(f"\n__RESULT__:{json.dumps({'ok': False, 'feature_id': feature_id_str, 'feature_guid': feature_guid, 'error': 'Evidence creation failed'})}")
        sys.exit(1)

    print(f"\n✅ Done — promoted to {feature_id_str}")
    print(f"__RESULT__:{json.dumps({'ok': True, 'feature_id': feature_id_str, 'feature_guid': feature_guid, 'evidence_id': ev_id})}")


if __name__ == "__main__":
    main()

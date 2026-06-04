#!/usr/bin/env python3
"""Create a new CRM Feature Request from an ADO UAT work item.

Creates the FR, links it to the UAT (both directions), and adds all evidence.

Usage:
  python create_fr_from_uat.py --ado-id 725395
"""

import argparse
import json
import os
import re
import subprocess
import sys
import requests

ADO_BASE = "https://dev.azure.com/unifiedactiontracker/Technical%20Feedback/_apis"
ADO_URL_BASE = "https://unifiedactiontracker.visualstudio.com/Technical%20Feedback/_workitems/edit/"
CRM_BASE = "https://m365crm.crm.dynamics.com/api/data/v9.2"
CRM_APP_URL = "https://m365crm.crm.dynamics.com/main.aspx?appid=bf381023-a1eb-42ad-b3f5-4ad6a41655fc&pagetype=entityrecord&etn=ems_productfeature&id="
PURVIEW_AI_PRODUCT_ID = "eee1947c-0ea7-ef11-8a69-6045bdee9a10"
AZURE_TENANT_ID = os.getenv("AZURE_TENANT_ID", "").strip()


def get_token(resource):
    cmd = ["az", "account", "get-access-token", "--resource", resource, "--query", "accessToken", "-o", "tsv"]
    if AZURE_TENANT_ID:
        cmd.extend(["--tenant", AZURE_TENANT_ID])
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"❌ Failed to get token for {resource}: {result.stderr}", file=sys.stderr)
        sys.exit(1)
    return result.stdout.strip()


def strip_html(text):
    """Remove HTML tags from text."""
    return re.sub(r'<[^>]+>', ' ', text or '').strip()


def main():
    parser = argparse.ArgumentParser(description="Create CRM Feature Request from ADO UAT")
    parser.add_argument("--ado-id", required=True, help="ADO work item ID")
    args = parser.parse_args()

    ado_id = args.ado_id

    print(f"🆕 Creating Feature Request from ADO #{ado_id}")

    # Get tokens
    crm_token = get_token("https://m365crm.crm.dynamics.com")
    ado_token = get_token("499b84ac-1321-427f-aa17-267ca6975798")

    # Step 1: Fetch ADO work item details
    print("  📋 Fetching ADO work item...")
    resp = requests.get(
        f"{ADO_BASE}/wit/workitems/{ado_id}?$expand=relations&api-version=7.0",
        headers={"Authorization": f"Bearer {ado_token}"})
    if resp.status_code != 200:
        print(f"  ❌ ADO item not found: {resp.status_code}")
        sys.exit(1)

    item = resp.json()
    fields = item.get("fields", {})
    title = fields.get("System.Title", "")
    description = strip_html(fields.get("System.Description", ""))
    print(f"  ✓ Title: {title[:80]}")

    # Step 2: Check if FR with same title already exists (dedup)
    safe_title = title.replace("'", "''")
    check_resp = requests.get(
        f"{CRM_BASE}/ems_productfeatures?$filter=_ems_productname_value eq {PURVIEW_AI_PRODUCT_ID} and ems_featurename eq '{safe_title}'&$select=ems_featureid,ems_productfeatureid,ems_featurename&$top=1",
        headers={"Authorization": f"Bearer {crm_token}"})
    if check_resp.status_code == 200 and check_resp.json().get("value"):
        existing = check_resp.json()["value"][0]
        print(f"  ⚠️ Feature Request already exists: {existing['ems_featureid']} — {existing['ems_featurename']}")
        print(f"  ❌ Aborting — use the Link button instead to link to existing FR")
        sys.exit(1)

    # Step 3: Create new Feature Request in CRM
    print("  📝 Creating CRM Feature Request...")
    fr_payload = {
        "ems_featurename": title,
        "ems_featurdescription": description[:4000] if description else title,
        "ems_ProductName@odata.bind": f"/products({PURVIEW_AI_PRODUCT_ID})",
        "ems_featurestatus": 172430000,  # Evidence Gathering
        "ems_onelist": str(ado_id),
        "ems_onelisturl": f"{ADO_URL_BASE}{ado_id}",
    }
    resp = requests.post(
        f"{CRM_BASE}/ems_productfeatures",
        headers={
            "Authorization": f"Bearer {crm_token}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        },
        json=fr_payload)
    if resp.status_code not in (200, 201):
        print(f"  ❌ CRM create failed: {resp.status_code} {resp.text[:300]}")
        sys.exit(1)

    fr_data = resp.json()
    fr_guid = fr_data.get("ems_productfeatureid", "")
    fr_id = fr_data.get("ems_featureid", "")
    # Fallback: extract GUID from OData-EntityId header if not in body
    if not fr_guid:
        entity_id = resp.headers.get("OData-EntityId", "")
        if "(" in entity_id and ")" in entity_id:
            fr_guid = entity_id.split("(")[-1].rstrip(")")
    if not fr_guid:
        print("  ❌ Could not determine new FR GUID")
        sys.exit(1)
    print(f"  ✓ Created: {fr_id} (GUID: {fr_guid})")

    # Step 4: Update ADO work item with CRM URL
    crm_url = f"{CRM_APP_URL}{fr_guid}"
    print(f"  📝 Updating ADO #{ado_id}: Custom.EngineeringWorkItemURL")
    resp = requests.patch(
        f"{ADO_BASE}/wit/workitems/{ado_id}?api-version=7.0",
        headers={
            "Authorization": f"Bearer {ado_token}",
            "Content-Type": "application/json-patch+json",
        },
        json=[{
            "op": "add",
            "path": "/fields/Custom.EngineeringWorkItemURL",
            "value": crm_url,
        }])
    if resp.status_code not in (200, 204):
        print(f"  ⚠️ ADO update failed: {resp.status_code} {resp.text[:200]}")
    else:
        print("  ✓ ADO work item updated")

    # Step 5: Sync evidence using the proven sync_ado_to_crm.py logic
    print("  🔍 Syncing evidence from ADO to CRM...")
    sync_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sync_ado_to_crm.py")
    sync_result = subprocess.run(
        [sys.executable, sync_script, "--feature", fr_id],
        capture_output=True, text=True, timeout=120)
    if sync_result.returncode == 0:
        # Extract evidence stats from output
        for line in sync_result.stdout.split('\n'):
            if '✅' in line or 'Created' in line or 'evidence' in line.lower():
                print(f"  {line.strip()}")
    else:
        print(f"  ⚠️ Evidence sync had issues (check output):")
        for line in (sync_result.stdout + sync_result.stderr).split('\n')[-10:]:
            if line.strip():
                print(f"    {line.strip()}")

    print(f"\n✅ Successfully created: {fr_id} from ADO #{ado_id}")
    print(f"   CRM: {crm_url}")
    print(f"   ADO: {ADO_URL_BASE}{ado_id}")


if __name__ == "__main__":
    main()

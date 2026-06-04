#!/usr/bin/env python3
"""Link an ADO UAT work item to a CRM Feature Request (MFR).

Updates:
  - CRM Feature Request: sets ems_onelist (UAT ID) and ems_onelisturl (UAT URL)
  - ADO Work Item: sets Custom.EngineeringWorkItemURL to CRM record URL

Usage:
  python link_uat_to_fr.py --ado-id 686428 --fr-guid <CRM-GUID>
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


def main():
    parser = argparse.ArgumentParser(description="Link ADO UAT to CRM Feature Request")
    parser.add_argument("--ado-id", required=True, help="ADO work item ID")
    parser.add_argument("--fr-guid", required=True, help="CRM Feature Request GUID")
    args = parser.parse_args()

    ado_id = args.ado_id
    fr_guid = args.fr_guid

    print(f"🔗 Linking ADO #{ado_id} ↔ CRM Feature Request {fr_guid}")

    # Get tokens
    crm_token = get_token("https://m365crm.crm.dynamics.com")
    ado_token = get_token("499b84ac-1321-427f-aa17-267ca6975798")

    # Step 1: Verify CRM Feature Request exists and check current state
    print("  📋 Checking CRM Feature Request...")
    resp = requests.get(
        f"{CRM_BASE}/ems_productfeatures({fr_guid})?$select=ems_featureid,ems_featurename,ems_onelist,ems_onelisturl",
        headers={"Authorization": f"Bearer {crm_token}"})
    if resp.status_code != 200:
        print(f"  ❌ Feature Request not found: {resp.status_code} {resp.text[:200]}")
        sys.exit(1)
    fr = resp.json()
    fr_id = fr.get("ems_featureid", "")
    fr_name = fr.get("ems_featurename", "")
    existing_uat = fr.get("ems_onelist")
    print(f"  ✓ Found: {fr_id} — {fr_name}")

    if existing_uat and str(existing_uat) != str(ado_id):
        print(f"  ⚠️  Feature Request already linked to UAT #{existing_uat}")
        print(f"  ❌ Aborting — unlink existing UAT first if you want to re-link")
        sys.exit(1)

    # Step 2: Update CRM Feature Request with UAT info
    uat_url = f"{ADO_URL_BASE}{ado_id}"
    print(f"  📝 Updating CRM: ems_onelist={ado_id}, ems_onelisturl={uat_url}")
    resp = requests.patch(
        f"{CRM_BASE}/ems_productfeatures({fr_guid})",
        headers={
            "Authorization": f"Bearer {crm_token}",
            "Content-Type": "application/json",
        },
        json={
            "ems_onelist": str(ado_id),
            "ems_onelisturl": uat_url,
        })
    if resp.status_code not in (200, 204):
        print(f"  ❌ CRM update failed: {resp.status_code} {resp.text[:200]}")
        sys.exit(1)
    print("  ✓ CRM Feature Request updated")

    # Step 3: Update ADO work item with CRM URL
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
        print(f"  ❌ ADO update failed: {resp.status_code} {resp.text[:200]}")
        sys.exit(1)
    print("  ✓ ADO work item updated")

    # Step 4: Sync evidence using the proven sync_ado_to_crm.py logic
    print("  🔍 Syncing evidence from ADO to CRM...")
    sync_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sync_ado_to_crm.py")
    sync_result = subprocess.run(
        [sys.executable, sync_script, "--feature", fr_id],
        capture_output=True, text=True, timeout=120)
    if sync_result.returncode == 0:
        for line in sync_result.stdout.split('\n'):
            if '✅' in line or 'Created' in line or 'evidence' in line.lower():
                print(f"  {line.strip()}")
    else:
        print(f"  ⚠️ Evidence sync had issues:")
        for line in (sync_result.stdout + sync_result.stderr).split('\n')[-10:]:
            if line.strip():
                print(f"    {line.strip()}")

    print(f"\n✅ Successfully linked: ADO #{ado_id} ↔ {fr_id} ({fr_name})")
    print(f"   CRM: {crm_url}")
    print(f"   ADO: {uat_url}")


if __name__ == "__main__":
    main()

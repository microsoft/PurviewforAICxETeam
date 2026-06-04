#!/usr/bin/env python3
"""Link an Engineering ADO work item to a CRM Feature Request (MFR).

Updates CRM Feature Request:
  - ems_onelist: Engineering ADO work item ID
  - ems_onelisturl: Engineering ADO work item URL

Usage:
  python link_eng_to_fr.py --ado-id 6051888 --fr-guid <CRM-GUID>
"""

import argparse
import json
import subprocess
import sys
import urllib.request
import urllib.error

ADO_ENG_URL_BASE = "https://o365exchange.visualstudio.com/IP%20Engineering/_workitems/edit/"
CRM_BASE = "https://m365crm.crm.dynamics.com/api/data/v9.2"
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
    parser = argparse.ArgumentParser(description="Link Engineering ADO to CRM Feature Request")
    parser.add_argument("--ado-id", required=True, help="Engineering ADO work item ID")
    parser.add_argument("--fr-guid", required=True, help="CRM Feature Request GUID")
    args = parser.parse_args()

    ado_id = args.ado_id
    fr_guid = args.fr_guid

    print(f"🔗 Linking Engineering ADO #{ado_id} → CRM Feature Request {fr_guid}")

    crm_token = get_token("https://m365crm.crm.dynamics.com")

    # Step 1: Verify CRM Feature Request exists
    print("  📋 Checking CRM Feature Request...")
    url = f"{CRM_BASE}/ems_productfeatures({fr_guid})?$select=ems_featureid,ems_featurename,ems_onelist,ems_onelisturl"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {crm_token}",
        "Accept": "application/json",
    })
    try:
        resp = urllib.request.urlopen(req)
        fr = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"  ❌ Feature Request not found: {e.code} {e.read().decode()[:200]}")
        sys.exit(1)

    fr_id = fr.get("ems_featureid", "")
    fr_name = fr.get("ems_featurename", "")
    existing_ado = fr.get("ems_onelist")
    print(f"  ✓ Found: {fr_id} — {fr_name}")

    if existing_ado and str(existing_ado) != str(ado_id):
        print(f"  ⚠️  Feature Request already linked to ADO #{existing_ado}")
        print(f"  Overwriting with Engineering ADO #{ado_id}...")

    # Step 2: Update CRM Feature Request with Engineering ADO info
    ado_url = f"{ADO_ENG_URL_BASE}{ado_id}"
    print(f"  📝 Updating CRM: ems_tfsid={ado_id}, ems_tfsidurl={ado_url}")

    patch_data = json.dumps({
        "ems_tfsid": str(ado_id),
        "ems_tfsidurl": ado_url,
    }).encode()

    patch_req = urllib.request.Request(
        f"{CRM_BASE}/ems_productfeatures({fr_guid})",
        data=patch_data,
        method="PATCH",
        headers={
            "Authorization": f"Bearer {crm_token}",
            "Content-Type": "application/json",
            "OData-MaxVersion": "4.0",
            "OData-Version": "4.0",
        })
    try:
        urllib.request.urlopen(patch_req)
    except urllib.error.HTTPError as e:
        print(f"  ❌ CRM update failed: {e.code} {e.read().decode()[:200]}")
        sys.exit(1)

    print("  ✓ CRM Feature Request updated")
    print(f"\n✅ Linked: Engineering ADO #{ado_id} → {fr_id} ({fr_name})")
    print(f"   ADO URL: {ado_url}")


if __name__ == "__main__":
    main()

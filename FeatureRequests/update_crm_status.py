#!/usr/bin/env python3
"""Update CRM Feature Status (ems_featurestatus) for a given Feature Request GUID."""
import argparse
import json
import subprocess
import sys
import os

CRM_BASE = "https://m365crm.crm.dynamics.com/api/data/v9.2"
AZURE_TENANT_ID = os.getenv("AZURE_TENANT_ID", "").strip()

def get_crm_token():
    cmd = ["az", "account", "get-access-token", "--resource", "https://m365crm.crm.dynamics.com/", "--query", "accessToken", "-o", "tsv"]
    if AZURE_TENANT_ID:
        cmd.extend(["--tenant", AZURE_TENANT_ID])
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Failed to get CRM token: {result.stderr}", file=sys.stderr)
        sys.exit(1)
    return result.stdout.strip()

def update_feature_status(fr_guid, status_code):
    token = get_crm_token()
    import urllib.request
    url = f"{CRM_BASE}/ems_productfeatures({fr_guid})"
    data = json.dumps({"ems_featurestatus": int(status_code)}).encode()
    req = urllib.request.Request(url, data=data, method="PATCH", headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "If-Match": "*",
        "OData-MaxVersion": "4.0",
        "OData-Version": "4.0",
    })
    try:
        with urllib.request.urlopen(req) as resp:
            print(f"OK: Updated {fr_guid} to status {status_code} (HTTP {resp.status})")
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--fr-guid", required=True, help="CRM Feature Request GUID")
    parser.add_argument("--status", required=True, type=int, help="ems_featurestatus numeric code")
    args = parser.parse_args()
    update_feature_status(args.fr_guid, args.status)

#!/usr/bin/env python3
"""Update ADO work item State and/or SubState (TFTSubState) on dev.azure.com/unifiedactiontracker.
Optionally update the linked CRM Feature Request status based on the status lookup table."""

import json
import subprocess
import sys
import urllib.request
import urllib.error
import urllib.parse
import os
from datetime import datetime

# CRM Feature Status optionset mapping (label → numeric value)
CRM_STATUS_MAP = {
    "In Engineering Backlog": 500350002,
    "Assigned to Release/Sprint": 172430005,
    "Considered": 500350009,
    "Evidence Gathering": 172430000,
    "Generally Available": 172430008,
    "Needs More Information": 500350011,
    "Private Preview": 172430006,
    "Public Preview": 172430007,
    "Rejected": 172430004,
    "Rejected - Workaround Available": 172430010,
    "Tracking Demand": 500350010,
}

# ADO State+Substate → CRM Feature Status mapping (from status_lookup_table.csv)
STATE_TO_CRM_STATUS = {
    ("Not Committed", ""): "In Engineering Backlog",
    ("Engineering Committed", ""): "Assigned to Release/Sprint",
    ("PG Investigating", ""): "Considered",
    ("Not Considered", "Needs Increased Customers"): "Evidence Gathering",
    ("GA", ""): "Generally Available",
    ("In Review", ""): "Needs More Information",
    ("Private Preview", ""): "Private Preview",
    ("Public Preview/GTM", ""): "Public Preview",
    ("Not Considered", "Does Not Fit Product Direction"): "Rejected",
    ("Not Considered", "Return on Investment"): "Rejected - Workaround Available",
}

# List-based lookup for get_crm_status()
STATE_CRM_LOOKUP = [
    ("Not Committed", "", "In Engineering Backlog"),
    ("Engineering Committed", "", "Assigned to Release/Sprint"),
    ("PG Investigating", "", "Considered"),
    ("Not Considered", "Needs Increased Customers", "Evidence Gathering"),
    ("GA", "", "Generally Available"),
    ("In Review", "", "Needs More Information"),
    ("Private Preview", "", "Private Preview"),
    ("Public Preview/GTM", "", "Public Preview"),
    ("Not Considered", "Does Not Fit Product Direction", "Rejected"),
    ("Not Considered", "Return on Investment", "Rejected - Workaround Available"),
]

AZURE_TENANT_ID = os.getenv("AZURE_TENANT_ID", "").strip()


def _current_planned_semester():
    """Return the current ADO Planned Semester label based on today's date."""
    now = datetime.now()
    if 4 <= now.month <= 9:
        return f"[Apr{now.year}-Sep{now.year}]"
    start_year = now.year if now.month >= 10 else now.year - 1
    end_year = start_year + 1
    return f"[Oct{start_year}–Mar{end_year}]"


def get_crm_status(state, substate):
    """Look up CRM Feature Status from ADO state+substate."""
    substate = substate or ""
    for s, ss, crm in STATE_CRM_LOOKUP:
        if s == state and ss == substate:
            return crm
    # If substate doesn't match, try matching state only (first match)
    for s, ss, crm in STATE_CRM_LOOKUP:
        if s == state and ss == "":
            return crm
    return None


def get_token(resource):
    cmd = ["az", "account", "get-access-token", "--resource", resource, "--query", "accessToken", "-o", "tsv"]
    if AZURE_TENANT_ID:
        cmd.extend(["--tenant", AZURE_TENANT_ID])
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"❌ Auth failed for {resource}: {result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)
    return result.stdout.strip()


def get_work_item(token, work_item_id):
    """Fetch the current work item fields needed for safe transitions."""
    url = f"https://dev.azure.com/unifiedactiontracker/Technical%20Feedback/_apis/wit/workitems/{work_item_id}?fields=System.State,Custom.TFTSubState,Custom.PlannedSemester&api-version=7.0"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def update_work_item(token, work_item_id, state=None, substate=None):
    """PATCH the work item to update State and/or TFTSubState."""
    url = f"https://dev.azure.com/unifiedactiontracker/Technical%20Feedback/_apis/wit/workitems/{work_item_id}?api-version=7.0"

    operations = []
    current = get_work_item(token, work_item_id).get("fields", {})
    current_planned_semester = (current.get("Custom.PlannedSemester") or "").strip()

    if state:
        operations.append({
            "op": "replace",
            "path": "/fields/System.State",
            "value": state
        })
        if state == "Engineering Committed" and not current_planned_semester:
            operations.append({
                "op": "add",
                "path": "/fields/Custom.PlannedSemester",
                "value": _current_planned_semester()
            })
    if substate:
        operations.append({
            "op": "replace",
            "path": "/fields/Custom.TFTSubState",
            "value": substate
        })

    if not operations:
        print("Nothing to update")
        return False

    data = json.dumps(operations).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json-patch+json",
        },
        method="PATCH"
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
            new_state = result.get("fields", {}).get("System.State", "")
            new_substate = result.get("fields", {}).get("Custom.TFTSubState", "")
            print(f"✅ Updated work item {work_item_id}: State={new_state}, SubState={new_substate}")
            return True
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:500]
        print(f"❌ Failed to update {work_item_id}: HTTP {e.code}\n{body}", file=sys.stderr)
        return False


def update_crm_feature_status(crm_token, fr_guid, crm_status_label):
    """Update the ems_featurestatus field on a CRM Feature Request."""
    status_value = CRM_STATUS_MAP.get(crm_status_label)
    if status_value is None:
        print(f"⚠️ Unknown CRM status: {crm_status_label}")
        return False

    url = f"https://m365crm.crm.dynamics.com/api/data/v9.2/ems_productfeatures({fr_guid})"
    payload = json.dumps({"ems_featurestatus": status_value}).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bearer {crm_token}",
            "Content-Type": "application/json",
            "If-Match": "*",
        },
        method="PATCH"
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            pass  # 204 No Content = success
        print(f"✅ Updated CRM Feature Request {fr_guid}: Status → {crm_status_label} ({status_value})")
        return True
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:500]
        print(f"❌ Failed to update CRM {fr_guid}: HTTP {e.code}\n{body}", file=sys.stderr)
        return False


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Update ADO work item state")
    parser.add_argument("--id", required=True, type=int, help="Work item ID")
    parser.add_argument("--state", help="New state value")
    parser.add_argument("--substate", help="New substate value")
    parser.add_argument("--fr-guid", help="Linked CRM Feature Request GUID (to sync status)")
    args = parser.parse_args()

    if not args.state and not args.substate:
        print("❌ Must provide --state and/or --substate")
        sys.exit(1)

    ado_token = get_token("499b84ac-1321-427f-aa17-267ca6975798")
    success = update_work_item(ado_token, args.id, state=args.state, substate=args.substate)

    # If there's a linked CRM FR, also update its status
    if success and args.fr_guid:
        state = args.state or ""
        substate = args.substate or ""
        crm_status = get_crm_status(state, substate)
        if crm_status:
            crm_token = get_token("https://m365crm.crm.dynamics.com")
            update_crm_feature_status(crm_token, args.fr_guid, crm_status)
        else:
            print(f"⚠️ No CRM status mapping for State={state}, SubState={substate}")

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Refresh Purview for AI dashboard pages with latest data from CRM and ADO.

Usage:
    python refresh_dashboard.py                   # Refresh all pages
    python refresh_dashboard.py --page features   # Field Submitted Features only
    python refresh_dashboard.py --page requests   # Feature Requests only
    python refresh_dashboard.py --page feedback   # Technical Feedback only
"""

import json
import subprocess
import sys
import re
import urllib.request
import urllib.parse
import urllib.error
import argparse
import os
import hashlib
import openpyxl
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCORE_CACHE_FILE = os.path.join(SCRIPT_DIR, ".llm_score_cache.json")
ADMIN_CONFIG_FILE = os.path.join(SCRIPT_DIR, "purview-admin-config.json")
CRM_BASE = "https://m365crm.crm.dynamics.com/api/data/v9.2"
ADO_BASE = "https://dev.azure.com/unifiedactiontracker/Technical%20Feedback/_apis"
AZURE_TENANT_ID = os.getenv("AZURE_TENANT_ID", "").strip()


def _format_generated_time(dt):
    """Cross-platform generated timestamp (Windows-safe day format)."""
    return f"{dt.day} {dt.strftime('%b %Y at %H:%M')}"

# Load product ID from admin config, fallback to default
def _load_product_id():
    try:
        with open(ADMIN_CONFIG_FILE) as f:
            cfg = json.load(f)
        return cfg.get("productId", "eee1947c-0ea7-ef11-8a69-6045bdee9a10")
    except Exception:
        return "eee1947c-0ea7-ef11-8a69-6045bdee9a10"

def _load_area_path():
    try:
        with open(ADMIN_CONFIG_FILE) as f:
            cfg = json.load(f)
        return cfg.get("areaPath", "IP Engineering\\Purview for AI\\1P Copilots\\M365 Copilot")
    except Exception:
        return "IP Engineering\\Purview for AI\\1P Copilots\\M365 Copilot"

def _load_uat_service_names():
    try:
        with open(ADMIN_CONFIG_FILE) as f:
            cfg = json.load(f)
        names = cfg.get("uatServiceNames")
        if isinstance(names, list):
            cleaned = [str(n).strip() for n in names if str(n).strip()]
            if cleaned:
                return cleaned
        single = str(cfg.get("uatServiceName", "")).strip()
        if single:
            return [single]
        return ["Purview for AI"]
    except Exception:
        return ["Purview for AI"]

def _load_product_name():
    try:
        with open(ADMIN_CONFIG_FILE) as f:
            cfg = json.load(f)
        return cfg.get("productName", "Purview for AI")
    except Exception:
        return "Purview for AI"

PURVIEW_AI_PRODUCT_ID = _load_product_id()
PURVIEW_AI_PRODUCT_NAME = _load_product_name()
ENG_ADO_AREA_PATH = _load_area_path()
UAT_SERVICE_NAMES = _load_uat_service_names()

# Purview for AI keyword patterns for ADO filtering
ADO_KEYWORDS = [
    r'purview for ai', r'dspm for ai', r'copilot interaction', r'copilot prompt',
    r'copilot response', r'dlp for copilot', r'dlp.*copilot', r'copilot.*dlp',
    r'ai hub', r'purview ai', r'ai agent', r'ai governance', r'ai activity',
    r'sensitivity.*copilot', r'foundry.*purview', r'fabric.*purview',
    r'oversharing.*copilot', r'p4ai', r'purview for m365', r'purview for a365',
    r'data risk assessment', r'copilot.*sensitivity', r'copilot.*label',
    r'copilot.*sit\b', r'copilot.*block', r'block.*copilot', r'gcch.*dlp',
    r'gcch.*copilot', r'network dlp', r'inline.*dlp', r'edge.*dlp',
    r'browser.*dlp', r'ocr.*dlp', r'managed app.*dlp', r'copilot.*grounding',
    r'web grounding', r'mfr-m365', r'purview.*copilot', r'copilot.*purview',
    r'dspm.*copilot',
]

# ADO work items to manually include (not in saved query but relevant)
ADO_MANUAL_INCLUDE = [664383]

PROGRESS_FILE = os.path.join(SCRIPT_DIR, ".refresh_progress.json")

def _report_progress(step, pct):
    """Write progress to file for the server to read."""
    try:
        with open(PROGRESS_FILE, 'w') as f:
            json.dump({"step": step, "pct": pct}, f)
    except Exception:
        pass


# ── Score Cache ────────────────────────────────────────────────────────────────

def _load_score_cache():
    """Load cached LLM scores from disk."""
    try:
        with open(SCORE_CACHE_FILE, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def _save_score_cache(cache):
    """Save LLM scores cache to disk."""
    with open(SCORE_CACHE_FILE, 'w') as f:
        json.dump(cache, f)

def _cache_key(source_text, candidates_text):
    """Create a hash key from source + candidates text."""
    combined = f"{source_text[:600]}|||{candidates_text[:3000]}"
    return hashlib.md5(combined.encode()).hexdigest()


# ── Auth ───────────────────────────────────────────────────────────────────────

def get_token(resource):
    cmd = [
        "az", "account", "get-access-token",
        "--resource", resource,
        "--query", "accessToken", "-o", "tsv"
    ]
    if AZURE_TENANT_ID:
        cmd.extend(["--tenant", AZURE_TENANT_ID])
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  ❌ Auth failed for {resource}: {result.stderr.strip()}")
        sys.exit(1)
    return result.stdout.strip()


# ── API Helpers ────────────────────────────────────────────────────────────────

def api_get(token, url, params=None, extra_headers=None):
    if params:
        # Build query string keeping $ intact (OData requires literal $)
        parts = []
        for k, v in params.items():
            parts.append(f"{k}={urllib.parse.quote(str(v), safe=',()_-.')}")
        url += "?" + "&".join(parts)
    hdrs = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "OData-MaxVersion": "4.0",
        "OData-Version": "4.0",
    }
    if extra_headers:
        hdrs.update(extra_headers)
    import time as _t
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            resp = urllib.request.urlopen(req)
            return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 503) and attempt < 3:
                wait = int(e.headers.get("Retry-After", 2 ** (attempt + 1)))
                print(f"    ⚠️ HTTP {e.code}, retrying in {wait}s...")
                _t.sleep(wait)
                continue
            raise


def api_post(token, url, data):
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
        if e.code == 404:
            print(f"    ❌ HTTP 404 Not Found: {url}")
            print(f"    Data: {data}")
        raise


def crm_get(token, path, params=None, extra_headers=None):
    return api_get(token, f"{CRM_BASE}/{path}", params, extra_headers)


def crm_fetch_xml(token, entity_set, xml):
    url = f"{CRM_BASE}/{entity_set}?fetchXml={urllib.parse.quote(xml)}"
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
        if e.code == 404:
            print(f"    ❌ HTTP 404 Not Found: {url}")
        raise


def ado_get(token, path):
    return api_get(token, f"{ADO_BASE}/{path}")


def ado_post(token, path, data):
    return api_post(token, f"{ADO_BASE}/{path}", data)


def strip_html(text):
    return re.sub(r'<[^>]+>', '', text).strip() if text else ""


def replace_data_block(html, var_name, new_data_json):
    """Replace a JavaScript variable assignment in HTML with new data."""
    # Handle both array and object data
    opener = '[' if new_data_json.startswith('[') else '{'
    closer = '];' if opener == '[' else '};'
    pattern_start = f'const {var_name} = {opener}'

    start = html.index(pattern_start)
    end = html.index(closer, start) + len(closer)
    return html[:start] + f'const {var_name} = {new_data_json};' + html[end:]


# ── Page 1: Field Submitted Features ───────────────────────────────────────────

def refresh_features(crm_token):
    filepath = os.path.join(SCRIPT_DIR, "purview-ai-features.html")
    if not os.path.exists(filepath):
        print("  ⚠️  purview-ai-features.html not found — skipping")
        return

    print("\n📋 Refreshing Field Submitted Features...")

    # Fetch Quick Feature Creates for Purview for AI
    fields = "m365_name,m365_description,m365_priority,m365_blocking,m365_opportunitysize,m365_s500,createdon,m365_quickfeaturecreateid,m365_proposed_solution,m365_whatstheroleofyourcustomer,m365_whichorganizationrepresentthecustomer,_m365_createdbyportaluser_value,_m365_customerlookup_value"
    annotations_header = {"Prefer": "odata.include-annotations=OData.Community.Display.V1.FormattedValue"}
    data = crm_get(crm_token, "m365_quickfeaturecreates", {
        "$filter": f"_m365_product_value eq {PURVIEW_AI_PRODUCT_ID}",
        "$select": fields,
        "$orderby": "createdon desc",
    }, extra_headers=annotations_header)
    records = data.get("value", [])

    # Collect unique portal user (contact) IDs to batch-resolve emails
    contact_ids = set()
    for r in records:
        cid = r.get("_m365_createdbyportaluser_value")
        if cid:
            contact_ids.add(cid)

    # Fetch contact emails in batches
    contact_map = {}  # contact_id -> {name, email}
    for cid in contact_ids:
        try:
            cdata = crm_get(crm_token, f"contacts({cid})", {"$select": "fullname,emailaddress1"})
            contact_map[cid] = {
                "name": cdata.get("fullname", ""),
                "email": cdata.get("emailaddress1", ""),
            }
        except Exception:
            pass

    # Strip OData annotations but preserve requester info
    clean = []
    for r in records:
        portal_user_id = r.get("_m365_createdbyportaluser_value")
        portal_user_name = r.get("_m365_createdbyportaluser_value@OData.Community.Display.V1.FormattedValue", "")
        contact = contact_map.get(portal_user_id, {})
        row = {k: v for k, v in r.items() if not k.startswith("@") and not k.startswith("_")}
        row["requester_name"] = contact.get("name") or portal_user_name or ""
        row["requester_email"] = contact.get("email", "")
        row["customer_name"] = r.get("_m365_customerlookup_value@OData.Community.Display.V1.FormattedValue", "") or ""
        clean.append(row)
    print(f"  ✓ Fetched {len(clean)} quick feature creates")

    # Fetch CRM Feature Requests for cross-referencing
    print("  🔍 Cross-referencing with CRM Feature Requests...")
    req_data = crm_get(crm_token, "ems_productfeatures", {
        "$filter": f"_ems_productname_value eq {PURVIEW_AI_PRODUCT_ID}",
        "$select": "ems_featureid,ems_featurename,ems_featurdescription,ems_productfeatureid",
        "$top": "500",
    })
    crm_features = req_data.get("value", [])

    crm_matches = {}
    # Build exact-name lookup for linked feature requests
    linked_features = {}  # qf_id -> {featureId, guid}
    crm_name_map = {}
    for cf in crm_features:
        fname = (cf.get("ems_featurename") or "").strip()
        if fname:
            crm_name_map[fname] = cf

    for qf in clean:
        qf_name = (qf.get("m365_name") or "").strip()
        exact = crm_name_map.get(qf_name)
        if exact:
            fid = exact.get("ems_featureid", "")
            if fid.startswith("MFR-M365-"):
                linked_features[qf["m365_quickfeaturecreateid"]] = {
                    "featureId": fid,
                    "guid": exact.get("ems_productfeatureid", ""),
                }

    # LLM-based similarity scoring
    gh_token = None
    try:
        gh_result = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
        if gh_result.returncode == 0:
            gh_token = gh_result.stdout.strip()
    except Exception:
        pass
    if not gh_token:
        print("    ⚠️ No GitHub token available, falling back to keyword matching")

    _score_cache = _load_score_cache()
    _cache_hits = 0

    # Stopwords and tokenizer for pre-filtering
    stopwords = {'the','a','an','is','are','was','were','be','been','being','have','has','had',
                 'do','does','did','will','would','could','should','may','might','shall','can',
                 'to','of','in','for','on','with','at','by','from','as','into','through','during',
                 'before','after','above','below','between','out','off','over','under','again',
                 'further','then','once','and','but','or','nor','not','so','yet','both','either',
                 'neither','each','every','all','any','few','more','most','other','some','such',
                 'no','only','own','same','than','too','very','just','because','if','when','while',
                 'that','this','these','those','it','its','they','them','their','we','our','you',
                 'your','he','she','his','her','which','what','who','whom','how','where','why',
                 'also','about','up','there','here','new','need','able','like','want','use','using',
                 'used','customer','customers','ability','feature','request','support','currently',
                 'data','policy','policies','content','information','microsoft','purview','m365'}

    def tokenize(text):
        return set(w for w in re.findall(r'[a-z][a-z0-9]+', text.lower()) if len(w) > 2 and w not in stopwords)

    # Pre-compute CRM feature tokens
    crm_prepared = []
    for cf in crm_features:
        fid = cf.get("ems_featureid", "")
        if not fid.startswith("MFR-M365-"):
            continue
        title = cf.get("ems_featurename", "") or ""
        desc = strip_html(cf.get("ems_featurdescription", "") or "")
        tokens = tokenize(title) | tokenize(desc)
        crm_prepared.append({"id": fid, "guid": cf.get("ems_productfeatureid", ""), "title": title, "desc": desc, "tokens": tokens})

    def llm_score_crm_batch(qf_title, qf_desc, candidates):
        """Use LLM to score semantic similarity of CRM candidates against a field feature."""
        nonlocal _cache_hits
        if not gh_token or not candidates:
            return {}
        cand_lines = []
        for i, c in enumerate(candidates):
            cand_lines.append(f'{i+1}. [{c["id"]}] Title: {c["title"]}\n   Desc: {c["desc"][:300]}')
        candidates_text = "\n".join(cand_lines)
        source_text = f"{qf_title}\n{qf_desc[:500]}"

        ckey = _cache_key(source_text, candidates_text)
        if ckey in _score_cache:
            _cache_hits += 1
            return _score_cache[ckey]

        prompt = f"""Score the semantic similarity between this Field Submitted Feature and each CRM Feature Request candidate.
Consider whether they discuss the SAME topic, capability, or customer need — even if worded differently.

Field Submitted Feature:
Title: {qf_title}
Description: {qf_desc[:500]}

Candidates:
{candidates_text}

For each candidate, respond with ONLY a JSON array of objects with "index" (1-based) and "score" (0-100 integer).
Score meaning: 0=unrelated, 20=vaguely related topic, 40=related area, 60=similar need, 80=very similar, 95+=nearly identical request.
Respond with ONLY the JSON array, no other text."""

        import time as _time
        for attempt in range(4):
            try:
                req_data_llm = json.dumps({"model": "gpt-4o-mini", "messages": [{"role": "user", "content": prompt}], "max_tokens": 500, "temperature": 0.1}).encode()
                req = urllib.request.Request(
                    "https://models.inference.ai.azure.com/chat/completions",
                    data=req_data_llm,
                    headers={"Authorization": f"Bearer {gh_token}", "Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=30) as resp:
                    llm_content = json.loads(resp.read())["choices"][0]["message"]["content"].strip()
                    llm_content = llm_content.replace("```json", "").replace("```", "").strip()
                    scores = json.loads(llm_content)
                    result = {s["index"]: min(100, max(0, int(s["score"]))) for s in scores if "index" in s and "score" in s}
                    _score_cache[ckey] = result
                    _time.sleep(1)
                    return result
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < 3:
                    wait = int(e.headers.get("Retry-After", 2 ** (attempt + 1)))
                    _time.sleep(wait)
                    continue
                print(f"    ⚠️ LLM scoring error: {str(e)[:80]}")
            except Exception as e:
                print(f"    ⚠️ LLM scoring error: {str(e)[:80]}")
                break
        return {}

    scored_count = 0
    total_to_score = len(clean)
    print(f"  🔍 Building Similar CRM Feature Requests matching...")
    print(f"    Found {len(crm_prepared)} CRM Feature Requests for comparison")

    for qf in clean:
        qf_id = qf["m365_quickfeaturecreateid"]
        qf_name = (qf.get("m365_name") or "").strip()
        qf_desc = (qf.get("m365_description") or "").strip()
        qf_tokens = tokenize(qf_name) | tokenize(qf_desc)

        if len(qf_tokens) < 2:
            continue

        # Keyword pre-filter: top 15 CRM candidates by token overlap
        pre_scores = []
        for crm in crm_prepared:
            shared = len(qf_tokens & crm["tokens"])
            if shared >= 2:
                union = len(qf_tokens | crm["tokens"])
                pre_scores.append((shared / union if union else 0, crm))
        pre_scores.sort(key=lambda x: x[0], reverse=True)
        candidates = [ps[1] for ps in pre_scores[:15]]

        if not candidates:
            continue

        # LLM semantic scoring
        if gh_token:
            llm_scores = llm_score_crm_batch(qf_name, qf_desc, candidates)
            scored_count += 1
            if scored_count % 10 == 0:
                print(f"    ... scored {scored_count}/{total_to_score} ({_cache_hits} from cache)")
                pct = 5 + int(10 * scored_count / max(total_to_score, 1))
                _report_progress(f"AI scoring Field Submitted Features... ({scored_count}/{total_to_score})", pct)
        else:
            llm_scores = {}

        matches = []
        for i, c in enumerate(candidates):
            score = llm_scores.get(i + 1)
            if score is None:
                # Fallback to keyword overlap percentage
                shared = len(qf_tokens & c["tokens"])
                union = len(qf_tokens | c["tokens"])
                score = min(100, round((shared / union if union else 0) * 100))
            if score >= 20:
                matches.append({"featureId": c["id"], "guid": c["guid"], "score": score})

        matches.sort(key=lambda m: m["score"], reverse=True)
        if matches:
            crm_matches[qf_id] = matches[:5]

    # Save cache
    if gh_token:
        _save_score_cache(_score_cache)
        print(f"    ({scored_count} LLM-scored, {_cache_hits} cached)")

    print(f"  ✓ {len(crm_matches)} field features with similar CRM Feature Requests")
    print(f"  ✓ {len(linked_features)} field features with linked Feature Requests")

    # Update HTML
    html = open(filepath).read()
    new_json = json.dumps(clean, ensure_ascii=True)
    html = replace_data_block(html, 'rawData', new_json)
    html = replace_data_block(html, 'CRM_MATCHES', json.dumps(crm_matches, ensure_ascii=True))
    html = replace_data_block(html, 'LINKED_FEATURES', json.dumps(linked_features, ensure_ascii=True))

    # Update generated date and product name in header
    today = _format_generated_time(datetime.now())
    html = re.sub(r'Generated \d+ \w+ \d{4}(?: at \d{2}:\d{2})?', f'Generated {today}', html)
    html = re.sub(r'Product: [^&]+&bull;', f'Product: {PURVIEW_AI_PRODUCT_NAME} &bull;', html)

    open(filepath, 'w').write(html)
    print(f"  ✅ Updated purview-ai-features.html ({len(clean)} records)")


# ── Page 2: Feature Requests ──────────────────────────────────────────────────

def refresh_requests(crm_token, ado_token, skip_llm=False):
    filepath = os.path.join(SCRIPT_DIR, "purview-ai-feature-requests.html")
    if not os.path.exists(filepath):
        print("  ⚠️  purview-ai-feature-requests.html not found — skipping")
        return

    print("\n📋 Refreshing Feature Requests...")

    # Step 1: Fetch all product features for Purview for AI
    all_features = []
    url_params = {
        "$filter": f"_ems_productname_value eq {PURVIEW_AI_PRODUCT_ID}",
        "$select": "ems_featureid,ems_featurename,ems_featurdescription,ems_featurestatus,ems_priorityrating,createdon,modifiedon,ems_totalnumberofcustomers,_ems_featurecategory_value,ems_onelist,ems_onelisturl,ems_tfsid,ems_tfsidurl,ems_productfeatureid",
        "$orderby": "createdon desc",
        "$top": "500",
    }
    data = crm_get(crm_token, "ems_productfeatures", url_params)
    raw_features = data.get("value", [])
    print(f"  ✓ Fetched {len(raw_features)} features from CRM")

    # Step 2: Get CRM customer evidence via FetchXML
    print("  🔍 Fetching CRM customer evidence...")
    fetchxml = f'''<fetch>
      <entity name="ems_featureevidence">
        <attribute name="ems_evidencename"/>
        <attribute name="ems_featureevidenceid"/>
        <link-entity name="ems_productfeature" from="ems_productfeatureid" to="ems_lookupfeatureevidenceid" alias="feat">
          <attribute name="ems_productfeatureid"/>
          <filter><condition attribute="ems_productname" operator="eq" value="{PURVIEW_AI_PRODUCT_ID}"/></filter>
        </link-entity>
      </entity>
    </fetch>'''
    ev_data = crm_fetch_xml(crm_token, "ems_featureevidences", fetchxml)
    evidence_records = ev_data.get("value", [])

    # Build customer map: feature GUID → [customer names]
    crm_customer_map = {}
    for ev in evidence_records:
        feat_id = ev.get("feat.ems_productfeatureid") or ev.get("_ems_lookupfeatureevidenceid_value", "")
        name = ev.get("ems_evidencename", "")
        # Evidence naming: "{feature} | {product} - {account}" or "{product} - {account}"
        if " | " in name:
            cust_part = name.split(" | ", 1)[1].strip()
        else:
            cust_part = name
        # Extract account name after last " - "
        if " - " in cust_part:
            cust_name = cust_part.rsplit(" - ", 1)[-1].strip()
        else:
            cust_name = cust_part.strip()
        if feat_id and cust_name:
            crm_customer_map.setdefault(feat_id, []).append(cust_name)
    print(f"  ✓ {len(evidence_records)} evidence records across {len(crm_customer_map)} features")

    # Step 3: Build feature data array
    features_data = []
    linked_features = {}  # feature_guid → uatId
    for f in raw_features:
        guid = f.get("ems_productfeatureid", "")
        uat_id = f.get("ems_onelist")
        uat_url = f.get("ems_onelisturl", "")
        uat_str = str(uat_id) if uat_id else ""
        if uat_id:
            linked_features[guid] = uat_id

        features_data.append({
            "id": guid,
            "featureId": f.get("ems_featureid", ""),
            "name": f.get("ems_featurename", ""),
            "description": f.get("ems_featurdescription", ""),
            "status": f.get("ems_featurestatus"),
            "priority": f.get("ems_priorityrating"),
            "created": f.get("createdon", ""),
            "modified": f.get("modifiedon", ""),
            "customers": f.get("ems_totalnumberofcustomers", 0),
            "impactScore": None,
            "keywords": None,
            "categoryId": f.get("_ems_featurecategory_value"),
            "uatId": uat_str,
            "uatUrl": uat_url or "",
            "engAdoId": f.get("ems_tfsid") or "",
            "engAdoUrl": f.get("ems_tfsidurl") or "",
            "crmCustomers": list(set(crm_customer_map.get(guid, []))),
        })
    print(f"  ✓ {len(linked_features)} features linked to ADO")

    # Step 4: Fetch ADO customers for linked features
    print("  🔍 Fetching ADO customers for linked features...")
    ado_customers = {}
    for guid, uat_id in linked_features.items():
        try:
            item = ado_get(ado_token, f"/wit/workitems/{uat_id}?$expand=relations&api-version=7.0")
            child_ids = [int(r["url"].split("/")[-1]) for r in item.get("relations", [])
                        if "Hierarchy-Forward" in r.get("rel", "")]
            if child_ids:
                batch = ado_post(ado_token, "/wit/workitemsbatch?api-version=7.0", {
                    "ids": child_ids, "fields": ["Custom.Account"]
                })
                names = list(set(c["fields"].get("Custom.Account", "") for c in batch.get("value", [])
                            if c["fields"].get("Custom.Account")))
                if names:
                    ado_customers[str(uat_id)] = names
        except Exception as e:
            print(f"    ⚠️ ADO #{uat_id}: {str(e)[:80]}")
    print(f"  ✓ ADO customers for {len(ado_customers)} items")

    # Step 5: Get ADO items for cross-referencing (Similar in ADO)
    print("  🔍 Building ADO cross-reference data...")
    ado_items, ado_titles = _fetch_ado_filtered_items(ado_token)

    # Get GitHub token for LLM scoring
    gh_token = None
    if not skip_llm:
        try:
            gh_result = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
            if gh_result.returncode == 0:
                gh_token = gh_result.stdout.strip()
        except Exception:
            pass
    if not gh_token and not skip_llm:
        print("    ⚠️ No GitHub token available, falling back to keyword matching")

    _score_cache = _load_score_cache()
    _cache_hits = 0

    def llm_score_ado_batch(feat_title, feat_desc, candidates):
        """Use LLM to score semantic similarity of ADO candidates against a CRM feature."""
        nonlocal _cache_hits
        if not gh_token or not candidates:
            return {}
        cand_lines = []
        for i, c in enumerate(candidates):
            cand_lines.append(f'{i+1}. [ADO #{c["id"]}] Title: {c["title"]}\n   Desc: {c["desc"][:300]}')
        candidates_text = "\n".join(cand_lines)
        source_text = f"{feat_title}\n{feat_desc[:500]}"

        # Check cache
        ckey = _cache_key(source_text, candidates_text)
        if ckey in _score_cache:
            _cache_hits += 1
            return _score_cache[ckey]

        prompt = f"""Score the semantic similarity between this CRM Feature Request and each ADO Technical Feedback candidate.
Consider whether they discuss the SAME topic, capability, or customer need — even if worded differently.

CRM Feature Request:
Title: {feat_title}
Description: {feat_desc[:500]}

Candidates:
{candidates_text}

For each candidate, respond with ONLY a JSON array of objects with "index" (1-based), "score" (0-100 integer), and "reason" (a brief 8-15 word explanation of why this score was given).
Score meaning: 0=unrelated, 20=vaguely related topic, 40=related area, 60=similar need, 80=very similar, 95+=nearly identical request.
Respond with ONLY the JSON array, no other text."""

        import time as _time
        for attempt in range(4):
            try:
                req_data = json.dumps({"model": "gpt-4o-mini", "messages": [{"role": "user", "content": prompt}], "max_tokens": 1500, "temperature": 0.1}).encode()
                req = urllib.request.Request(
                    "https://models.inference.ai.azure.com/chat/completions",
                    data=req_data,
                    headers={"Authorization": f"Bearer {gh_token}", "Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=30) as resp:
                    content = json.loads(resp.read())["choices"][0]["message"]["content"].strip()
                    content = content.replace("```json", "").replace("```", "").strip()
                    scores = json.loads(content)
                    result = {s["index"]: {"score": min(100, max(0, int(s["score"]))), "reason": s.get("reason", "")} for s in scores if "index" in s and "score" in s}
                    _score_cache[ckey] = result
                    _time.sleep(1)  # pace requests to avoid rate limits
                    return result
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < 3:
                    wait = int(e.headers.get("Retry-After", 2 ** (attempt + 1)))
                    _time.sleep(wait)
                    continue
                elif e.code == 404:
                    print(f"    ⚠️ LLM endpoint not found (404) — skipping LLM scoring")
                    break
                else:
                    print(f"    ⚠️ LLM scoring error: HTTP {e.code}: {str(e)[:80]}")
            except Exception as e:
                print(f"    ⚠️ LLM scoring error: {str(e)[:80]}")
                break
        return {}

    # Keyword pre-filter setup
    stopwords = {'the','a','an','is','are','was','were','be','been','being','have','has','had',
                 'do','does','did','will','would','could','should','may','might','shall','can',
                 'to','of','in','for','on','with','at','by','from','as','into','through','during',
                 'before','after','above','below','between','out','off','over','under','again',
                 'further','then','once','and','but','or','nor','not','so','yet','both','either',
                 'neither','each','every','all','any','few','more','most','other','some','such',
                 'no','only','own','same','than','too','very','just','because','if','when','while',
                 'that','this','these','those','it','its','they','them','their','we','our','you',
                 'your','he','she','his','her','which','what','who','whom','how','where','why',
                 'also','about','up','there','here','new','need','able','like','want','use','using',
                 'used','customer','customers','ability','feature','request','support','currently',
                 'data','policy','policies','content','information','microsoft','purview','m365'}

    def tokenize_text(text):
        words = set(re.findall(r'[a-z][a-z0-9]+', text.lower()))
        return words - stopwords

    # Pre-tokenize and prepare ADO items
    ado_prepared = []
    for ado_item in ado_items:
        title = ado_item['title']
        desc = strip_html(ado_item.get('description', '') or '')
        tokens = tokenize_text(title) | tokenize_text(desc)
        ado_prepared.append({
            "id": ado_item["id"],
            "title": title,
            "desc": desc[:500],
            "tokens": tokens,
        })

    ado_matches = {}
    scored_count = 0
    # Sort features by most recent date first so we prioritise recent items for LLM scoring
    features_by_date = sorted(features_data, key=lambda f: f.get('modified') or f.get('created') or '', reverse=True)
    LLM_BATCH_LIMIT = 100
    total_to_score = len([f for f in features_by_date if len(tokenize_text(f['name'] or '') | tokenize_text(strip_html(f['description'] or ''))) >= 2])
    for feat_idx, feat in enumerate(features_by_date):
        feat_title = feat['name'] or ''
        feat_desc = strip_html(feat['description'] or '')
        feat_tokens = tokenize_text(feat_title) | tokenize_text(feat_desc)
        if len(feat_tokens) < 2:
            continue

        # Keyword pre-filter: top 15 ADO candidates by token overlap
        pre_scores = []
        for ado in ado_prepared:
            shared = len(feat_tokens & ado["tokens"])
            if shared >= 2:
                union = len(feat_tokens | ado["tokens"])
                pre_scores.append((shared / union if union else 0, ado))
        pre_scores.sort(key=lambda x: x[0], reverse=True)
        candidates = [ps[1] for ps in pre_scores[:15]]

        if not candidates:
            continue

        # LLM semantic scoring (limited to first 100 items by date)
        if gh_token and scored_count < LLM_BATCH_LIMIT:
            llm_scores = llm_score_ado_batch(feat_title, feat_desc, candidates)
            scored_count += 1
            if scored_count % 10 == 0:
                batch_total = min(LLM_BATCH_LIMIT, total_to_score)
                print(f"    ... scored {scored_count}/{batch_total} ({_cache_hits} from cache)")
                pct = 15 + int(30 * scored_count / max(batch_total, 1))
                _report_progress(f"AI scoring Feature Requests... ({scored_count}/{batch_total})", pct)
        else:
            llm_scores = {}

        matches = []
        for i, c in enumerate(candidates):
            score_data = llm_scores.get(i + 1)
            if score_data is not None and isinstance(score_data, dict):
                score = score_data["score"]
                reason = score_data.get("reason", "")
            elif score_data is not None:
                score = min(100, max(0, int(score_data)))
                reason = ""
            else:
                score = min(100, round(pre_scores[i][0] * 100))
                reason = "Keyword match"
            if score >= 20:
                matches.append({"adoId": c["id"], "score": score, "reason": reason})

        # Force the linked ADO item to 100%
        linked_uat = feat.get("uatId")
        if linked_uat:
            existing = next((m for m in matches if m["adoId"] == linked_uat), None)
            if existing:
                existing["score"] = 100
                existing["reason"] = "Linked item"
            else:
                matches.append({"adoId": linked_uat, "score": 100, "reason": "Linked item"})

        matches.sort(key=lambda m: m["score"], reverse=True)
        if matches:
            ado_matches[feat["featureId"]] = matches[:5]

    print(f"  ✓ {len(ado_matches)} features with ADO cross-references ({scored_count} LLM-scored of {min(LLM_BATCH_LIMIT, total_to_score)} batch, {_cache_hits} cached)")
    if gh_token:
        _save_score_cache(_score_cache)

    # Step 5b: Build Engineering ADO cross-reference (similar in Engineering ADO)
    print("  🔍 Building Engineering ADO cross-reference data...")
    eng_ado_matches = {}
    eng_title_map = {}
    try:
        # Fetch engineering items using WIQL
        wiql_url = "https://o365exchange.visualstudio.com/IP%20Engineering/_apis/wit/wiql?api-version=7.0"
        wiql_body = json.dumps({"query": f"SELECT [System.Id] FROM workitems WHERE [System.AreaPath] = '{ENG_ADO_AREA_PATH}' AND [System.WorkItemType] = 'Feature' ORDER BY [System.Id] DESC"}).encode()
        wiql_req = urllib.request.Request(wiql_url, data=wiql_body, headers={"Authorization": f"Bearer {ado_token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(wiql_req, timeout=30) as resp:
                eng_wiql = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                print(f"    ⚠️ Engineering ADO project/query not found (404)")
                print(f"       URL: {wiql_url}")
                print(f"       Area Path: {ENG_ADO_AREA_PATH}")
                print(f"       Skipping Engineering ADO cross-reference")
                eng_ado_matches = {}
            else:
                raise
            return
        
        eng_ids = [w["id"] for w in eng_wiql.get("workItems", [])]
        if eng_ids:
            print(f"    Found {len(eng_ids)} Engineering ADO features")

            # Fetch details in batches
            eng_fields = "System.Id,System.Title,System.Description"
            eng_items_raw = []
            for i in range(0, len(eng_ids), 200):
                batch_ids = ",".join(str(x) for x in eng_ids[i:i+200])
                url = f"https://o365exchange.visualstudio.com/IP%20Engineering/_apis/wit/workitems?ids={batch_ids}&fields={eng_fields}&api-version=7.0"
                req = urllib.request.Request(url, headers={"Authorization": f"Bearer {ado_token}"})
                try:
                    with urllib.request.urlopen(req, timeout=30) as resp:
                        batch_data = json.loads(resp.read())
                    eng_items_raw.extend(batch_data.get("value", []))
                except urllib.error.HTTPError as e:
                    if e.code == 404:
                        print(f"    ⚠️ Engineering ADO items batch not found (404) — skipping remaining batches")
                        break
                    else:
                        raise

            # Prepare engineering items for matching
            eng_prepared = []
            eng_title_map = {}
            for item in eng_items_raw:
                f = item["fields"]
                eid = f.get("System.Id", item["id"])
                title = f.get("System.Title", "")
                desc = strip_html(f.get("System.Description", "") or "")
                tokens = tokenize_text(title) | tokenize_text(desc)
                eng_prepared.append({"id": eid, "title": title, "desc": desc[:500], "tokens": tokens})
                eng_title_map[str(eid)] = title

            # Score each CRM feature against engineering items (sorted by date, limited to 100)
            eng_scored_count = 0
            features_by_date_eng = sorted(features_data, key=lambda f: f.get('modified') or f.get('created') or '', reverse=True)
            for feat in features_by_date_eng:
                feat_title = feat['name'] or ''
                feat_desc = strip_html(feat['description'] or '')
                feat_tokens = tokenize_text(feat_title) | tokenize_text(feat_desc)
                if len(feat_tokens) < 2:
                    continue

                # Keyword pre-filter: top 15 engineering candidates
                pre_scores = []
                for eng in eng_prepared:
                    shared = len(feat_tokens & eng["tokens"])
                    if shared >= 2:
                        union = len(feat_tokens | eng["tokens"])
                        pre_scores.append((shared / union if union else 0, eng))
                pre_scores.sort(key=lambda x: x[0], reverse=True)
                candidates = [ps[1] for ps in pre_scores[:15]]

                if not candidates:
                    continue

                # LLM semantic scoring (reuse same cache and function, limited to 100)
                if gh_token and eng_scored_count < 100:
                    llm_scores = llm_score_ado_batch(feat_title, feat_desc, candidates)
                    eng_scored_count += 1
                    if eng_scored_count % 10 == 0:
                        print(f"    ... eng scored {eng_scored_count}")
                else:
                    llm_scores = {}

                matches = []
                for i, c in enumerate(candidates):
                    score = llm_scores.get(i + 1) or llm_scores.get(str(i + 1))
                    if score is None:
                        score = min(100, round(pre_scores[i][0] * 100))
                    if score >= 20:
                        matches.append({"adoId": c["id"], "score": score})

                # Force linked Engineering ADO to 100%
                linked_eng = feat.get("engAdoId")
                if linked_eng:
                    linked_eng_int = int(linked_eng) if str(linked_eng).isdigit() else None
                    if linked_eng_int:
                        existing = next((m for m in matches if m["adoId"] == linked_eng_int), None)
                        if existing:
                            existing["score"] = 100
                        else:
                            matches.append({"adoId": linked_eng_int, "score": 100})

                matches.sort(key=lambda m: m["score"], reverse=True)
                if matches:
                    eng_ado_matches[feat["featureId"]] = matches[:5]

            if eng_ado_matches:
                print(f"  ✓ {len(eng_ado_matches)} features with Engineering ADO cross-references ({eng_scored_count} scored)")
            if gh_token:
                _save_score_cache(_score_cache)
        else:
            print(f"    No Engineering ADO features found in {ENG_ADO_AREA_PATH}")
    except Exception as e:
        print(f"  ⚠️ Engineering ADO cross-reference failed: {e}")

    # Step 6: Update HTML
    html = open(filepath).read()
    html = replace_data_block(html, 'FEATURE_REQUESTS_DATA', json.dumps(features_data, ensure_ascii=True))
    html = replace_data_block(html, 'ADO_CUSTOMERS', json.dumps(ado_customers, ensure_ascii=True))
    html = replace_data_block(html, 'ADO_MATCHES', json.dumps(ado_matches, ensure_ascii=True))
    html = replace_data_block(html, 'ADO_TITLES', json.dumps(ado_titles, ensure_ascii=True))
    html = replace_data_block(html, 'ENG_ADO_MATCHES', json.dumps(eng_ado_matches, ensure_ascii=True))
    html = replace_data_block(html, 'ENG_ADO_TITLES', json.dumps(eng_title_map if eng_title_map else {}, ensure_ascii=True))

    today = _format_generated_time(datetime.now())
    html = re.sub(r'Generated \d+ \w+ \d{4}(?: at \d{2}:\d{2})?', f'Generated {today}', html)
    html = re.sub(r'Product: [^&]+&bull;', f'Product: {PURVIEW_AI_PRODUCT_NAME} &bull;', html)

    open(filepath, 'w').write(html)
    print(f"  ✅ Updated purview-ai-feature-requests.html ({len(features_data)} features)")


# ── Page 3: Technical Feedback ─────────────────────────────────────────────────

def refresh_feedback(ado_token, crm_token=None, skip_llm=False):
    filepath = os.path.join(SCRIPT_DIR, "purview-ai-technical-feedback.html")
    if not os.path.exists(filepath):
        print("  ⚠️  purview-ai-technical-feedback.html not found — skipping")
        return

    print("\n📋 Refreshing Technical Feedback...")

    # Fetch filtered ADO items — all items for this service (no state filter)
    ado_items, ado_titles = _fetch_ado_filtered_items(
        ado_token, active_states=None
    )

    # Build ADO ID → Feature Request ID mapping from CRM
    linked_fr = {}  # ado_id (str) → { featureId, featureName }
    if crm_token:
        print("  🔍 Fetching linked Feature Requests from CRM...")
        url_params = {
            "$filter": f"_ems_productname_value eq {PURVIEW_AI_PRODUCT_ID} and ems_onelist ne null",
            "$select": "ems_featureid,ems_featurename,ems_onelist,ems_productfeatureid",
            "$top": "500",
        }
        fr_data = crm_get(crm_token, "ems_productfeatures", url_params)
        for fr in fr_data.get("value", []):
            uat_id = fr.get("ems_onelist")
            if uat_id:
                linked_fr[str(uat_id)] = {
                    "featureId": fr.get("ems_featureid", ""),
                    "featureName": fr.get("ems_featurename", ""),
                    "guid": fr.get("ems_productfeatureid", ""),
                }
        print(f"  ✓ {len(linked_fr)} Feature Requests linked to ADO items")

    # Build rawData array
    raw_data = []
    for item in ado_items:
        raw_data.append({
            "id": item["id"],
            "title": item["title"],
            "state": item["state"],
            "substate": item.get("substate", ""),
            "priority": item["priority"],
            "created": item["created"],
            "changed": item["changed"],
            "assignedTo": item["assignedTo"],
            "tags": item["tags"],
            "description": strip_html(item.get("description", ""))[:300],
            "type": "Feature",
            "area": "Technical Feedback\\Security",
        })
    print(f"  ✓ {len(raw_data)} filtered Purview for AI items")

    # Fetch enrichment (child items) for all parents
    print("  🔍 Fetching evidence enrichment for all items...")
    enrichment = {}
    for item in ado_items:
        try:
            parent = ado_get(ado_token, f"/wit/workitems/{item['id']}?$expand=relations&api-version=7.0")
            child_ids = [int(r["url"].split("/")[-1]) for r in parent.get("relations", [])
                        if "Hierarchy-Forward" in r.get("rel", "")]
            if not child_ids:
                continue
            batch = ado_post(ado_token, "/wit/workitemsbatch?api-version=7.0", {
                "ids": child_ids,
                "fields": ["Custom.Account", "Custom.Industry", "Custom.TPID",
                           "Custom.TFTOptyName", "Custom.Blockertype",
                           "Custom.TFTOpportunityIntent", "Custom.TFTOpportunityStage",
                           "Custom.SalesPlay", "Custom.AreaField", "Custom.CustomerImpact"]
            })
            customers = []
            for c in batch.get("value", []):
                cf = c.get("fields", {})
                if not cf.get("Custom.Account"):
                    continue
                customers.append({
                    "name": cf.get("Custom.Account", ""),
                    "industry": cf.get("Custom.Industry", ""),
                    "area": cf.get("Custom.AreaField", ""),
                    "eou": "",
                    "tpid": cf.get("Custom.TPID", ""),
                    "optyName": cf.get("Custom.TFTOptyName", ""),
                    "optyIntent": cf.get("Custom.TFTOpportunityIntent", ""),
                    "optyStage": cf.get("Custom.TFTOpportunityStage", ""),
                    "blocker": cf.get("Custom.Blockertype", ""),
                    "salesPlay": cf.get("Custom.SalesPlay", ""),
                    "solutionArea": "",
                    "impact": strip_html(cf.get("Custom.CustomerImpact", ""))[:300],
                    "id": c["id"],
                })
            if customers:
                enrichment[str(item["id"])] = {
                    "evidenceCount": len(customers),
                    "customers": customers,
                }
        except Exception as e:
            print(f"    ⚠️ Item #{item['id']}: {str(e)[:80]}")

    total_evidence = sum(e["evidenceCount"] for e in enrichment.values())
    print(f"  ✓ Enrichment: {len(enrichment)} items with {total_evidence} evidence records")

    # Build Similar FRs matching (ADO item → CRM Feature Requests by LLM semantic similarity)
    similar_frs = {}
    if crm_token:
        print("  🔍 Building Similar Feature Requests matching...")
        one_year_ago = (datetime.now(timezone.utc).replace(year=datetime.now(timezone.utc).year - 1)).strftime("%Y-%m-%dT00:00:00Z")
        fr_url_params = {
            "$filter": f"_ems_productname_value eq {PURVIEW_AI_PRODUCT_ID} and createdon ge {one_year_ago}",
            "$select": "ems_featureid,ems_featurename,ems_featurdescription,ems_productfeatureid",
            "$top": "500",
        }
        all_frs = crm_get(crm_token, "ems_productfeatures", fr_url_params).get("value", [])
        print(f"    Found {len(all_frs)} Purview for AI MFRs from the last year")

        # Prepare FR summaries for matching
        fr_data = []
        for fr in all_frs:
            title = fr.get('ems_featurename', '') or ''
            desc = strip_html(fr.get('ems_featurdescription', '') or '')
            fr_data.append({
                "featureId": fr.get("ems_featureid", ""),
                "name": title,
                "desc": desc[:500],
                "guid": fr.get("ems_productfeatureid", ""),
            })

        # Get GitHub token for Models API
        gh_token = None
        if not skip_llm:
            try:
                gh_result = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
                if gh_result.returncode == 0:
                    gh_token = gh_result.stdout.strip()
            except Exception:
                pass

        if not gh_token and not skip_llm:
            print("    ⚠️ No GitHub token available, falling back to keyword matching")

        _score_cache2 = _load_score_cache()
        _cache_hits2 = 0

        def llm_score_batch(ado_title, ado_desc, candidates):
            """Use LLM to score semantic similarity of candidates against an ADO item."""
            nonlocal _cache_hits2
            if not gh_token or not candidates:
                return {}
            cand_lines = []
            for i, c in enumerate(candidates):
                cand_lines.append(f'{i+1}. [{c["featureId"]}] Title: {c["name"]}\n   Desc: {c["desc"][:300]}')
            candidates_text = "\n".join(cand_lines)
            source_text = f"{ado_title}\n{ado_desc[:500]}"

            # Check cache
            ckey = _cache_key(source_text, candidates_text)
            if ckey in _score_cache2:
                _cache_hits2 += 1
                return _score_cache2[ckey]

            prompt = f"""Score the semantic similarity between this ADO work item and each CRM Feature Request candidate.
Consider whether they discuss the SAME topic, capability, or customer need — even if worded differently.
Use your understanding of the context and meaning behind the titles and descriptions, not just keyword overlap.

ADO Work Item:
Title: {ado_title}
Description: {ado_desc[:500]}

Candidates:
{candidates_text}

For each candidate, respond with ONLY a JSON array of objects with "index" (1-based), "score" (0-100 integer), and "reason" (a brief 8-15 word explanation of why this score was given).
Score meaning: 0=unrelated, 20=vaguely related topic, 40=related area, 60=similar need, 80=very similar, 95+=nearly identical request.
Respond with ONLY the JSON array, no other text."""

            import time as _time
            for attempt in range(4):
                try:
                    req_data = json.dumps({"model": "gpt-4o-mini", "messages": [{"role": "user", "content": prompt}], "max_tokens": 1500, "temperature": 0.1}).encode()
                    req = urllib.request.Request(
                        "https://models.inference.ai.azure.com/chat/completions",
                        data=req_data,
                        headers={"Authorization": f"Bearer {gh_token}", "Content-Type": "application/json"},
                    )
                    with urllib.request.urlopen(req, timeout=30) as resp:
                        content = json.loads(resp.read())["choices"][0]["message"]["content"].strip()
                        content = content.replace("```json", "").replace("```", "").strip()
                        scores = json.loads(content)
                        result = {s["index"]: {"score": min(100, max(0, int(s["score"]))), "reason": s.get("reason", "")} for s in scores if "index" in s and "score" in s}
                        _score_cache2[ckey] = result
                        _time.sleep(1)  # pace requests to avoid rate limits
                        return result
                except urllib.error.HTTPError as e:
                    if e.code == 429 and attempt < 3:
                        wait = int(e.headers.get("Retry-After", 2 ** (attempt + 1)))
                        _time.sleep(wait)
                        continue
                    elif e.code == 404:
                        print(f"    ⚠️ LLM endpoint not found (404) — skipping LLM scoring")
                        break
                    else:
                        print(f"    ⚠️ LLM scoring error: HTTP {e.code}: {str(e)[:80]}")
                except Exception as e:
                    print(f"    ⚠️ LLM scoring error: {str(e)[:80]}")
                    break
            return {}

        # Keyword pre-filter: quick Jaccard to find top ~15 candidates per ADO item
        stopwords = {'the','a','an','is','are','was','were','be','been','being','have','has','had',
                     'do','does','did','will','would','could','should','may','might','shall','can',
                     'to','of','in','for','on','with','at','by','from','as','into','through','during',
                     'before','after','above','below','between','out','off','over','under','again',
                     'further','then','once','and','but','or','nor','not','so','yet','both','either',
                     'neither','each','every','all','any','few','more','most','other','some','such',
                     'no','only','own','same','than','too','very','just','because','if','when','while',
                     'that','this','these','those','it','its','they','them','their','we','our','you',
                     'your','he','she','his','her','which','what','who','whom','how','where','why',
                     'also','about','up','there','here','new','need','able','like','want','use','using',
                     'used','customer','customers','ability','feature','request','support','currently',
                     'data','policy','policies','content','information','microsoft','purview','m365'}
        def tokenize(text):
            words = set(re.findall(r'[a-z][a-z0-9]+', text.lower()))
            return words - stopwords

        # Pre-tokenize FRs (title only for UAT comparison)
        for fr in fr_data:
            fr["tokens"] = tokenize(fr["name"])

        scored_count = 0
        # Sort ADO items by most recent date first for LLM scoring priority
        ado_items_by_date = sorted(ado_items, key=lambda x: x.get('changed') or x.get('created') or '', reverse=True)
        for item in ado_items_by_date:
            ado_title = item['title']
            ado_tokens = tokenize(ado_title)
            if len(ado_tokens) < 2:
                continue

            # Quick keyword pre-filter: find top 15 candidates by title token overlap
            pre_scores = []
            for fr in fr_data:
                shared = len(ado_tokens & fr["tokens"])
                if shared >= 1:
                    union = len(ado_tokens | fr["tokens"])
                    pre_scores.append((shared / union if union else 0, fr))
            pre_scores.sort(key=lambda x: x[0], reverse=True)
            candidates = [ps[1] for ps in pre_scores[:15]]

            if not candidates:
                continue

            # LLM semantic scoring (title-based, limited to first 100 items by date)
            if gh_token and scored_count < 100:
                llm_scores = llm_score_batch(ado_title, "", candidates)
                scored_count += 1
                if scored_count % 10 == 0:
                    _report_progress(f"AI scoring Technical Feedback... ({scored_count} items)", 45 + min(25, int(25 * scored_count / 100)))
            else:
                llm_scores = {}

            matches = []
            for i, c in enumerate(candidates):
                score_data = llm_scores.get(i + 1)
                if score_data is not None and isinstance(score_data, dict):
                    score = score_data["score"]
                    reason = score_data.get("reason", "")
                elif score_data is not None:
                    score = min(100, max(0, int(score_data)))
                    reason = ""
                else:
                    # Fallback: use keyword score scaled to 0-100
                    score = min(100, round(pre_scores[i][0] * 100))
                    reason = "Keyword match"
                if score >= 20:
                    matches.append({
                        "featureId": c["featureId"],
                        "name": c["name"],
                        "guid": c["guid"],
                        "score": score,
                        "reason": reason,
                    })

            # Force linked FR to 100%
            ado_id_str = str(item["id"])
            if ado_id_str in linked_fr:
                linked_fid = linked_fr[ado_id_str]["featureId"]
                existing = next((m for m in matches if m["featureId"] == linked_fid), None)
                if existing:
                    existing["score"] = 100
                    existing["reason"] = "Linked item"
                else:
                    lf = linked_fr[ado_id_str]
                    matches.append({"featureId": lf["featureId"], "name": lf.get("featureName", ""), "guid": lf.get("guid", ""), "score": 100, "reason": "Linked item"})

            matches.sort(key=lambda m: m["score"], reverse=True)
            if matches:
                similar_frs[str(item["id"])] = matches[:5]

        print(f"  ✓ {len(similar_frs)} items with similar Feature Requests ({scored_count} LLM-scored, {_cache_hits2} cached)")
        if gh_token:
            _save_score_cache(_score_cache2)
    # Update HTML
    html = open(filepath).read()
    html = replace_data_block(html, 'rawData', json.dumps(raw_data, ensure_ascii=True))
    html = replace_data_block(html, 'ENRICHMENT', json.dumps(enrichment, ensure_ascii=True))
    html = replace_data_block(html, 'LINKED_FR', json.dumps(linked_fr, ensure_ascii=True))
    html = replace_data_block(html, 'SIMILAR_FRS', json.dumps(similar_frs, ensure_ascii=True))

    today = _format_generated_time(datetime.now())
    html = re.sub(r'Generated \d+ \w+ \d{4}(?: at \d{2}:\d{2})?', f'Generated {today}', html)

    open(filepath, 'w').write(html)
    print(f"  ✅ Updated purview-ai-technical-feedback.html ({len(raw_data)} items)")



# ── Shared: ADO item fetching and filtering ────────────────────────────────────

def _fetch_ado_filtered_items(ado_token, assigned_to=None, active_states=None):
    """Fetch ADO items for configured service names, return items + titles dict.
    If assigned_to is a list of names, only include items assigned to those people.
    If active_states is a list of states, only include items in those states."""
    all_ids = []
    service_names = UAT_SERVICE_NAMES or ["Purview for AI"]
    service_names_lower = {n.lower() for n in service_names}
    
    # Try saved query first, but fall back to direct service name query if it fails
    try:
        query_data = ado_get(ado_token, "/wit/wiql/fe2a745c-3561-4e98-99ba-222b358e5971?api-version=7.0")
        all_ids = [wi["id"] for wi in query_data.get("workItems", [])]
        print(f"  ✓ Saved query returned {len(all_ids)} work items")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print(f"  ⚠️  Saved query not found (404), falling back to service name query")
            print(f"  ⚠️  Saved query not found (404)")
        else:
            raise

    # Always query directly by configured service name(s) so admin filter is fully honored.
    service_clauses = " OR ".join([f"[Custom.AZServiceName] = '{name}'" for name in service_names])
    wiql = f"SELECT [System.Id] FROM WorkItems WHERE ({service_clauses})"
    service_query_data = ado_post(ado_token, "/wit/wiql?api-version=7.0", {"query": wiql})
    service_ids = [wi["id"] for wi in service_query_data.get("workItems", [])]
    added_service_ids = 0
    for sid in service_ids:
        if sid not in all_ids:
            all_ids.append(sid)
            added_service_ids += 1
    print(f"  ✓ Service-name query returned {len(service_ids)} work items ({added_service_ids} added)")

    # Add manually included items
    for mid in ADO_MANUAL_INCLUDE:
        if mid not in all_ids:
            all_ids.append(mid)

    # If filtering by assigned_to, also query directly for their Purview for AI items
    if assigned_to:
        assignee_clauses = " OR ".join([f"[System.AssignedTo] = '{name}'" for name in assigned_to])
        wiql = f"SELECT [System.Id] FROM WorkItems WHERE ({assignee_clauses}) AND ({service_clauses})"
        extra_data = ado_post(ado_token, "/wit/wiql?api-version=7.0", {"query": wiql})
        extra_ids = [wi["id"] for wi in extra_data.get("workItems", [])]
        added = 0
        for eid in extra_ids:
            if eid not in all_ids:
                all_ids.append(eid)
                added += 1
        if added:
            print(f"  ✓ Added {added} additional items from direct assignee query")

     # Batch fetch all items
    items = []
    for i in range(0, len(all_ids), 200):
        batch = ado_post(ado_token, "/wit/workitemsbatch?api-version=7.0", {
            "ids": all_ids[i:i+200],
            "fields": ["System.Title", "System.State", "System.Tags",
                       "System.CreatedDate", "System.ChangedDate",
                       "System.AssignedTo", "System.Description",
                       "Microsoft.VSTS.Common.Priority", "Custom.SolutionArea",
                       "Custom.AZServiceName", "Custom.TFTSubState", "System.WorkItemType", "Custom.OfferingName", "System.AreaPath"]
        })
        items.extend(batch.get("value", []))

    # Filter by service name field and WorkItemType=Feature
    active_states_lower = [s.lower() for s in active_states] if active_states else None
    
    # Count at each filter stage
    feature_count = 0
    service_name_count = 0
    
    filtered = []
    for item in items:
        f = item.get("fields", {})
        # Check Work Item Type
        work_item_type = (f.get("System.WorkItemType") or "").lower()
        if work_item_type != "feature":
            continue
        feature_count += 1
        
        # Check AZ Service Name
        service_name = (f.get("Custom.AZServiceName") or "").lower()
        if service_name not in service_names_lower:
            continue
        service_name_count += 1
        
        # Check state if specified
        if active_states_lower:
            item_state = (f.get("System.State") or "").lower()
            if item_state not in active_states_lower:
                continue
        assigned = f.get("System.AssignedTo", {})
        filtered.append({
            "id": item["id"],
            "title": f.get("System.Title", ""),
            "state": f.get("System.State", ""),
            "substate": f.get("Custom.TFTSubState", ""),
            "priority": f.get("Microsoft.VSTS.Common.Priority", 3),
            "created": f.get("System.CreatedDate", ""),
            "changed": f.get("System.ChangedDate", ""),
            "assignedTo": assigned.get("displayName", "") if isinstance(assigned, dict) else str(assigned or ""),
            "tags": f.get("System.Tags", ""),
            "description": f.get("System.Description", ""),
        })
    
    print(f"    Filter breakdown: {feature_count} Features, {service_name_count} in {service_names}, {len(filtered)} final")

    # Optional: filter by assigned person
    if assigned_to:
        assigned_lower = [name.lower() for name in assigned_to]
        filtered = [item for item in filtered if item["assignedTo"].lower() in assigned_lower]
        print(f"  ✓ Filtered to {len(filtered)} items assigned to {', '.join(assigned_to)}")
    else:
        state_note = f" in states {active_states}" if active_states else ""
        print(f"  ✓ Filtered to {len(filtered)} items for service filter {service_names}{state_note}")

    # Build titles dict
    titles = {str(item["id"]): item["title"] for item in filtered}

    return filtered, titles


# ── Shared: Concept extraction for cross-referencing ──────────────────────────

CONCEPT_PATTERNS = [
    (r'copilot.{0,5}dlp|dlp.{0,5}copilot', 'copilot_dlp'),
    (r'dspm', 'dspm'), (r'gcch|gcc.?high', 'gcch'),
    (r'\bsit\b|sensitive.?info', 'sit'), (r'block.{0,5}copilot|copilot.{0,5}block', 'block_copilot'),
    (r'\bocr\b', 'ocr'), (r'file.?upload', 'file_upload'),
    (r'sensitivity.?label', 'sensitivity_label'), (r'auto.?label', 'auto_label'),
    (r'data.?risk|risk.?assess', 'data_risk'), (r'oversha', 'oversharing'),
    (r'ai.?hub', 'ai_hub'), (r'ai.?agent', 'ai_agent'),
    (r'ai.?govern', 'ai_governance'), (r'ai.?activity', 'ai_activity'),
    (r'prompt.*response|response.*prompt', 'prompt_response'),
    (r'admin.?unit', 'admin_unit'), (r'incident.?sever', 'incident_severity'),
    (r'edge.?dlp|browser.?dlp|endpoint.?dlp', 'endpoint_dlp'),
    (r'network.?dlp|inline.?dlp', 'network_dlp'),
    (r'grounding', 'grounding'), (r'foundry', 'foundry'),
    (r'fabric', 'fabric'), (r'unlabel', 'unlabeled'),
    (r'encryption|encrypt', 'encryption'), (r'email|exchange', 'email'),
    (r'attach', 'attachment'), (r'3p.?ai|third.?party.?ai', 'third_party_ai'),
    (r'billing|pay.?go|payg|charge.?back', 'billing'),
    (r'sentinel', 'sentinel'), (r'alert', 'alert'),
    (r'assessment|assess', 'assessment'), (r'report|dashboard', 'reporting'),
]

def _extract_concepts(text):
    concepts = set()
    for pattern, concept in CONCEPT_PATTERNS:
        if re.search(pattern, text):
            concepts.add(concept)
    return concepts


# ── Engineering ADO Refresh ─────────────────────────────────────────────────────

def refresh_engineering_ado(ado_token, crm_token=None, skip_llm=False):
    """Refresh the Engineering ADO page with work items from IP Engineering area."""
    print("📋 Refreshing Engineering ADO page...")
    filepath = os.path.join(SCRIPT_DIR, "purview-ai-engineering-ado.html")
    if not os.path.exists(filepath):
        print("  ⚠️ purview-ai-engineering-ado.html not found, skipping")
        return

    area_path = ENG_ADO_AREA_PATH
    wiql = f"SELECT [System.Id] FROM workitems WHERE [System.AreaPath] = '{area_path}' AND [System.WorkItemType] = 'Feature' ORDER BY [System.Id] DESC"

    headers = {"Authorization": f"Bearer {ado_token}", "Content-Type": "application/json"}
    wiql_url = "https://o365exchange.visualstudio.com/IP%20Engineering/_apis/wit/wiql?api-version=7.0"
    wiql_body = json.dumps({"query": wiql}).encode()
    wiql_result = None
    for _attempt in range(3):
        try:
            req = urllib.request.Request(wiql_url, data=wiql_body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw_body = resp.read()
                wiql_result = json.loads(raw_body)
            break
        except json.JSONDecodeError as e:
            print(f"    ⚠️ WIQL attempt {_attempt+1} JSON error: {e}")
            print(f"    Response body ({len(raw_body)} bytes): {raw_body[:300]}")
            if _attempt < 2:
                import time; time.sleep(5 * (_attempt + 1))
            else:
                print(f"    ⚠️ Failed to parse WIQL response after 3 attempts — skipping Engineering ADO refresh")
                return
        except urllib.error.HTTPError as e:
            if e.code == 404:
                print(f"    ⚠️ Engineering ADO project not found (404) — skipping Engineering ADO refresh")
                print(f"       URL: {wiql_url}")
                print(f"       Area Path: {area_path}")
                return
            elif _attempt < 2:
                print(f"    ⚠️ WIQL attempt {_attempt+1} failed: HTTP {e.code}, retrying...")
                import time; time.sleep(5 * (_attempt + 1))
            else:
                print(f"    ⚠️ WIQL failed after 3 attempts: HTTP {e.code} — skipping Engineering ADO refresh")
                return
        except urllib.error.URLError as e:
            if _attempt < 2:
                print(f"    ⚠️ WIQL attempt {_attempt+1} failed: {e}, retrying...")
                import time; time.sleep(5 * (_attempt + 1))
            else:
                print(f"    ⚠️ WIQL failed after 3 attempts: {e} — skipping Engineering ADO refresh")
                return
    
    if wiql_result is None:
        print(f"    ⚠️ Failed to get WIQL results — skipping Engineering ADO refresh")
        return
        
    all_ids = [w["id"] for w in wiql_result.get("workItems", [])]
    if all_ids:
        print(f"  Found {len(all_ids)} work items (Features only)")
    else:
        print(f"  No Engineering ADO features found in {area_path}")

    # Fetch details in batches of 200
    fields = "System.Id,System.Title,System.State,System.WorkItemType,System.AssignedTo,Microsoft.VSTS.Common.Priority,System.CreatedDate,System.ChangedDate,System.IterationPath,System.Tags,System.Description"
    all_items = []
    for i in range(0, len(all_ids), 200):
        batch_ids = ",".join(str(x) for x in all_ids[i:i+200])
        url = f"https://o365exchange.visualstudio.com/IP%20Engineering/_apis/wit/workitems?ids={batch_ids}&fields={fields}&api-version=7.0"
        req2 = urllib.request.Request(url, headers={"Authorization": f"Bearer {ado_token}"})
        try:
            with urllib.request.urlopen(req2, timeout=30) as resp2:
                batch_data = json.loads(resp2.read())
            all_items.extend(batch_data.get("value", []))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                print(f"    ⚠️ Engineering ADO items batch not found (404) for IDs {batch_ids[:100]}... — skipping remaining batches")
                break
            else:
                print(f"    ⚠️ Failed to fetch batch: HTTP {e.code} — skipping remaining batches")
                break

    # Transform into page data
    page_data = []
    for item in all_items:
        f = item["fields"]
        assigned = f.get("System.AssignedTo", {})
        page_data.append({
            "id": f.get("System.Id", item["id"]),
            "title": f.get("System.Title", ""),
            "state": f.get("System.State", ""),
            "type": f.get("System.WorkItemType", ""),
            "assignedTo": assigned.get("displayName", "") if isinstance(assigned, dict) else "",
            "priority": f.get("Microsoft.VSTS.Common.Priority", 0),
            "created": (f.get("System.CreatedDate", "") or "")[:10],
            "changed": (f.get("System.ChangedDate", "") or "")[:10],
            "iteration": (f.get("System.IterationPath", "") or "").replace("IP Engineering\\", ""),
            "tags": f.get("System.Tags", "") or "",
            "description": f.get("System.Description", "") or "",
        })

    # Build Similar CRM Feature Requests matching
    similar_crm = {}
    if skip_llm:
        # Preserve existing LLM scores from HTML on quick refresh
        try:
            existing_html = open(filepath).read()
            import re as _re
            m = _re.search(r'const SIMILAR_CRM = ({.*?});', existing_html)
            if m:
                similar_crm = json.loads(m.group(1))
                print("  🔍 Preserving existing Similar CRM scores (quick refresh)")
        except Exception:
            pass
    elif crm_token:
        print("  🔍 Building Similar CRM Feature Requests matching...")
        fr_url_params = {
            "$filter": f"_ems_productname_value eq {PURVIEW_AI_PRODUCT_ID}",
            "$select": "ems_featureid,ems_featurename,ems_featurdescription,ems_productfeatureid",
            "$top": "500",
        }
        all_frs = crm_get(crm_token, "ems_productfeatures", fr_url_params).get("value", [])
        print(f"    Found {len(all_frs)} Purview for AI CRM Feature Requests")

        # Prepare FR data
        fr_data = []
        for fr in all_frs:
            title = fr.get('ems_featurename', '') or ''
            desc = strip_html(fr.get('ems_featurdescription', '') or '')
            fr_data.append({
                "featureId": fr.get("ems_featureid", ""),
                "name": title,
                "desc": desc[:500],
                "guid": fr.get("ems_productfeatureid", ""),
            })

        # Get GitHub token for LLM scoring
        gh_token = None
        if not skip_llm:
            try:
                gh_result = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
                if gh_result.returncode == 0:
                    gh_token = gh_result.stdout.strip()
            except Exception:
                pass

        if not gh_token and not skip_llm:
            print("    ⚠️ No GitHub token available, falling back to keyword matching")

        _score_cache3 = _load_score_cache()
        _cache_hits3 = 0

        def llm_score_eng_batch(eng_title, eng_desc, candidates):
            """Use LLM to score semantic similarity of CRM FR candidates against an engineering work item."""
            nonlocal _cache_hits3
            if not gh_token or not candidates:
                return {}
            cand_lines = []
            for i, c in enumerate(candidates):
                cand_lines.append(f'{i+1}. [{c["featureId"]}] Title: {c["name"]}\n   Desc: {c["desc"][:300]}')
            candidates_text = "\n".join(cand_lines)
            source_text = f"{eng_title}\n{eng_desc[:500]}"

            ckey = _cache_key(source_text, candidates_text)
            if ckey in _score_cache3:
                _cache_hits3 += 1
                return _score_cache3[ckey]

            prompt = f"""Score the semantic similarity between this Engineering ADO work item and each CRM Feature Request candidate.
Consider whether they discuss the SAME topic, capability, or customer need — even if worded differently.

Engineering Work Item:
Title: {eng_title}
Description: {eng_desc[:500]}

Candidates:
{candidates_text}

For each candidate, respond with ONLY a JSON array of objects with "index" (1-based) and "score" (0-100 integer).
Score meaning: 0=unrelated, 20=vaguely related topic, 40=related area, 60=similar need, 80=very similar, 95+=nearly identical request.
Respond with ONLY the JSON array, no other text."""

            import time as _time
            for attempt in range(4):
                try:
                    req_data = json.dumps({"model": "gpt-4o-mini", "messages": [{"role": "user", "content": prompt}], "max_tokens": 500, "temperature": 0.1}).encode()
                    req3 = urllib.request.Request(
                        "https://models.inference.ai.azure.com/chat/completions",
                        data=req_data,
                        headers={"Authorization": f"Bearer {gh_token}", "Content-Type": "application/json"},
                    )
                    with urllib.request.urlopen(req3, timeout=30) as resp3:
                        content = json.loads(resp3.read())["choices"][0]["message"]["content"].strip()
                        content = content.replace("```json", "").replace("```", "").strip()
                        scores = json.loads(content)
                        result = {s["index"]: min(100, max(0, int(s["score"]))) for s in scores if "index" in s and "score" in s}
                        _score_cache3[ckey] = result
                        _time.sleep(1)
                        return result
                except urllib.error.HTTPError as e:
                    if e.code == 429 and attempt < 3:
                        wait = int(e.headers.get("Retry-After", 2 ** (attempt + 1)))
                        _time.sleep(wait)
                        continue
                    elif e.code == 404:
                        print(f"    ⚠️ LLM endpoint not found (404) — skipping LLM scoring")
                        break
                    else:
                        print(f"    ⚠️ LLM scoring error: HTTP {e.code}: {str(e)[:80]}")
                except Exception as e:
                    print(f"    ⚠️ LLM scoring error: {str(e)[:80]}")
                    break
            return {}

        # Keyword pre-filter setup
        stopwords = {'the','a','an','is','are','was','were','be','been','being','have','has','had',
                     'do','does','did','will','would','could','should','may','might','shall','can',
                     'to','of','in','for','on','with','at','by','from','as','into','through','during',
                     'before','after','above','below','between','out','off','over','under','again',
                     'further','then','once','and','but','or','nor','not','so','yet','both','either',
                     'neither','each','every','all','any','few','more','most','other','some','such',
                     'no','only','own','same','than','too','very','just','because','if','when','while',
                     'that','this','these','those','it','its','they','them','their','we','our','you',
                     'your','he','she','his','her','which','what','who','whom','how','where','why',
                     'also','about','up','there','here','new','need','able','like','want','use','using',
                     'used','customer','customers','ability','feature','request','support','currently',
                     'data','policy','policies','content','information','microsoft','purview','m365'}
        def tokenize(text):
            words = set(re.findall(r'[a-z][a-z0-9]+', text.lower()))
            return words - stopwords

        # Pre-tokenize CRM FRs
        for fr in fr_data:
            fr["tokens"] = tokenize(fr["name"]) | tokenize(fr["desc"])

        scored_count = 0
        for item in page_data:
            if item["type"] != "Feature":
                continue
            eng_title = item["title"]
            eng_desc = strip_html(item["description"])
            eng_tokens = tokenize(eng_title) | tokenize(eng_desc)
            if len(eng_tokens) < 2:
                continue

            # Keyword pre-filter: top 15 CRM FR candidates
            pre_scores = []
            for fr in fr_data:
                shared = len(eng_tokens & fr["tokens"])
                if shared >= 1:
                    union = len(eng_tokens | fr["tokens"])
                    pre_scores.append((shared / union if union else 0, fr))
            pre_scores.sort(key=lambda x: x[0], reverse=True)
            candidates = [ps[1] for ps in pre_scores[:15]]

            if not candidates:
                continue

            # LLM semantic scoring
            if gh_token:
                llm_scores = llm_score_eng_batch(eng_title, eng_desc, candidates)
                scored_count += 1
                if scored_count % 10 == 0:
                    print(f"    ... scored {scored_count} ({_cache_hits3} from cache)")
                    _report_progress(f"AI scoring Engineering ADO... ({scored_count} items)", 75 + min(20, int(20 * scored_count / 90)))
            else:
                llm_scores = {}

            matches = []
            for i, c in enumerate(candidates):
                score = llm_scores.get(i + 1) or llm_scores.get(str(i + 1))
                if score is None:
                    score = min(100, round(pre_scores[i][0] * 100))
                if score >= 20:
                    matches.append({
                        "featureId": c["featureId"],
                        "name": c["name"],
                        "score": score,
                        "id": c["guid"],
                    })

            matches.sort(key=lambda m: m["score"], reverse=True)
            if matches:
                similar_crm[str(item["id"])] = matches[:5]

        print(f"  ✓ {len(similar_crm)} items with similar CRM FRs ({scored_count} LLM-scored, {_cache_hits3} cached)")
        if gh_token:
            _save_score_cache(_score_cache3)

    # Build LINKED_CRM: query CRM for Feature Requests where ems_tfsid matches an Engineering ADO ID
    print("  🔗 Checking linked CRM Feature Requests...")
    eng_ids = set(str(item["id"]) for item in page_data)
    linked_crm = {}
    try:
        linked_frs = crm_get(crm_token, "ems_productfeatures", {
            "$filter": f"_ems_productname_value eq {PURVIEW_AI_PRODUCT_ID} and ems_tfsid ne null",
            "$select": "ems_featureid,ems_featurename,ems_tfsid,ems_productfeatureid",
            "$top": "500",
        })
        for fr in linked_frs.get("value", []):
            tfs_id = str(fr.get("ems_tfsid", "")).strip()
            if tfs_id in eng_ids:
                if tfs_id not in linked_crm:
                    linked_crm[tfs_id] = []
                linked_crm[tfs_id].append({
                    "featureId": fr.get("ems_featureid", ""),
                    "name": fr.get("ems_featurename", ""),
                    "id": fr.get("ems_productfeatureid", ""),
                })
        print(f"  ✓ {len(linked_crm)} Engineering items linked to CRM")
    except Exception as e:
        print(f"  ⚠️ Could not fetch linked CRM data: {e}")

    # Update HTML
    html = open(filepath).read()
    html = replace_data_block(html, 'rawData', json.dumps(page_data, ensure_ascii=True))
    html = replace_data_block(html, 'SIMILAR_CRM', json.dumps(similar_crm, ensure_ascii=True))
    html = replace_data_block(html, 'LINKED_CRM', json.dumps(linked_crm, ensure_ascii=True))
    today = _format_generated_time(datetime.now())
    html = re.sub(r'Generated \d+ \w+ \d{4}(?: at \d{2}:\d{2})?', f'Generated {today}', html)
    with open(filepath, 'w') as fp:
        fp.write(html)

    print(f"  ✅ Updated purview-ai-engineering-ado.html ({len(page_data)} items)")

    # Roadmap page now uses live API data — no embedded rawData to update


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Refresh Purview for AI dashboard pages")
    parser.add_argument("--page", choices=["features", "requests", "feedback", "engineering", "roadmap"],
                       help="Refresh a specific page (default: all)")
    parser.add_argument("--fast", action="store_true",
                       help="Skip LLM scoring for faster refresh")
    args = parser.parse_args()

    print(f"🔄 Purview for AI Dashboard Refresh — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)

    needs_crm = args.page in (None, "features", "requests", "feedback", "engineering", "roadmap")
    needs_ado = args.page in (None, "requests", "feedback", "engineering", "roadmap")

    _report_progress("Authenticating...", 0)
    print("🔐 Authenticating...")
    crm_token = get_token("https://m365crm.crm.dynamics.com") if needs_crm else None
    ado_token = get_token("499b84ac-1321-427f-aa17-267ca6975798") if needs_ado else None
    print("✅ Authenticated\n")

    if args.page in (None, "features"):
        _report_progress("Refreshing Field Submitted Features...", 5)
        refresh_features(crm_token)

    if args.page in (None, "requests"):
        _report_progress("Refreshing Feature Requests...", 15)
        refresh_requests(crm_token, ado_token, skip_llm=args.fast)

    if args.page in (None, "feedback"):
        _report_progress("Refreshing Technical Feedback (UATs)...", 45)
        refresh_feedback(ado_token, crm_token, skip_llm=args.fast)

    if args.page in (None, "engineering", "roadmap"):
        _report_progress("Refreshing Engineering ADO...", 75)
        refresh_engineering_ado(ado_token, crm_token, skip_llm=args.fast)

    _report_progress("Complete!", 100)
    print(f"\n{'=' * 60}")
    print(f"✅ Refresh complete at {datetime.now().strftime('%H:%M:%S')}")
    print("   Reload the pages in your browser to see the latest data.")


if __name__ == "__main__":
    main()

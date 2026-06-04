#!/usr/bin/env python3
"""Local HTTP server for dashboard refresh and ADO→CRM sync.

Start once:  python refresh_server.py
Then use the 🔄 Refresh and ⬆️ Sync buttons in any dashboard page.
"""

import http.server
import json
import subprocess
import sys
import os
import threading
import urllib.request
from urllib.parse import urlparse, parse_qs

PORT = 8765
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REFRESH_SCRIPT = os.path.join(SCRIPT_DIR, "refresh_dashboard.py")
SYNC_SCRIPT = os.path.join(SCRIPT_DIR, "sync_ado_to_crm.py")
PROMOTE_SCRIPT = os.path.join(SCRIPT_DIR, "promote_to_feature.py")
LINK_SCRIPT = os.path.join(SCRIPT_DIR, "link_uat_to_fr.py")
LINK_ENG_SCRIPT = os.path.join(SCRIPT_DIR, "link_eng_to_fr.py")
CREATE_FR_SCRIPT = os.path.join(SCRIPT_DIR, "create_fr_from_uat.py")
UPDATE_UAT_SCRIPT = os.path.join(SCRIPT_DIR, "update_uat_state.py")
UPDATE_CRM_STATUS_SCRIPT = os.path.join(SCRIPT_DIR, "update_crm_status.py")

_lock = threading.Lock()
_running = False
_job_result = None  # stores {"ok":..., "output":..., "error":...} when done
_process = None  # track running subprocess for stop functionality
_progress_file = os.path.join(SCRIPT_DIR, ".refresh_progress.json")

# ADO token cache — avoid slow az CLI calls on every request
_ado_token = None
_ado_token_expires = 0
_ado_token_lock = threading.Lock()
AZURE_TENANT_ID = os.getenv("AZURE_TENANT_ID", "").strip()

def get_ado_token():
    """Get cached ADO token, refreshing if expired or about to expire."""
    global _ado_token, _ado_token_expires
    import time
    with _ado_token_lock:
        now = time.time()
        if _ado_token and now < _ado_token_expires - 120:  # 2 min buffer
            return _ado_token
    # Fetch new token outside lock (slow operation)
    cmd = [
        "az", "account", "get-access-token",
        "--resource", "499b84ac-1321-427f-aa17-267ca6975798",
        "--query", "accessToken", "-o", "tsv"
    ]
    if AZURE_TENANT_ID:
        cmd.extend(["--tenant", AZURE_TENANT_ID])
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"az token failed: {result.stderr.strip()}")
    token = result.stdout.strip()
    with _ado_token_lock:
        _ado_token = token
        _ado_token_expires = time.time() + 3000  # ~50 min (tokens last ~1hr)
    return token

# CRM token cache
_crm_token = None
_crm_token_expires = 0
_crm_token_lock = threading.Lock()

def get_crm_token():
    """Get cached CRM token, refreshing if expired or about to expire."""
    global _crm_token, _crm_token_expires
    import time
    with _crm_token_lock:
        now = time.time()
        if _crm_token and now < _crm_token_expires - 120:
            return _crm_token
    cmd = [
        "az", "account", "get-access-token",
        "--resource", "https://m365crm.crm.dynamics.com/",
        "--query", "accessToken", "-o", "tsv"
    ]
    if AZURE_TENANT_ID:
        cmd.extend(["--tenant", AZURE_TENANT_ID])
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"CRM token failed: {result.stderr.strip()}")
    token = result.stdout.strip()
    with _crm_token_lock:
        _crm_token = token
        _crm_token_expires = time.time() + 3000
    return token


class DashboardHandler(http.server.BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")

    def _json_response(self, code, data):
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        global _running, _job_result, _process
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path == "/status":
            self._json_response(200, {"running": _running, "ok": True})
            return

        if path == "/admin_config":
            config_path = os.path.join(SCRIPT_DIR, "purview-admin-config.json")
            try:
                with open(config_path) as f:
                    cfg = json.load(f)
                self._json_response(200, cfg)
            except Exception:
                self._json_response(200, {"productId": "", "productName": ""})
            return

        if path == "/result":
            if _running:
                progress = {}
                try:
                    if os.path.exists(_progress_file):
                        with open(_progress_file, 'r') as f:
                            progress = json.load(f)
                except Exception:
                    pass
                self._json_response(202, {"running": True, "ok": True, "progress": progress})
            elif _job_result is not None:
                res = _job_result
                _job_result = None
                self._json_response(200, res)
            else:
                self._json_response(200, {"running": False, "ok": True, "output": "", "error": ""})
            return

        if path == "/stop":
            with _lock:
                if not _running:
                    self._json_response(200, {"ok": True, "message": "No refresh running"})
                    return
                if _process and _process.poll() is None:
                    try:
                        _process.terminate()
                        _process.wait(timeout=5)
                    except Exception:
                        try:
                            _process.kill()
                        except Exception:
                            pass
                _running = False
                _process = None
                _job_result = {"ok": False, "output": "", "error": "Stopped by user"}
                try:
                    os.remove(_progress_file)
                except Exception:
                    pass
            self._json_response(200, {"ok": True, "message": "Refresh stopped"})
            return

        if path == "/refresh":
            mode = params.get("mode", ["fast"])[0]
            page = params.get("page", [None])[0]
            cmd = [sys.executable, REFRESH_SCRIPT]
            if page:
                cmd += ["--page", page]
            if mode != "full":
                cmd.append("--fast")
            label = "Refresh (full AI scoring)" if mode == "full" else "Refresh (quick)"
            if page:
                label += f" [{page}]"
            self._start_async(cmd, label)
            return

        if path == "/sync":
            feature = params.get("feature", [None])[0]
            dry_run = params.get("dry_run", ["false"])[0] == "true"
            if not feature:
                self._json_response(400, {"ok": False, "error": "Missing ?feature=MFR-M365-XXXXX"})
                return
            cmd = [sys.executable, SYNC_SCRIPT, "--feature", feature]
            if dry_run:
                cmd.append("--dry-run")
            self._start_async(cmd, f"Sync {feature}")
            return

        if path == "/promote":
            feature_id = params.get("id", [None])[0]
            existing = params.get("existing", [None])[0]
            if not feature_id:
                self._json_response(400, {"ok": False, "error": "Missing ?id=GUID"})
                return
            cmd = [sys.executable, PROMOTE_SCRIPT, "--feature-id", feature_id]
            if existing:
                cmd += ["--existing-feature", existing]
            self._start_async(cmd, f"Promote {feature_id[:12]}")
            return

        if path == "/link":
            ado_id = params.get("ado_id", [None])[0]
            fr_guid = params.get("fr_guid", [None])[0]
            if not ado_id or not fr_guid:
                self._json_response(400, {"ok": False, "error": "Missing ?ado_id=ID&fr_guid=GUID"})
                return
            cmd = [sys.executable, LINK_SCRIPT, "--ado-id", ado_id, "--fr-guid", fr_guid]
            self._start_async(cmd, f"Link {ado_id}→{fr_guid[:8]}")
            return

        if path == "/create_fr":
            ado_id = params.get("ado_id", [None])[0]
            if not ado_id:
                self._json_response(400, {"ok": False, "error": "Missing ?ado_id=ID"})
                return
            cmd = [sys.executable, CREATE_FR_SCRIPT, "--ado-id", ado_id]
            self._start_async(cmd, f"Create FR from {ado_id}")
            return

        if path == "/update_crm_status":
            fr_guid = params.get("fr_guid", [None])[0]
            status = params.get("status", [None])[0]
            if not fr_guid or not status:
                self._json_response(400, {"ok": False, "error": "Missing ?fr_guid=GUID&status=CODE"})
                return
            cmd = [sys.executable, UPDATE_CRM_STATUS_SCRIPT, "--fr-guid", fr_guid, "--status", status]
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                if result.returncode == 0:
                    self._json_response(200, {"ok": True, "output": result.stdout.strip()})
                else:
                    self._json_response(500, {"ok": False, "error": result.stderr.strip() or result.stdout.strip()})
            except Exception as e:
                self._json_response(500, {"ok": False, "error": str(e)})
            return

        if path == "/link_eng":
            ado_id = params.get("ado_id", [None])[0]
            fr_guid = params.get("fr_guid", [None])[0]
            if not ado_id or not fr_guid:
                self._json_response(400, {"ok": False, "error": "Missing ?ado_id=ID&fr_guid=GUID"})
                return
            cmd = [sys.executable, LINK_ENG_SCRIPT, "--ado-id", ado_id, "--fr-guid", fr_guid]
            self._start_async(cmd, f"Link Eng {ado_id}→{fr_guid[:8]}")
            return

        if path == "/update_uat_state":
            ado_id = params.get("id", [None])[0]
            state = params.get("state", [None])[0]
            substate = params.get("substate", [None])[0]
            fr_guid = params.get("fr_guid", [None])[0]
            if not ado_id:
                self._json_response(400, {"ok": False, "error": "Missing ?id=WORK_ITEM_ID"})
                return
            cmd = [sys.executable, UPDATE_UAT_SCRIPT, "--id", ado_id]
            if state:
                cmd += ["--state", state]
            if substate:
                cmd += ["--substate", substate]
            if fr_guid:
                cmd += ["--fr-guid", fr_guid]
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                if result.returncode == 0:
                    self._json_response(200, {"ok": True, "output": result.stdout.strip()})
                else:
                    self._json_response(500, {"ok": False, "error": result.stderr.strip() or result.stdout.strip()})
            except Exception as e:
                self._json_response(500, {"ok": False, "error": str(e)})
            return

        if path == "/ado_comments":
            ado_id = params.get("id", [None])[0]
            if not ado_id:
                self._json_response(400, {"ok": False, "error": "Missing id parameter"})
                return
            try:
                token = get_ado_token()
                url = f"https://dev.azure.com/unifiedactiontracker/Technical%20Feedback/_apis/wit/workItems/{ado_id}/comments?api-version=7.0-preview.3"
                req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read())
                self._json_response(200, {"ok": True, "comments": data.get("comments", [])})
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    self._json_response(404, {"ok": False, "error": f"Work item {ado_id} not found in ADO"})
                else:
                    self._json_response(500, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json_response(500, {"ok": False, "error": str(e)})
            return

        if path == "/ado_users":
            query = params.get("q", [None])[0]
            if not query or len(query) < 2:
                self._json_response(200, {"ok": True, "users": []})
                return
            try:
                token = get_ado_token()
                url = "https://dev.azure.com/unifiedactiontracker/_apis/IdentityPicker/Identities?api-version=7.0-preview.1"
                payload = json.dumps(
                    {
                        "query": query,
                        "identityTypes": ["user"],
                        "operationScopes": ["ims", "source"],
                        "properties": ["DisplayName", "Mail", "SignInAddress"],
                    }
                ).encode()
                req = urllib.request.Request(
                    url,
                    data=payload,
                    method="POST",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                )
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read())
                users = []
                seen = set()
                for result in data.get("results", []):
                    for identity in result.get("identities", []):
                        if identity.get("entityType") != "User":
                            continue
                        display = identity.get("displayName", "")
                        if not display:
                            continue
                        vsid = identity.get("localId", "") or ""
                        origin = identity.get("originId", "")
                        key = vsid or origin
                        if key in seen:
                            continue
                        seen.add(key)
                        users.append(
                            {
                                "displayName": display,
                                "mail": identity.get("mail", "") or identity.get("signInAddress", ""),
                                "vsid": vsid,
                                "originId": origin,
                            }
                        )
                self._json_response(200, {"ok": True, "users": users[:15]})
            except Exception as e:
                self._json_response(500, {"ok": False, "error": str(e)})
            return

        if path == "/crm_notes":
            entity_id = params.get("id", [None])[0]
            if not entity_id:
                self._json_response(400, {"ok": False, "error": "Missing id parameter"})
                return
            try:
                token = get_crm_token()
                from urllib.parse import quote
                filter_param = f"_objectid_value eq '{entity_id}'"
                url = (
                    f"https://m365crm.crm.dynamics.com/api/data/v9.2/annotations"
                    f"?$filter={quote(filter_param)}"
                    f"&$orderby={quote('createdon desc')}"
                    f"&$select=subject,notetext,createdon,modifiedon,_createdby_value"
                    f"&$expand={quote('createdby($select=fullname)')}"
                )
                req = urllib.request.Request(url, headers={
                    "Authorization": f"Bearer {token}",
                    "OData-MaxVersion": "4.0",
                    "OData-Version": "4.0",
                    "Accept": "application/json",
                })
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read())
                notes = []
                for n in data.get("value", []):
                    notes.append({
                        "subject": n.get("subject", ""),
                        "text": n.get("notetext", ""),
                        "createdOn": n.get("createdon", ""),
                        "createdBy": (n.get("createdby") or {}).get("fullname", "Unknown"),
                    })
                self._json_response(200, {"ok": True, "notes": notes})
            except Exception as e:
                self._json_response(500, {"ok": False, "error": str(e)})
            return

        if path == "/scan_actions":
            # Scan UAT comments for action items
            import re as _re
            import concurrent.futures

            ACTION_KEYWORDS = _re.compile(
                r'(?:follow[- ]?up|action[- ]?required|action[- ]?needed|please\s+(?:update|provide|confirm|share|send|check|review|respond|advise|clarify|let\s+us\s+know|circle\s+back)|can\s+you|could\s+you|waiting\s+(?:for|on)|need\s+(?:an?\s+)?(?:update|response|answer|input|feedback)|next\s+steps|@\w+|todo|to-do|urgently|asap|by\s+(?:eod|end\s+of\s+(?:day|week))|eta\s*\?|when\s+(?:will|can)|any\s+update|pending|blocker|blocked)',
                _re.IGNORECASE
            )

            CRM_STATUS_MAP = {
                500350013: "Accepted", 172430003: "Approved", 172430005: "In Sprint",
                500350008: "Committed", 500350009: "Considered", 500350012: "Duplicate",
                172430000: "Evidence Gathering", 172430008: "GA", 500350003: "In Development",
                500350015: "In Dev – Current", 500350016: "In Dev – Multi-sem",
                500350002: "Eng Backlog", 500350014: "Island Preview", 500350011: "Needs Info",
                500350005: "Hold – External", 500350006: "Hold – Internal", 500350007: "Parked",
                500350000: "Pending Triage", 172430006: "Private Preview", 172430007: "Public Preview",
                500350001: "PM Review", 172430004: "Rejected", 500350019: "Rejected – PG",
                500350004: "Not a Feature", 500350018: "Not Prioritized", 500350017: "Obsolete",
                172430010: "Workaround", 497730001: "Spec Review", 172430001: "Submitted",
            }

            # Load UAT items from technical feedback page
            tech_file = os.path.join(SCRIPT_DIR, "purview-ai-technical-feedback.html")
            fr_file = os.path.join(SCRIPT_DIR, "purview-ai-feature-requests.html")

            uat_items = []
            crm_uat_items = []

            try:
                with open(tech_file) as f:
                    content = f.read()
                m = _re.search(r'const rawData = (\[.*?\]);\s*\n', content, _re.DOTALL)
                if m:
                    uat_items = json.loads(m.group(1))
            except Exception:
                pass

            # Also get CRM items that have linked UAT IDs
            try:
                with open(fr_file) as f:
                    content = f.read()
                m = _re.search(r'FEATURE_REQUESTS_DATA = (\[.*?\]);\s*\n', content, _re.DOTALL)
                if m:
                    crm_uat_items = [item for item in json.loads(m.group(1)) if item.get("uatId")]
            except Exception:
                pass

            actions = []

            def fetch_ado_latest(item, source_page, source_label, crm_id=None):
                ado_id = item.get("id") if not crm_id else item.get("uatId")
                title = item.get("title", "") or item.get("name", "")
                raw_state = item.get("state", "") or item.get("status", "")
                state = CRM_STATUS_MAP.get(raw_state, raw_state) if isinstance(raw_state, int) else str(raw_state)
                try:
                    token = get_ado_token()
                    url = f"https://dev.azure.com/unifiedactiontracker/Technical%20Feedback/_apis/wit/workItems/{ado_id}/comments?api-version=7.0-preview.3&$top=1&$order=desc"
                    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        data = json.loads(resp.read())
                    comments = data.get("comments", [])
                    if comments:
                        c = comments[0]
                        text = c.get("text", "")
                        plain = _re.sub(r'<[^>]+>', '', text)
                        if ACTION_KEYWORDS.search(plain):
                            return {
                                "source": "UAT",
                                "itemId": str(ado_id),
                                "crmId": crm_id or "",
                                "title": title,
                                "state": state,
                                "assignedTo": item.get("assignedTo", ""),
                                "lastComment": plain[:500],
                                "commentBy": c.get("createdBy", {}).get("displayName", "Unknown"),
                                "commentDate": c.get("createdDate", ""),
                                "page": source_page,
                                "pageLabel": source_label
                            }
                except urllib.error.HTTPError as e:
                    if e.code == 404:
                        # Item doesn't exist in ADO, skip silently
                        pass
                    else:
                        # Re-raise non-404 errors for logging
                        pass
                except Exception:
                    pass
                return None

            try:
                token_ado = get_ado_token()

                with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
                    futures = []
                    # Scan direct UAT items from technical feedback
                    for item in uat_items:
                        futures.append(executor.submit(fetch_ado_latest, item, "purview-ai-technical-feedback.html", "Field Feature Requests"))
                    # Scan CRM items with linked UATs
                    for item in crm_uat_items:
                        futures.append(executor.submit(fetch_ado_latest, item, "purview-ai-feature-requests.html", "CRM Feature Requests", item.get("id")))

                    for future in concurrent.futures.as_completed(futures):
                        try:
                            result = future.result()
                            if result:
                                actions.append(result)
                        except Exception as e:
                            # Silently skip individual item fetch errors
                            pass

                # Sort by comment date descending
                actions.sort(key=lambda x: x.get("commentDate", ""), reverse=True)
                # Deduplicate by ADO item ID — keep the first (most recent comment)
                seen_ids = set()
                unique_actions = []
                for a in actions:
                    if a["itemId"] not in seen_ids:
                        seen_ids.add(a["itemId"])
                        unique_actions.append(a)
                self._json_response(200, {"ok": True, "actions": unique_actions})
            except Exception as e:
                self._json_response(500, {"ok": False, "error": str(e), "actions": actions})
            return

        if path == "/roadmap":
            try:
                url = "https://www.microsoft.com/releasecommunications/api/v1/m365"
                req = urllib.request.Request(url, headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Accept": "application/json",
                })
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read())
                # Filter to Purview items
                purview = [i for i in data if any('purview' in (t.get('tagName','') or '').lower() for t in (i.get('tagsContainer') or {}).get('products',[]))]

                # Load engineering ADO items for matching
                import re as _re
                eng_file = os.path.join(SCRIPT_DIR, "purview-ai-engineering-ado.html")
                eng_items = []
                try:
                    with open(eng_file) as f:
                        content = f.read()
                    # rawData is on a single line — find the balanced JSON array
                    idx = content.find('const rawData = [')
                    if idx >= 0:
                        start = content.index('[', idx)
                        depth = 0
                        for i in range(start, len(content)):
                            if content[i] == '[': depth += 1
                            elif content[i] == ']': depth -= 1
                            if depth == 0:
                                eng_items = json.loads(content[start:i+1])
                                break
                except Exception:
                    pass

                # Keyword matching — uses title + description with prefix-based stemming
                stop_words = {'microsoft', 'purview', 'feature', 'support', 'data', 'request',
                              'office', 'enable', 'ability', 'need', 'would', 'like', 'also',
                              'that', 'this', 'with', 'have', 'from', 'will', 'when', 'which',
                              'their', 'been', 'more', 'should', 'does', 'could', 'other',
                              'some', 'than', 'into', 'only', 'very', 'what', 'about', 'just',
                              'make', 'made', 'currently', 'available', 'users', 'user', 'work',
                              'item', 'items', 'please', 'want', 'allow', 'allows', 'using',
                              'release', 'introduces', 'within', 'based', 'such', 'each',
                              'being', 'were', 'your', 'they', 'them', 'then', 'here', 'there'}

                def stem(word):
                    """Use first 6 chars as stem for consistent matching across word forms."""
                    return word[:6] if len(word) > 6 else word

                def extract_kw(text):
                    text = _re.sub(r'<[^>]+>', '', (text or '').lower())
                    text = _re.sub(r'\[.*?\]', '', text)
                    words = set(_re.findall(r'[a-z]{4,}', text))
                    return {stem(w) for w in words if w not in stop_words}

                def extract_kw_with_originals(text):
                    """Returns (set of stems, dict of stem→original_word)"""
                    text = _re.sub(r'<[^>]+>', '', (text or '').lower())
                    text = _re.sub(r'\[.*?\]', '', text)
                    words = set(_re.findall(r'[a-z]{4,}', text))
                    stem_map = {}
                    stems = set()
                    for w in words:
                        if w not in stop_words:
                            s = stem(w)
                            stems.add(s)
                            if s not in stem_map or len(w) < len(stem_map[s]):
                                stem_map[s] = w
                    return stems, stem_map

                def get_item_keywords(item, title_key='title', desc_key='description'):
                    title_kw = extract_kw(item.get(title_key, ''))
                    desc_kw = extract_kw(item.get(desc_key, ''))
                    return title_kw | desc_kw

                def compute_match_score(kw_a, kw_b):
                    """Returns (percentage 0-100, overlapping keywords)"""
                    if not kw_a or not kw_b:
                        return 0, set()
                    overlap = kw_a & kw_b
                    # Require at least 3 shared keywords for a meaningful match
                    if len(overlap) < 3:
                        return 0, set()
                    # Score = overlap relative to the smaller set (so both items need contextual alignment)
                    min_size = min(len(kw_a), len(kw_b))
                    if min_size == 0:
                        return 0, set()
                    pct = int((len(overlap) / min_size) * 100)
                    return pct, overlap

                MATCH_THRESHOLD = 60  # Contextual match threshold (with prefix-stemming + 3kw min)

                # Pre-compute engineering keywords (title + description) with originals
                def get_kw_with_map(item, title_key='title', desc_key='description'):
                    t_stems, t_map = extract_kw_with_originals(item.get(title_key, ''))
                    d_stems, d_map = extract_kw_with_originals(item.get(desc_key, ''))
                    combined = t_stems | d_stems
                    combined_map = {**d_map, **t_map}  # title words take priority
                    return combined, combined_map

                eng_kws = [(e, *get_kw_with_map(e)) for e in eng_items]

                # Match roadmap items to engineering items
                for rm in purview:
                    rm_kw = get_item_keywords(rm)
                    best_score = 0
                    best_eng = None
                    best_overlap = set()
                    best_eng_map = {}
                    for eng, ekw, emap in eng_kws:
                        pct, overlap = compute_match_score(rm_kw, ekw)
                        if pct > best_score and pct >= MATCH_THRESHOLD:
                            best_score = pct
                            best_eng = eng
                            best_overlap = overlap
                            best_eng_map = emap
                    if best_eng:
                        rm['matchedEngId'] = best_eng.get('id')
                        rm['matchedEngTitle'] = best_eng.get('title','')
                        rm['matchedEngState'] = best_eng.get('state','')
                        rm['matchScore'] = best_score
                        readable = [best_eng_map.get(s, s) for s in sorted(best_overlap)[:10]]
                        rm['matchReason'] = f"{best_score}% match ({len(best_overlap)} shared terms): {', '.join(readable)}"

                # Match engineering items to roadmap items
                rm_kws = [(r, *get_kw_with_map(r)) for r in purview]
                for eng in eng_items:
                    ekw = get_item_keywords(eng)
                    best_score = 0
                    best_rm = None
                    best_overlap = set()
                    best_rm_map = {}
                    for rm, rkw, rmap in rm_kws:
                        pct, overlap = compute_match_score(ekw, rkw)
                        if pct > best_score and pct >= MATCH_THRESHOLD:
                            best_score = pct
                            best_rm = rm
                            best_overlap = overlap
                            best_rm_map = rmap
                    if best_rm:
                        eng['matchedRoadmapId'] = best_rm.get('id')
                        eng['matchedRoadmapTitle'] = best_rm.get('title','')
                        eng['matchedRoadmapStatus'] = best_rm.get('status','')
                        eng['matchScore'] = best_score
                        readable = [best_rm_map.get(s, s) for s in sorted(best_overlap)[:10]]
                        eng['matchReason'] = f"{best_score}% match ({len(best_overlap)} shared terms): {', '.join(readable)}"

                self._json_response(200, {"ok": True, "items": purview, "engineering": eng_items})
            except Exception as e:
                self._json_response(500, {"ok": False, "error": str(e)})
            return

        self._json_response(404, {"ok": False, "error": "Not found"})

    def _start_async(self, cmd, label):
        """Start a task in a background thread — returns 202 immediately."""
        global _running, _job_result, _process
        with _lock:
            if _running:
                self._json_response(409, {"ok": False, "error": "A task is already in progress"})
                return
            _running = True
            _job_result = None
            _process = None

        def run():
            global _running, _job_result, _process
            # Clear progress file
            try:
                with open(_progress_file, 'w') as f:
                    json.dump({"step": "Starting...", "pct": 0}, f)
            except Exception:
                pass
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                _process = proc
                stdout, stderr = proc.communicate(timeout=1800)
                ok = proc.returncode == 0
                _job_result = {
                    "ok": ok,
                    "output": stdout[-2000:] if stdout else "",
                    "error": stderr[-1000:] if not ok else "",
                }
            except subprocess.TimeoutExpired:
                if _process:
                    _process.kill()
                    _process.communicate()
                _job_result = {"ok": False, "error": f"{label} timed out (30 min)", "output": ""}
            except Exception as e:
                _job_result = {"ok": False, "error": str(e), "output": ""}
            finally:
                _running = False
                _process = None
                try:
                    os.remove(_progress_file)
                except Exception:
                    pass
            print(f"[server] {label} finished — ok={_job_result.get('ok')}")

        threading.Thread(target=run, daemon=True).start()
        self._json_response(202, {"ok": True, "started": True, "message": f"{label} started"})

    def log_message(self, fmt, *args):
        print(f"[server] {args[0]}")

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path == "/save_admin_config":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length)) if length else {}
                config_path = os.path.join(SCRIPT_DIR, "purview-admin-config.json")
                with open(config_path, "w") as f:
                    json.dump(body, f, indent=2)
                print(f"[server] Admin config saved: {body.get('productName', '?')}")
                self._json_response(200, {"ok": True})
            except Exception as e:
                self._json_response(500, {"ok": False, "error": str(e)})
            return

        if path == "/ado_comments":
            ado_id = params.get("id", [None])[0]
            if not ado_id:
                self._json_response(400, {"ok": False, "error": "Missing id parameter"})
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length)) if length else {}
                text = body.get("text", "").strip()
                if not text:
                    self._json_response(400, {"ok": False, "error": "Empty comment"})
                    return
                token = get_ado_token()
                url = f"https://dev.azure.com/unifiedactiontracker/Technical%20Feedback/_apis/wit/workItems/{ado_id}/comments?api-version=7.0-preview.3"
                payload = json.dumps({"text": text}).encode()
                req = urllib.request.Request(
                    url,
                    data=payload,
                    method="POST",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                )
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read())
                self._json_response(200, {"ok": True, "comment": data})
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    self._json_response(404, {"ok": False, "error": f"Work item {ado_id} not found in ADO"})
                else:
                    self._json_response(500, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json_response(500, {"ok": False, "error": str(e)})
            return

        if path == "/crm_notes":
            entity_id = params.get("id", [None])[0]
            if not entity_id:
                self._json_response(400, {"ok": False, "error": "Missing id parameter"})
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length)) if length else {}
                text = body.get("text", "").strip()
                subject = body.get("subject", "").strip() or "Note"
                if not text:
                    self._json_response(400, {"ok": False, "error": "Empty note"})
                    return
                token = get_crm_token()
                url = "https://m365crm.crm.dynamics.com/api/data/v9.2/annotations"
                payload = json.dumps({
                    "subject": subject,
                    "notetext": text,
                    "objectid_ems_productfeature@odata.bind": f"/ems_productfeatures({entity_id})"
                }).encode()
                req = urllib.request.Request(url, data=payload, method="POST", headers={
                    "Authorization": f"Bearer {token}",
                    "OData-MaxVersion": "4.0",
                    "OData-Version": "4.0",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                })
                with urllib.request.urlopen(req, timeout=30) as resp:
                    resp.read()
                self._json_response(200, {"ok": True})
            except Exception as e:
                self._json_response(500, {"ok": False, "error": str(e)})
            return

        self._json_response(404, {"error": "Not found"})


def main():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), DashboardHandler)
    print(f"🔄 Dashboard server listening on http://127.0.0.1:{PORT}")
    print(f"   Endpoints:")
    print(f"     GET /refresh                          — refresh all pages")
    print(f"     GET /sync?feature=MFR-M365-XXXXX      — sync ADO→CRM")
    print(f"     GET /sync?feature=...&dry_run=true     — preview sync")
    print(f"     GET /promote?id=GUID                  — promote field feature to FR")
    print(f"     GET /link?ado_id=ID&fr_guid=GUID      — link UAT to Feature Request")
    print(f"     GET /create_fr?ado_id=ID              — create new FR from UAT")
    print(f"     GET /status                           — check if busy")
    # Pre-warm ADO token cache
    try:
        get_ado_token()
        print(f"   ✅ ADO token cached")
    except Exception as e:
        print(f"   ⚠️  ADO token not available: {e}")
    print(f"   Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n🛑 Server stopped.")
        server.server_close()


if __name__ == "__main__":
    # Daemonize: redirect stdin/stdout/stderr to /dev/null and log file
    # so the server survives after parent shell exits
    if os.environ.get("_REFRESH_SERVER_DAEMONIZED") != "1":
        # Re-launch self with proper file descriptors
        log_path = os.path.join(SCRIPT_DIR, ".refresh_server.log")
        env = os.environ.copy()
        env["_REFRESH_SERVER_DAEMONIZED"] = "1"
        with open(log_path, "a") as log, open(os.devnull, "r") as devnull:
            proc = subprocess.Popen(
                [sys.executable, __file__],
                stdin=devnull, stdout=log, stderr=log,
                env=env, start_new_session=True
            )
        print(f"🔄 Dashboard server started (PID {proc.pid}), log: {log_path}")
    else:
        main()

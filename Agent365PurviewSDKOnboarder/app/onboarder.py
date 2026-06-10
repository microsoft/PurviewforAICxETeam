"""Agent SDK Onboarder — Flask entrypoint.

Routes:
    GET  /                              -> the multi-step form
    GET  /history                       -> previously onboarded agents
    POST /submit                        -> kick off the onboarding workflow, return run_id
    GET  /progress/<id>                 -> SSE stream of progress log lines
    GET  /result/<id>                   -> success/failure page once the run is done
    GET  /diagnostics                   -> standalone diagnostics page
    POST /api/diagnostics               -> JSON diagnostics report
    GET  /api/accounts/list             -> current az accounts (picker source)
    POST /api/accounts/select           -> {subscription_id} -> switch global az context
    POST /api/accounts/login            -> {tenant_id?} -> start az login, returns {login_id}
    GET  /api/accounts/login/<id>       -> poll an in-flight login
    POST /api/accounts/login/<id>/cancel -> kill the in-flight login
    GET  /api/check-slug                -> {slug} -> {exists, report?}
    GET  /api/port-check                -> {port, host?} -> {available, suggested?}
    GET  /api/azure/whoami              -> active az identity (UPN, tenant, oid)
    GET  /api/azure/subscriptions       -> subscriptions visible to the user
    GET  /api/azure/vaults              -> ?subscription_id= -> Key Vaults visible
    GET  /api/azure/resource-groups     -> ?subscription_id= -> RGs for the create form
    GET  /api/azure/locations           -> ?subscription_id= -> Azure regions
    POST /api/azure/vaults/probe        -> {name} -> {can_write, can_read, ...}
    POST /api/azure/vaults              -> {name, subscription_id, resource_group, location} -> provisions vault + role assignment
    GET  /static/*                      -> CSS/JS

Security:
    Localhost-only Flask app. Defends against drive-by browser attacks
    (DNS rebinding / cross-origin form POSTs) via:
      * Host-header allowlist (127.0.0.1 / localhost on the bind port)
      * Origin/Referer check on state-mutating POSTs
      * Per-launch CSRF token required on /submit and /api/* mutations
"""
from __future__ import annotations

import json
import logging
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from flask import Flask, Response, abort, jsonify, redirect, render_template, request, url_for

import diagnostics
import identity
import azure_vaults as av
import keyvault as kv
import workflow

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("onboarder")

ROOT = Path(os.environ.get("ONBOARDER_ROOT", Path(__file__).resolve().parent.parent))
GENERATED = ROOT / "generated"
GENERATED.mkdir(parents=True, exist_ok=True)

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024  # 1 MB form posts is plenty

# In-memory registry of in-flight runs. Each run has its own log queue.
RUNS: dict[str, dict[str, Any]] = {}
RUNS_LOCK = threading.Lock()

# In-memory registry of agent launches (v1.4 one-click launcher). The launched
# uvicorn process is detached so it survives onboarder restarts; .launch-state.json
# in each agent's folder is the durable PID/port record.
LAUNCHED: dict[str, dict[str, Any]] = {}
LAUNCHED_LOCK = threading.Lock()

# Per-launch CSRF token, re-minted every time the process starts.
CSRF_TOKEN = identity.csrf_token()
CSRF_HEADER = "X-Onboarder-CSRF"

# Serialize onboarding runs (per rubber-duck): one az-mutating workflow at a
# time. Concurrent destructive runs against shared az CLI state are dangerous.
ONBOARDING_LOCK = threading.Lock()
ONBOARDING_IN_FLIGHT: dict[str, str | None] = {"run_id": None}

# Allowlist for the Host header — populated from the actual bind address on
# launch via `set_bind_address()`. We accept 127.0.0.1 and localhost on the
# bound port (with or without an explicit port for the default :80/:443 cases,
# which don't apply here but are cheap to handle).
_ALLOWED_HOSTS: set[str] = set()


def set_bind_address(host: str, port: int) -> None:
    _ALLOWED_HOSTS.clear()
    for h in {host, "127.0.0.1", "localhost"}:
        _ALLOWED_HOSTS.add(f"{h}:{port}")


# Default allow-list pre-fills assuming the launcher will call set_bind_address
# before serving requests; the dev server (`flask run`) falls back to whatever
# the launcher sets in main().


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9-]+", "-", name.strip()).strip("-").lower()
    return s or f"agent-{uuid.uuid4().hex[:6]}"


# Slugs are constrained to this charset to match _slugify output. Used by
# /api/check-slug and the overwrite/archive path to defeat traversal attempts.
_SLUG_RE = re.compile(r"^[a-z0-9-]+$")


def _safe_slug_dir(slug: str) -> Path | None:
    """Return the path GENERATED/<slug> only if slug is well-formed and stays
    inside GENERATED. Returns None on any traversal attempt or invalid slug."""
    if not slug or not _SLUG_RE.match(slug):
        return None
    candidate = (GENERATED / slug).resolve()
    try:
        candidate.relative_to(GENERATED.resolve())
    except ValueError:
        return None
    return candidate


def _port_is_free(host: str, port: int) -> bool:
    """Best-effort check: try to bind a TCP socket on host:port. Free on success.

    Note: this is racy — a process could grab the port between this check and
    actual server start. Caller treats the result as advisory."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
            s.bind((host, port))
        return True
    except OSError:
        return False


def _next_free_port(host: str, start: int, span: int = 20) -> int | None:
    """Scan [start+1 .. start+span] for the first free port on host. None if none free."""
    for p in range(start + 1, min(start + span + 1, 65536)):
        if _port_is_free(host, p):
            return p
    return None


# ----- v1.4 agent launcher helpers -----------------------------------------
# Process-detachment flags for Windows subprocess.Popen.
# DETACHED_PROCESS: no console window on Windows.
# CREATE_NEW_PROCESS_GROUP: child gets its own process group (so a Ctrl+C
#   delivered to the onboarder doesn't propagate).
# CREATE_BREAKAWAY_FROM_JOB: escape any Job Object the onboarder is in. When
#   the onboarder is launched from PowerShell/cmd, the shell wraps it in a
#   Job; without breakaway, the spawned uvicorn dies the moment that shell
#   exits — defeating the whole point of "detached".
_WIN_DETACHED_PROCESS = 0x00000008
_WIN_CREATE_NEW_PROCESS_GROUP = 0x00000200
_WIN_CREATE_BREAKAWAY_FROM_JOB = 0x01000000

def _agent_state_path(slug: str) -> Path | None:
    folder = _safe_slug_dir(slug)
    return (folder / ".launch-state.json") if folder else None


def _agent_log_path(slug: str) -> Path | None:
    folder = _safe_slug_dir(slug)
    return (folder / ".launch.log") if folder else None


def _read_launch_state(slug: str) -> dict | None:
    p = _agent_state_path(slug)
    if not p or not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _write_launch_state(slug: str, state: dict | None) -> None:
    p = _agent_state_path(slug)
    if p is None:
        return
    if state is None:
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        return
    p.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _pid_alive(pid: int | None) -> bool:
    """Return True if a process with this PID is currently alive."""
    if not pid or pid < 1:
        return False
    if os.name == "nt":
        # tasklist exits 0 and prints the PID if alive; otherwise prints "INFO: No tasks..."
        try:
            r = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                capture_output=True, text=True, timeout=5,
            )
            return str(pid) in (r.stdout or "")
        except Exception:  # noqa: BLE001
            return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False
    except PermissionError:
        return True


def _venv_python(folder: Path) -> Path:
    """Path to .venv's python, regardless of platform."""
    if os.name == "nt":
        return folder / ".venv" / "Scripts" / "python.exe"
    return folder / ".venv" / "bin" / "python"


def _agent_is_running(slug: str) -> dict | None:
    """Return persisted state if the agent's uvicorn process is alive.

    PID is the authoritative signal of liveness; cleans up state ONLY when
    the PID is dead. The port-bound bit is reported as state['port_bound']
    so callers can distinguish "starting up" from "fully ready" without
    racing the launcher worker (which writes state BEFORE uvicorn binds)."""
    state = _read_launch_state(slug)
    if not state:
        return None
    pid = state.get("pid")
    if not pid or not _pid_alive(pid):
        _write_launch_state(slug, None)
        return None
    port = state.get("port")
    host = state.get("host") or "127.0.0.1"
    state["port_bound"] = bool(port) and not _port_is_free(host, port)
    return state


def _ensure_redirect_uri_for_slug(folder: Path, host: str, port: int,
                                 on_log) -> None:
    """Idempotently register the wrapper's OAuth /auth/callback URI on the
    onboarded Entra app, reading the app_id from the agent's report.

    Why launch-time: the port can be different at launch than at onboarding
    (user passes --port; port-collision logic may bump it). Running this every
    launch makes the URI registration self-heal regardless. Pre-v1.7.1 agents
    had no URI registered at all (first sign-in always failed); this single
    helper retrofits them without any re-onboarding.
    """
    report_path = folder / "_onboarding-report.json"
    if not report_path.exists():
        on_log("[INFO] no _onboarding-report.json found; skipping OAuth URI auto-registration")
        return
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        on_log(f"[WARN] could not read onboarding report ({exc!s}); "
               f"skipping OAuth URI auto-registration")
        return
    app_id = ""
    for st in report.get("steps") or []:
        if st.get("name") == "create_entra_app":
            app_id = (st.get("detail") or {}).get("app_id", "")
            if app_id:
                break
    if not app_id:
        on_log("[INFO] no app_id in onboarding report; skipping OAuth URI auto-registration")
        return

    want = [f"http://{host}:{port}/auth/callback"]
    if host in ("127.0.0.1", "::1"):
        want.append(f"http://localhost:{port}/auth/callback")
    elif host == "localhost":
        want.append(f"http://127.0.0.1:{port}/auth/callback")

    az = shutil.which("az") or "az"
    try:
        show = subprocess.run(
            [az, "ad", "app", "show", "--id", app_id,
             "--query", "web.redirectUris", "-o", "json"],
            capture_output=True, text=True, timeout=30,
        )
    except Exception as exc:  # noqa: BLE001
        on_log(f"[WARN] could not query Entra app for redirect URIs ({exc!s}); "
               f"will continue. Sign-in will fail with AADSTS500113 if the "
               f"URI {want[0]} is not already registered.")
        return
    if show.returncode != 0:
        on_log(f"[WARN] az ad app show failed (rc={show.returncode}); "
               f"redirect URI registration skipped. If sign-in fails with "
               f"AADSTS500113, register manually:")
        on_log(f"         az ad app update --id {app_id} --web-redirect-uris {' '.join(want)}")
        return
    try:
        current = json.loads((show.stdout or "[]").strip()) or []
        if not isinstance(current, list):
            current = []
    except json.JSONDecodeError:
        current = []
    missing = [u for u in want if u not in current]
    if not missing:
        on_log(f"[OK] OAuth redirect URI already registered for app {app_id}")
        return

    merged = list(current) + missing
    on_log(f"[INFO] registering OAuth redirect URIs on Entra app {app_id}: {', '.join(missing)}")
    upd = subprocess.run(
        [az, "ad", "app", "update", "--id", app_id,
         "--web-redirect-uris", *merged],
        capture_output=True, text=True, timeout=30,
    )
    if upd.returncode == 0:
        on_log(f"[OK] redirect URIs now: {', '.join(merged)}")
    else:
        err = (upd.stderr or upd.stdout or "(no output)").strip().splitlines()
        on_log(f"[WARN] az ad app update failed (rc={upd.returncode}). "
               f"Sign-in will fail with AADSTS500113. Manual fix:")
        on_log(f"         az ad app update --id {app_id} --web-redirect-uris {' '.join(merged)}")
        for line in err[:3]:
            on_log("         " + line)


def _launcher_worker(slug: str, folder: Path, host: str, port: int) -> None:
    """Background worker for /api/agent/launch: venv -> pip install -> uvicorn detached.

    Streams progress into LAUNCHED[slug]["queue"] for SSE consumers."""
    rec = LAUNCHED[slug]
    q: queue.Queue = rec["queue"]

    def on_log(line: str) -> None:
        with LAUNCHED_LOCK:
            rec["log_lines"].append(line)
        q.put(line)

    try:
        # --- Phase 0: idempotently ensure OAuth redirect URI is registered.
        # Self-healing for older agents (or those launched on a different port
        # than was registered at onboarding time). Failures here are non-fatal:
        # the user will see AADSTS500113 at sign-in with a clear remedy.
        _ensure_redirect_uri_for_slug(folder, host, port, on_log)

        # --- Phase A: ensure .venv exists ----------------------------------
        venv_py = _venv_python(folder)
        if not venv_py.exists():
            on_log("[INFO] Creating virtual environment at .venv (this is a one-time step) ...")
            r = subprocess.run(
                [sys.executable, "-m", "venv", ".venv"],
                cwd=str(folder), capture_output=True, text=True,
            )
            if r.stdout:
                for ln in r.stdout.splitlines(): on_log("    " + ln)
            if r.returncode != 0:
                on_log("[FATAL] venv creation failed: " + (r.stderr or "(no stderr)"))
                with LAUNCHED_LOCK: rec["status"] = "failed"
                q.put(None); return
            on_log("[OK] .venv created")
        else:
            on_log("[INFO] .venv already exists, skipping creation")

        # --- Phase B: pip install (idempotent; fast if already installed) --
        on_log("[INFO] Installing dependencies from requirements.txt ...")
        proc = subprocess.Popen(
            [str(venv_py), "-m", "pip", "install", "-r", "requirements.txt",
             "--disable-pip-version-check"],
            cwd=str(folder), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        )
        assert proc.stdout is not None
        for line in iter(proc.stdout.readline, ""):
            on_log("    " + line.rstrip())
        proc.wait()
        if proc.returncode != 0:
            on_log(f"[FATAL] pip install failed with exit code {proc.returncode}")
            with LAUNCHED_LOCK: rec["status"] = "failed"
            q.put(None); return
        on_log("[OK] dependencies installed")

        # --- Phase C: spawn uvicorn detached -------------------------------
        log_path = folder / ".launch.log"
        log_fh = open(log_path, "w", encoding="utf-8", buffering=1)
        if os.name == "nt":
            base_flags = _WIN_DETACHED_PROCESS | _WIN_CREATE_NEW_PROCESS_GROUP | _WIN_CREATE_BREAKAWAY_FROM_JOB
        else:
            base_flags = 0
        on_log(f"[INFO] Starting uvicorn on http://{host}:{port}/ (detached; PID will survive onboarder restart) ...")
        try:
            uvicorn_proc = subprocess.Popen(
                [str(venv_py), "-m", "uvicorn", "server:app", "--host", host, "--port", str(port)],
                cwd=str(folder),
                stdout=log_fh, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                creationflags=base_flags,
                close_fds=True,
            )
        except OSError as exc:
            # If the parent job rejects breakaway, retry without that flag.
            if os.name == "nt" and base_flags & _WIN_CREATE_BREAKAWAY_FROM_JOB:
                on_log(f"[WARN] Job breakaway not allowed ({exc!s}); retrying without it")
                uvicorn_proc = subprocess.Popen(
                    [str(venv_py), "-m", "uvicorn", "server:app", "--host", host, "--port", str(port)],
                    cwd=str(folder),
                    stdout=log_fh, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    creationflags=_WIN_DETACHED_PROCESS | _WIN_CREATE_NEW_PROCESS_GROUP,
                    close_fds=True,
                )
            else:
                raise
        on_log(f"[INFO] uvicorn spawned, pid={uvicorn_proc.pid}")
        with LAUNCHED_LOCK:
            rec["pid"] = uvicorn_proc.pid
        _write_launch_state(slug, {
            "pid": uvicorn_proc.pid,
            "port": port,
            "host": host,
            "started_at": rec["started_at"],
            "log_path": str(log_path),
        })

        # --- Phase D: wait for the port to bind ----------------------------
        deadline = time.time() + 25
        while time.time() < deadline:
            # Process died early?
            if uvicorn_proc.poll() is not None:
                on_log(f"[FATAL] uvicorn exited early with code {uvicorn_proc.returncode}")
                try:
                    tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-20:]
                    on_log("--- last 20 lines of .launch.log ---")
                    for ln in tail: on_log("    " + ln)
                except Exception: pass  # noqa: BLE001
                _write_launch_state(slug, None)
                with LAUNCHED_LOCK: rec["status"] = "failed"
                q.put(None); return
            if not _port_is_free(host, port):
                on_log(f"[OK] uvicorn is listening on http://{host}:{port}/")
                # Echo the last few uvicorn startup lines into our log for context
                try:
                    tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-8:]
                    for ln in tail: on_log("    " + ln.rstrip())
                except Exception: pass  # noqa: BLE001
                with LAUNCHED_LOCK: rec["status"] = "running"
                q.put(None); return
            time.sleep(0.3)
        on_log("[WARN] uvicorn did not bind to the port within 25s — check .launch.log")
        with LAUNCHED_LOCK: rec["status"] = "timeout"
        q.put(None)
    except Exception as exc:  # noqa: BLE001
        log.exception("launcher crashed")
        on_log(f"[FATAL] {exc!r}")
        with LAUNCHED_LOCK: rec["status"] = "failed"
        q.put(None)


def _kill_pid_tree(pid: int) -> tuple[bool, str]:
    """Kill a PID and its child processes. Returns (ok, message)."""
    if not pid or not _pid_alive(pid):
        return True, "process not alive"
    try:
        if os.name == "nt":
            r = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True, text=True, timeout=10,
            )
            return r.returncode == 0, (r.stdout + r.stderr).strip() or f"exit {r.returncode}"
        import signal
        os.kill(pid, signal.SIGTERM)
        return True, "SIGTERM sent"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def _last_provider_defaults(
    provider: str,
    env_to_field: dict[str, str],
) -> dict[str, str]:
    """Find the most recent successfully-onboarded agent of `provider` and
    return its non-secret config as form-field defaults.

    `env_to_field` maps env-file key (e.g. "GCP_PROJECT") to form field
    name (e.g. "vertex_project"). Only keys in this whitelist are read;
    secrets such as API keys MUST NOT be included.

    Result keys are the form field names, plus `<provider>_source_slug`
    and `<provider>_source_name` so the UI can show provenance.
    """
    out: dict[str, str] = {}
    if not GENERATED.exists():
        return out
    candidates: list[tuple[float, Path]] = []
    for folder in GENERATED.iterdir():
        if not folder.is_dir():
            continue
        report_path = folder / "_onboarding-report.json"
        env_path = folder / ".env"
        if not (report_path.exists() and env_path.exists()):
            continue
        try:
            rep = json.loads(report_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        rep_provider = ((rep.get("agent") or {}).get("provider") or "").lower()
        if rep_provider != provider.lower():
            continue
        try:
            mtime = env_path.stat().st_mtime
        except OSError:
            continue
        candidates.append((mtime, env_path))
    if not candidates:
        return out
    candidates.sort(reverse=True)
    # Common placeholders we should never propagate as a "real" default.
    placeholders = {
        "", "my-gcp-project", "your-gcp-project", "your-project-id",
        "project-id", "example-project", "todo", "tbd",
        "your-api-key", "sk-…", "sk-...", "...",
    }
    for _, env_path in candidates:
        try:
            text = env_path.read_text(encoding="utf-8")
        except OSError:
            continue
        found: dict[str, str] = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k in env_to_field and v and v.lower() not in placeholders:
                found[env_to_field[k]] = v
        if found:
            out.update(found)
            slug = env_path.parent.name
            out[f"{provider}_source_slug"] = slug
            rep_path = env_path.parent / "_onboarding-report.json"
            try:
                rep = json.loads(rep_path.read_text(encoding="utf-8"))
                out[f"{provider}_source_name"] = (
                    (rep.get("agent") or {}).get("display_name") or slug
                )
            except Exception:  # noqa: BLE001
                out[f"{provider}_source_name"] = slug
            break
    return out


# Whitelisted (non-secret) env keys to pull from prior agents per provider.
# IMPORTANT: never include secret-bearing keys here -- they're sourced from
# the user form on each onboarding, not recovered from disk.
_PROVIDER_PREFILL_KEYS: dict[str, dict[str, str]] = {
    # Intentionally empty: pre-fill was disabled in v1.7.1 because
    # silently inheriting a previous agent's identifiers (e.g. Vertex
    # `AGENT_RESOURCE_NAME`, Azure OpenAI endpoint/deployment, Custom HTTP
    # URL) caused new agents to bind to the *previous* agent's deployment.
    # The form's static placeholders + per-field defaults are sufficient.
    # If you want to re-enable pre-fill for a *truly environment-wide*
    # field (e.g. AWS_REGION, GCP_LOCATION) add it back here -- but never
    # add anything that identifies a specific model deployment / engine /
    # endpoint.
}


def _all_provider_defaults() -> dict[str, str]:
    """Merge pre-fill defaults across every supported provider so the
    form's value="..." bindings can reference them without per-provider
    plumbing."""
    out: dict[str, str] = {}
    for provider, keys in _PROVIDER_PREFILL_KEYS.items():
        out.update(_last_provider_defaults(provider, keys))
    return out


def _last_vertex_defaults() -> dict[str, str]:
    """Backwards-compatible alias preserved for any external callers.
    Pre-fill is disabled in v1.7.1, so this always returns an empty dict
    (the previous behaviour silently injected the prior agent's IDs)."""
    return {}


def _load_history() -> list[dict]:
    """Scan generated/*/_onboarding-report.json and return parsed rows, newest first.

    Each row has display_name, slug, provider, acting_as_upn, tenant_id,
    tenant_domain, subscription_id, app_id, sp_object_id, generated_at, ok,
    folder, error (if any). Corrupt reports become rows flagged with error=<msg>
    rather than crashing the page.
    """
    rows: list[dict] = []
    if not GENERATED.exists():
        return rows
    for folder in sorted(GENERATED.iterdir()):
        if not folder.is_dir():
            continue
        slug = folder.name
        report_path = folder / "_onboarding-report.json"
        if not report_path.exists():
            rows.append({
                "slug": slug,
                "display_name": slug,
                "folder": str(folder),
                "error": "no _onboarding-report.json (manual or pre-v1.3 folder)",
                "generated_at": "",
            })
            continue
        try:
            data = json.loads(report_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            rows.append({
                "slug": slug,
                "display_name": slug,
                "folder": str(folder),
                "error": f"could not parse report: {exc!r}",
                "generated_at": "",
            })
            continue
        agent = data.get("agent") or {}
        acting = data.get("acting_as") or {}
        # app_id/sp_object_id live inside steps[].detail for the create_entra_app step,
        # not at the report root.
        app_id = ""
        sp_object_id = ""
        for st in data.get("steps") or []:
            if st.get("name") == "create_entra_app":
                detail = st.get("detail") or {}
                app_id = detail.get("app_id", "") or app_id
                sp_object_id = detail.get("sp_object_id", "") or sp_object_id
        # Overall ok = no step failed
        ok = bool(data.get("steps")) and all(s.get("ok") for s in data.get("steps") or [])
        rows.append({
            "slug": agent.get("slug") or slug,
            "display_name": agent.get("display_name") or slug,
            "provider": agent.get("provider") or "",
            "tenant_id": data.get("tenant_id", ""),
            "tenant_domain": acting.get("tenant_default_domain") or "",
            "subscription_id": data.get("subscription_id", ""),
            "acting_as_upn": acting.get("user_name", ""),
            "app_id": app_id,
            "sp_object_id": sp_object_id,
            "generated_at": data.get("generated_at", ""),
            "ok": ok,
            "folder": str(folder),
            "error": "",
        })
    # Sort: newest generated_at first; rows missing generated_at sort last.
    rows.sort(key=lambda r: r.get("generated_at") or "", reverse=True)
    return rows


# -----------------------------------------------------------------------------
# Security middleware
# -----------------------------------------------------------------------------
_STATIC_ROUTES = {"/healthz", "/static"}
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _host_is_allowed(host_header: str | None) -> bool:
    if not host_header:
        return False
    # Strip any port if not set on the allowed list (allow exact-match).
    return host_header.lower() in {h.lower() for h in _ALLOWED_HOSTS}


def _origin_is_allowed(origin_or_referer: str | None) -> bool:
    if not origin_or_referer:
        return False
    try:
        u = urlparse(origin_or_referer)
    except Exception:
        return False
    if u.scheme not in ("http", "https"):
        return False
    netloc = u.netloc.lower()
    return netloc in {h.lower() for h in _ALLOWED_HOSTS}


@app.before_request
def _security_guard():
    # 1. Host header allowlist defends against DNS rebinding.
    if _ALLOWED_HOSTS and not _host_is_allowed(request.host):
        log.warning("rejected Host header: %r (allowed=%s)", request.host, _ALLOWED_HOSTS)
        return jsonify({"error": "host header not allowed"}), 421

    # 2. Mutating requests require CSRF token + same-origin Origin/Referer.
    if request.method in _SAFE_METHODS:
        return None
    if request.path == "/healthz":
        return None  # no-op safe endpoint (POST shouldn't reach it anyway)

    # Origin / Referer must match a known bind host. Some browsers omit Origin
    # on same-origin form POSTs; in that case we fall back to Referer.
    origin = request.headers.get("Origin") or request.headers.get("Referer")
    if not _origin_is_allowed(origin):
        log.warning("rejected POST: Origin/Referer %r not allowed", origin)
        return jsonify({"error": "origin not allowed"}), 403

    # CSRF: accept the token via header (preferred for AJAX) or hidden form
    # field (for the multipart Submit form).
    submitted = (
        request.headers.get(CSRF_HEADER)
        or request.form.get("csrf_token")
        or (request.get_json(silent=True) or {}).get("csrf_token")
    )
    if not submitted or submitted != CSRF_TOKEN:
        log.warning("rejected POST: bad CSRF on %s", request.path)
        return jsonify({"error": "missing or invalid csrf token"}), 403
    return None


@app.get("/")
def index():
    """Render the onboarding form.

    NOTE: Page load deliberately does NOT call `az account list`. The user
    types Tenant ID / UPN / Subscription manually; the auto-detected list is
    fetched lazily only when the user clicks "Load accounts" in the form.
    The server still re-derives identity from live `az account show` at
    /submit and rejects any mismatch.
    """
    return render_template(
        "form.html",
        csrf_token=CSRF_TOKEN,
        csrf_header=CSRF_HEADER,
        kv_configured=kv.is_kv_configured(),
        kv_vault_name=kv.vault_name() or "",
        # The picker renders without making any ARM call on page load - it
        # lazy-fetches subscriptions/vaults only when the user clicks "Change
        # vault". This keeps the form snappy for the 95% case where the
        # launcher's default vault is correct.
        kv_default_vault=kv.vault_name() or "",
        defaults={
            "host": os.environ.get("ONBOARDER_HOST", "127.0.0.1"),
            "port": "8080",
            "force_audit": True,
            "purview_dlp": True,
            "purview_audit": True,
            "a365_blueprint": False,
            "a365_observability": False,
            # Pre-fill every provider's non-secret config from the most
            # recent successful onboarding of that provider, so the user
            # rarely has to retype anything. Secrets are never pre-filled.
            **_all_provider_defaults(),
        },
    )


@app.post("/api/open-folder")
def open_folder_api():
    """Open a generated-agent folder in the OS file browser.

    Body: {"folder": "<absolute path>"} OR {"slug": "<agent-slug>"}.

    Hard constraint: the resolved path MUST live inside GENERATED/ — any
    traversal attempt or path outside the onboarder's generated tree is a
    400. This is a CSRF-gated POST (handled in before_request), and the
    spawned process is fully detached (no stdio inheritance).

    Returns {ok: bool, opened: str, error?: str}.
    """
    data = request.get_json(silent=True) or request.form.to_dict(flat=True)
    slug = (data.get("slug") or "").strip()
    folder = (data.get("folder") or "").strip()

    target: Path | None = None
    if slug:
        target = _safe_slug_dir(slug)
        if target is None:
            return jsonify({"ok": False, "error": f"invalid slug: {slug!r}"}), 400
    elif folder:
        try:
            candidate = Path(folder).resolve()
        except (OSError, ValueError) as exc:
            return jsonify({"ok": False, "error": f"bad path: {exc}"}), 400
        try:
            candidate.relative_to(GENERATED.resolve())
        except ValueError:
            log.warning("rejected open-folder: %r is outside %r", str(candidate), str(GENERATED))
            return jsonify({"ok": False, "error": "path is outside the onboarder's generated/ tree"}), 400
        target = candidate
    else:
        return jsonify({"ok": False, "error": "either 'slug' or 'folder' is required"}), 400

    if not target.exists():
        return jsonify({"ok": False, "error": f"folder does not exist: {target}"}), 404
    if not target.is_dir():
        return jsonify({"ok": False, "error": f"not a directory: {target}"}), 400

    try:
        if sys.platform.startswith("win"):
            # os.startfile is the right call for "open in default handler"
            # on Windows. For a directory that's Explorer. Fully detached,
            # no stdin/stdout inheritance, no shell injection (path is a
            # native string, not a command).
            os.startfile(str(target))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(
                ["open", str(target)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
        else:
            subprocess.Popen(
                ["xdg-open", str(target)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
    except FileNotFoundError as exc:
        return jsonify({"ok": False, "error": f"no file-browser opener available: {exc}"}), 500
    except OSError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({"ok": True, "opened": str(target)})


@app.get("/history")
def history_page():
    """Render the list of previously onboarded agents."""
    rows = _load_history()
    # Annotate each row with current running status so the page can render
    # Launch/Stop/Open buttons correctly without a second AJAX hop.
    for r in rows:
        slug = r.get("slug") or ""
        running = _agent_is_running(slug) if slug else None
        if running:
            r["running"] = True
            r["pid"] = running.get("pid")
            r["url"] = f"http://{running.get('host','127.0.0.1')}:{running['port']}/"
        else:
            r["running"] = False
    return render_template(
        "history.html",
        rows=rows,
        generated_root=str(GENERATED),
        csrf_token=CSRF_TOKEN,
        csrf_header=CSRF_HEADER,
    )


# -----------------------------------------------------------------------------
# Account picker / login API
# -----------------------------------------------------------------------------
@app.get("/api/accounts/list")
def accounts_list():
    return jsonify(identity.list_az_accounts().to_dict())


@app.post("/api/accounts/select")
def accounts_select():
    data = request.get_json(silent=True) or request.form.to_dict(flat=True)
    sub_id = (data.get("subscription_id") or "").strip()
    if not sub_id:
        return jsonify({"ok": False, "error": "subscription_id is required"}), 400
    res = identity.set_az_account(sub_id)
    payload = res.to_dict()
    return jsonify(payload), (200 if res.ok else 400)


@app.post("/api/accounts/login")
def accounts_login_start():
    data = request.get_json(silent=True) or request.form.to_dict(flat=True)
    tenant_id = (data.get("tenant_id") or "").strip() or None
    login_id = identity.start_az_login(tenant_id=tenant_id)
    return jsonify({"login_id": login_id, "status": "running"})


@app.get("/api/accounts/login/<login_id>")
def accounts_login_status(login_id: str):
    st = identity.get_login_status(login_id)
    if not st:
        return jsonify({"ok": False, "error": "unknown login_id"}), 404
    return jsonify(st.to_public_dict())


@app.post("/api/accounts/login/<login_id>/cancel")
def accounts_login_cancel(login_id: str):
    ok = identity.cancel_login(login_id)
    return jsonify({"ok": ok})


# ----- Read-only helpers used by the form for live validation -----

@app.get("/api/check-slug")
def check_slug():
    """Return whether an agent folder already exists for the given slug.

    Slug is normalized + validated against ^[a-z0-9-]+$ before any filesystem
    access to defeat path-traversal attempts (e.g. ?slug=../foo).
    """
    raw = (request.args.get("slug") or "").strip().lower()
    slug = _slugify(raw) if raw else ""
    folder = _safe_slug_dir(slug)
    if folder is None:
        return jsonify({"ok": False, "error": "invalid slug"}), 400
    if not folder.exists():
        return jsonify({"ok": True, "slug": slug, "exists": False})
    summary: dict[str, Any] = {"ok": True, "slug": slug, "exists": True}
    report_path = folder / "_onboarding-report.json"
    if report_path.exists():
        try:
            data = json.loads(report_path.read_text(encoding="utf-8"))
            agent = data.get("agent") or {}
            summary["report"] = {
                "display_name": agent.get("display_name") or slug,
                "provider": agent.get("provider") or "",
                "generated_at": data.get("generated_at", ""),
                "tenant_id": data.get("tenant_id", ""),
            }
        except Exception as exc:  # noqa: BLE001
            summary["report"] = {"error": f"could not parse report: {exc!r}"}
    return jsonify(summary)


@app.get("/api/port-check")
def port_check():
    """Return whether the given TCP port is bindable on host.

    Best-effort: a free result can still race with another process. The
    /submit handler re-checks at submit time. Always also returns a
    `suggested` next-free port (or null) in the 20-port window above.
    """
    host = (request.args.get("host") or "127.0.0.1").strip() or "127.0.0.1"
    try:
        port = int(request.args.get("port") or "0")
    except ValueError:
        return jsonify({"ok": False, "error": "port must be an integer"}), 400
    if not (1 <= port <= 65535):
        return jsonify({"ok": False, "error": "port must be in 1..65535"}), 400
    # Only allow loopback hosts to be probed (no scanning of arbitrary remotes).
    if host not in ("127.0.0.1", "localhost", "::1"):
        return jsonify({"ok": False, "error": "host must be loopback"}), 400
    available = _port_is_free(host, port)
    suggested = None if available else _next_free_port(host, port)
    return jsonify({
        "ok": True,
        "host": host,
        "port": port,
        "available": available,
        "suggested": suggested,
    })


# ----- v1.4 agent launcher routes ------------------------------------------

@app.post("/api/agent/launch")
def agent_launch():
    """Spawn (or report on) a uvicorn process for a generated agent.

    Body: {"slug": "<agent-slug>"}.

    Idempotent: if already running, returns {status:"already_running"} and the
    existing pid/port without doing anything destructive."""
    data = request.get_json(silent=True) or {}
    slug = (data.get("slug") or "").strip().lower()
    folder = _safe_slug_dir(slug)
    if folder is None or not folder.exists():
        return jsonify({"ok": False, "error": "no such generated agent"}), 404

    existing = _agent_is_running(slug)
    if existing:
        return jsonify({
            "ok": True,
            "status": "already_running",
            "slug": slug,
            **existing,
            "url": f"http://{existing.get('host','127.0.0.1')}:{existing['port']}/",
        })

    # Read host/port from the onboarding report so we use the same values that
    # were generated into .env / server.py.
    report_path = folder / "_onboarding-report.json"
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"could not read onboarding report: {exc}"}), 500
    server_cfg = report.get("server") or {}
    host = (server_cfg.get("host") or "127.0.0.1").strip()
    try:
        port = int(server_cfg.get("port") or 8080)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "port in report is not an integer"}), 500
    if host not in ("127.0.0.1", "localhost", "::1"):
        return jsonify({"ok": False, "error": "agent must bind to a loopback host"}), 400

    # If another process (not ours) is on the port, refuse — would conflict.
    if not _port_is_free(host, port):
        return jsonify({
            "ok": False,
            "error": (
                f"Port {port} on {host} is already bound by another process. "
                f"Stop that process or re-generate the agent with a different port."
            ),
        }), 409

    q: queue.Queue = queue.Queue()
    started_at = datetime.now(timezone.utc).isoformat()
    with LAUNCHED_LOCK:
        LAUNCHED[slug] = {
            "status": "starting",
            "slug": slug,
            "host": host,
            "port": port,
            "started_at": started_at,
            "queue": q,
            "log_lines": [],
            "pid": None,
        }
    threading.Thread(
        target=_launcher_worker, args=(slug, folder, host, port),
        daemon=True, name=f"launch-{slug}",
    ).start()
    return jsonify({
        "ok": True,
        "status": "starting",
        "slug": slug,
        "host": host,
        "port": port,
        "stream_url": url_for("agent_launch_stream", slug=slug),
    })


@app.get("/api/agent/launch/<slug>/stream")
def agent_launch_stream(slug: str):
    """SSE stream of launcher log lines. Closes when the launch worker finishes."""
    folder = _safe_slug_dir(slug)
    if folder is None:
        return jsonify({"error": "invalid slug"}), 400
    with LAUNCHED_LOCK:
        rec = LAUNCHED.get(slug)
    if not rec:
        return jsonify({"error": "no launch in progress for this slug"}), 404
    q: queue.Queue = rec["queue"]

    def _gen():
        # Replay any lines that arrived before the client connected.
        for line in list(rec["log_lines"]):
            yield f"data: {json.dumps({'line': line})}\n\n"
        while True:
            try:
                line = q.get(timeout=30)
            except queue.Empty:
                yield ": keepalive\n\n"
                continue
            if line is None:
                payload = {
                    "line": "[END]",
                    "status": rec.get("status"),
                    "pid": rec.get("pid"),
                    "host": rec.get("host"),
                    "port": rec.get("port"),
                    "done": True,
                }
                yield f"data: {json.dumps(payload)}\n\n"
                break
            yield f"data: {json.dumps({'line': line})}\n\n"

    return Response(_gen(), mimetype="text/event-stream")


@app.get("/api/agent/status")
def agent_status():
    """Report whether the agent at <slug> is currently up + bound to its port."""
    slug = (request.args.get("slug") or "").strip().lower()
    folder = _safe_slug_dir(slug)
    if folder is None:
        return jsonify({"ok": False, "error": "invalid slug"}), 400
    state = _agent_is_running(slug)
    if not state:
        return jsonify({"ok": True, "running": False, "slug": slug})
    return jsonify({
        "ok": True,
        "running": True,
        "slug": slug,
        "pid": state.get("pid"),
        "host": state.get("host", "127.0.0.1"),
        "port": state.get("port"),
        "started_at": state.get("started_at"),
        "url": f"http://{state.get('host','127.0.0.1')}:{state['port']}/",
    })


@app.post("/api/agent/stop")
def agent_stop():
    """Kill the launched agent process (and its children) for <slug>."""
    data = request.get_json(silent=True) or {}
    slug = (data.get("slug") or "").strip().lower()
    folder = _safe_slug_dir(slug)
    if folder is None:
        return jsonify({"ok": False, "error": "invalid slug"}), 400
    state = _read_launch_state(slug)
    if not state or not state.get("pid"):
        return jsonify({"ok": True, "stopped": False, "reason": "no tracked pid"})
    pid = int(state["pid"])
    ok, msg = _kill_pid_tree(pid)
    if ok:
        _write_launch_state(slug, None)
        with LAUNCHED_LOCK:
            LAUNCHED.pop(slug, None)
        return jsonify({"ok": True, "stopped": True, "pid": pid, "message": msg})
    return jsonify({"ok": False, "stopped": False, "pid": pid, "error": msg}), 500


# -----------------------------------------------------------------------------
# Multi-vault picker API  (v1.8 — Key Vault picker)
# -----------------------------------------------------------------------------
# These endpoints back the in-form vault picker. They expose:
#   * the active az identity (UPN + tenant + signed-in user object id)
#   * the subscriptions, vaults, RGs, and regions the identity can see
#   * a live capability probe of a selected vault
#   * provisioning of a new vault + automatic Secrets Officer role grant
#
# All wrap azure_vaults.* and surface failures as structured JSON so the
# UI can render actionable messages (auth/forbidden/network/not_found).
# Read endpoints (GET) are not CSRF-gated (only POSTs are, per the existing
# before_request hook). Write endpoints (POST) are CSRF + Origin protected.
# -----------------------------------------------------------------------------
def _av_error_response(exc: Exception) -> tuple[Any, int]:
    """Translate an azure_vaults exception into a JSON error + HTTP code."""
    name = type(exc).__name__
    msg = str(exc)
    if name in ("ClientAuthenticationError", "CredentialUnavailableError"):
        return jsonify({
            "error": "Not signed in to Azure (or token expired). Run `az login` and reload.",
            "error_code": "auth",
            "detail": f"{name}: {msg[:300]}",
        }), 401
    if "HttpResponseError" in name and "403" in msg:
        return jsonify({
            "error": "Azure refused the request (403). Your identity lacks permission.",
            "error_code": "forbidden",
            "detail": msg[:300],
        }), 403
    return jsonify({
        "error": f"{name}: {msg[:300]}",
        "error_code": "unknown",
    }), 500


@app.get("/api/azure/whoami")
def azure_whoami():
    """Active az identity + signed-in-user object id (for the picker header)."""
    try:
        return jsonify(av.whoami())
    except Exception as exc:
        log.exception("azure_whoami failed")
        return _av_error_response(exc)


@app.get("/api/azure/subscriptions")
def azure_subscriptions():
    """Subscriptions visible to the current az identity."""
    try:
        return jsonify({"subscriptions": av.list_subscriptions()})
    except Exception as exc:
        log.exception("azure_subscriptions failed")
        return _av_error_response(exc)


@app.get("/api/azure/vaults")
def azure_vaults_list():
    """Key Vaults in a subscription. Query: ?subscription_id=<id>"""
    sub_id = (request.args.get("subscription_id") or "").strip()
    if not sub_id:
        return jsonify({"error": "subscription_id query param required"}), 400
    # Optional cache-bust for "Refresh from Azure" button
    if (request.args.get("refresh") or "").lower() in ("1", "true", "yes"):
        av.invalidate_cache()
    try:
        return jsonify({
            "vaults": av.list_vaults(sub_id),
            "subscription_id": sub_id,
        })
    except Exception as exc:
        log.exception("azure_vaults_list failed")
        return _av_error_response(exc)


@app.get("/api/azure/resource-groups")
def azure_resource_groups():
    """Resource groups in a subscription (for the Provision-New form)."""
    sub_id = (request.args.get("subscription_id") or "").strip()
    if not sub_id:
        return jsonify({"error": "subscription_id query param required"}), 400
    try:
        return jsonify({
            "resource_groups": av.list_resource_groups(sub_id),
            "subscription_id": sub_id,
        })
    except Exception as exc:
        log.exception("azure_resource_groups failed")
        return _av_error_response(exc)


@app.get("/api/azure/locations")
def azure_locations():
    """Azure regions available to a subscription."""
    sub_id = (request.args.get("subscription_id") or "").strip()
    if not sub_id:
        return jsonify({"error": "subscription_id query param required"}), 400
    try:
        return jsonify({
            "locations": av.list_locations(sub_id),
            "subscription_id": sub_id,
        })
    except Exception as exc:
        log.exception("azure_locations failed")
        return _av_error_response(exc)


@app.post("/api/azure/vaults/probe")
def azure_vault_probe():
    """Capability probe: can the current identity write/read/delete on this vault?

    Body: {"name": "<vault-name>"}.
    Returns the probe result directly so the UI can colour the row.
    """
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    try:
        return jsonify(av.probe_vault(name))
    except Exception as exc:
        log.exception("azure_vault_probe(%s) failed", name)
        return _av_error_response(exc)


@app.post("/api/azure/vaults")
def azure_vault_create():
    """Provision a new Key Vault + best-effort grant the current user Secrets Officer.

    Body: {"name", "subscription_id", "resource_group", "location"}.
    The vault tenant is derived from the active az identity (you can only
    create a vault in the tenant you are currently logged into).

    Blocks for ~1-3 minutes while the LRO completes. Returns the provision
    + role-assignment outcome; on partial success (vault created, role grant
    failed) the response is still 200 but the role_assignment.ok=False.
    """
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    sub_id = (body.get("subscription_id") or "").strip()
    rg = (body.get("resource_group") or "").strip()
    loc = (body.get("location") or "").strip()
    if not (name and sub_id and rg and loc):
        return jsonify({
            "error": "name, subscription_id, resource_group, location are all required",
            "error_code": "missing_field",
        }), 400

    # Sanity-check the vault name shape BEFORE making any cloud call.
    name_ok, name_err = av.validate_vault_name(name)
    if not name_ok:
        return jsonify({"error": name_err, "error_code": "name"}), 400

    # Tenant must match the currently-active az identity (Azure rejects
    # cross-tenant vault creation; surfacing this clearly here is cheaper
    # than waiting for the LRO to fail).
    me = av.whoami()
    tenant_id = me.get("tenant_id") or ""
    if not tenant_id:
        return jsonify({
            "error": "No active Azure identity. Run `az login` and retry.",
            "error_code": "auth",
        }), 401

    try:
        result = av.create_vault(
            subscription_id=sub_id,
            resource_group=rg,
            vault_name=name,
            location=loc,
            tenant_id=tenant_id,
        )
    except Exception as exc:
        log.exception("azure_vault_create failed")
        return _av_error_response(exc)
    return jsonify(result)


@app.post("/submit")
def submit():
    """Validate input, register a run, start a background worker, redirect to progress."""
    data = request.form.to_dict(flat=True)
    log.info("submit raw fields: %s", list(data.keys()))

    # ----- serialize: one onboarding at a time -----
    if not ONBOARDING_LOCK.acquire(blocking=False):
        return jsonify({
            "error": "another onboarding is already in flight",
            "in_flight_run_id": ONBOARDING_IN_FLIGHT.get("run_id"),
        }), 409
    locked = True
    try:
        # ----- Key Vault gate (refuses to write any literal secret to disk) -----
        # Per-onboarding override from the picker takes precedence over the
        # launcher default; either has to resolve to a vault name.
        kv_picked = (data.get("kv_vault_name") or "").strip()
        kv_effective = kv_picked or (kv.vault_name() or "")
        if not kv_effective:
            return jsonify({
                "error": (
                    "Azure Key Vault is not configured on this onboarder, and "
                    "no vault was picked in the form. Either set "
                    "AGENT_KV_VAULT_NAME=<vault-name> in the launcher environment, "
                    "or pick a vault using the picker on the form. The onboarder "
                    "refuses to write literal secrets to local disk."
                ),
                "kv_required": True,
            }), 400

        # If the user picked a non-default vault, defend against form-field
        # injection by verifying the vault is in the live list of vaults the
        # current identity can see. (Defense in depth: even if a malicious
        # local process injected a vault name, ARM would reject the write
        # at runtime - but failing fast here gives a much better error.)
        if kv_picked and kv_picked != (kv.vault_name() or ""):
            try:
                subs = av.list_subscriptions()
                accessible_names = set()
                for s in subs:
                    try:
                        for v in av.list_vaults(s["id"]):
                            accessible_names.add((v.get("name") or "").lower())
                    except Exception as exc:
                        log.warning(
                            "submit: list_vaults(%s) failed during whitelist: %s",
                            s.get("id"), exc,
                        )
                        # Best effort - one inaccessible subscription shouldn't
                        # block the whole submit if other subs cover the choice.
                if kv_picked.lower() not in accessible_names:
                    return jsonify({
                        "error": (
                            f"The picked vault {kv_picked!r} is not visible to "
                            f"your current Azure identity ({(av.whoami() or {}).get('user_name') or 'unknown'}). "
                            f"Refresh the picker and try again."
                        ),
                        "kv_required": True,
                    }), 400
            except Exception as exc:
                log.exception("submit: vault whitelist check failed")
                return jsonify({
                    "error": (
                        f"Could not verify access to vault {kv_picked!r}: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                    "kv_required": True,
                }), 400

        # ----- minimal validation -----
        display_name = (data.get("agent_display_name") or "").strip()
        if not display_name:
            return jsonify({"error": "Agent display name is required"}), 400

        # ----- explicit identity confirmation -----
        if (data.get("acting_as_confirm") or "").lower() != "on":
            return jsonify({
                "error": "You must explicitly confirm the identity you are "
                         "onboarding as (the confirmation checkbox in Step 3).",
            }), 400

        echoed_sub = (data.get("acting_as_subscription_id") or "").strip()
        echoed_tenant = (data.get("acting_as_tenant_id") or "").strip()
        echoed_user = (data.get("acting_as_user_name") or "").strip()
        if not echoed_tenant:
            return jsonify({
                "error": "No acting-as tenant submitted. Pick an account in Step 1.",
            }), 400

        # Re-resolve from live az and treat the hidden fields as echo-only.
        # If they differ from the live context the user has switched accounts
        # since they ticked the confirmation — that's a hard reject.
        live = identity.get_active_az_context()
        if not live:
            return jsonify({
                "error": "Not signed in to az anymore. Re-launch the onboarder.",
            }), 400
        live_tenant = live.get("tenantId") or ""
        live_sub = live.get("id") or ""
        live_user = (live.get("user") or {}).get("name") or ""
        if live_tenant != echoed_tenant or (echoed_sub and live_sub != echoed_sub):
            return jsonify({
                "error": (
                    "Active az identity changed between confirmation and "
                    "submit. Confirmed tenant {ce}, subscription {cs}; "
                    "active is tenant {lt}, subscription {ls}. Refresh the "
                    "picker and re-confirm."
                ).format(
                    ce=echoed_tenant, cs=echoed_sub or "(none)",
                    lt=live_tenant, ls=live_sub or "(none)",
                ),
            }), 409
        if echoed_user and live_user and echoed_user.lower() != live_user.lower():
            return jsonify({
                "error": (
                    f"Active az user changed since confirmation. Confirmed "
                    f"{echoed_user}, active is {live_user}. Refresh and re-confirm."
                ),
            }), 409

        # Identity is now SERVER-DERIVED, not user-supplied.
        acting_as = {
            "tenant_id": live_tenant,
            "tenant_default_domain": data.get("acting_as_tenant_domain", "").strip(),
            "subscription_id": live_sub,
            "subscription_name": live.get("name"),
            "user_name": live_user,
            "confirmed_at": datetime.now(timezone.utc).isoformat(),
        }

        slug = _slugify(data.get("agent_slug") or display_name)
        provider = (data.get("provider") or "custom").strip().lower()

        # ----- port collision guard (best-effort; UX-grade) -----
        server_host = data.get("server_host", "127.0.0.1").strip() or "127.0.0.1"
        try:
            server_port = int(data.get("server_port") or "8080")
        except (TypeError, ValueError):
            return jsonify({"error": "Server port must be an integer (1..65535)."}), 400
        if not (1 <= server_port <= 65535):
            return jsonify({"error": "Server port must be in 1..65535."}), 400
        if not _port_is_free(server_host, server_port):
            suggested = _next_free_port(server_host, server_port)
            return jsonify({
                "error": (
                    f"TCP port {server_port} on {server_host} is already in use. "
                    f"Pick a free port (suggested: {suggested})."
                ),
                "port": server_port,
                "suggested": suggested,
            }), 409

        # ----- duplicate-slug guard (paired with /history) -----
        existing = _safe_slug_dir(slug)
        if existing is None:
            return jsonify({"error": f"Invalid agent slug {slug!r}"}), 400
        overwrite = (data.get("overwrite_existing") or "").lower() == "on"
        if existing.exists():
            if not overwrite:
                # Surface enough detail for the form to render an inline warning.
                prev: dict[str, Any] = {"slug": slug, "folder": str(existing)}
                report_path = existing / "_onboarding-report.json"
                if report_path.exists():
                    try:
                        rep = json.loads(report_path.read_text(encoding="utf-8"))
                        prev["display_name"] = (rep.get("agent") or {}).get("display_name") or slug
                        prev["generated_at"] = rep.get("generated_at", "")
                        prev["provider"] = (rep.get("agent") or {}).get("provider", "")
                    except Exception:  # noqa: BLE001
                        pass
                return jsonify({
                    "error": (
                        f"An onboarded agent named {prev.get('display_name', slug)!r} "
                        f"(slug={slug}) already exists. To replace it, re-submit "
                        f"with the 'Overwrite previous run' checkbox ticked. "
                        f"(Note: the existing Entra app is NOT deleted.)"
                    ),
                    "duplicate_slug": True,
                    "existing": prev,
                }), 409
            # Archive the old folder rather than destroy it. Preserves any
            # local edits, .venv, audit.log, secrets the user added by hand.
            archive_ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            archive_path = existing.with_name(f"{slug}.archived-{archive_ts}")
            try:
                existing.rename(archive_path)
                log.info("archived previous run %s -> %s", existing, archive_path)
            except OSError as exc:
                return jsonify({
                    "error": (
                        f"Could not archive previous run at {existing}: {exc}. "
                        f"Close any process or editor holding files in that folder."
                    ),
                }), 409

        # ----- assemble config dict for the workflow -----
        cfg = {
            "agent_display_name": display_name,
            "agent_slug": slug,
            "agent_description": data.get("agent_description", "").strip(),
            "provider": provider,
            "provider_config": {
                "vertex_project": data.get("vertex_project", "").strip(),
                "vertex_location": data.get("vertex_location", "").strip(),
                "vertex_resource_name": data.get("vertex_resource_name", "").strip(),
                "vertex_adc_path": data.get("vertex_adc_path", "").strip(),
                "azure_openai_endpoint": data.get("azure_openai_endpoint", "").strip(),
                "azure_openai_deployment": data.get("azure_openai_deployment", "").strip(),
                "azure_openai_api_version": data.get("azure_openai_api_version", "2024-08-01-preview").strip(),
                "azure_openai_api_key": data.get("azure_openai_api_key", "").strip(),
                "openai_model": data.get("openai_model", "gpt-4o-mini").strip(),
                "openai_api_key": data.get("openai_api_key", "").strip(),
                "http_url": data.get("http_url", "").strip(),
                "http_method": data.get("http_method", "POST").strip().upper(),
                "http_prompt_field": data.get("http_prompt_field", "prompt").strip(),
                "http_response_jsonpath": data.get("http_response_jsonpath", "response").strip(),
                "http_auth_header": data.get("http_auth_header", "").strip(),
                "bedrock_region": data.get("bedrock_region", "us-east-1").strip(),
                "bedrock_mode": (data.get("bedrock_mode", "") or "model").strip().lower(),
                "bedrock_model_id": data.get("bedrock_model_id", "").strip(),
                "bedrock_agent_id": data.get("bedrock_agent_id", "").strip(),
                "bedrock_agent_alias_id": data.get("bedrock_agent_alias_id", "").strip(),
                "bedrock_session_id": data.get("bedrock_session_id", "").strip(),
                "bedrock_enable_trace": "1" if data.get("bedrock_enable_trace") == "on" else "",
                "bedrock_access_key_id": data.get("bedrock_access_key_id", "").strip(),
                "bedrock_secret_access_key": data.get("bedrock_secret_access_key", "").strip(),
                "bedrock_session_token": data.get("bedrock_session_token", "").strip(),
                "anthropic_model": data.get("anthropic_model", "claude-3-5-sonnet-20241022").strip(),
                "anthropic_api_key": data.get("anthropic_api_key", "").strip(),
                "anthropic_base_url": data.get("anthropic_base_url", "").strip(),
            },
            "tenant_id": live_tenant,
            "subscription_id": live_sub,
            "acting_as": acting_as,
            "monitoring": {
                "purview_dlp": data.get("purview_dlp") == "on",
                "purview_audit": data.get("purview_audit") == "on",
                "force_audit": data.get("force_audit") == "on",
                "a365_blueprint": data.get("a365_blueprint") == "on",
                "a365_observability": data.get("a365_observability") == "on",
            },
            "server": {
                "host": server_host,
                "port": server_port,
            },
            "kv_vault_name": kv_effective,
            "output_root": str(GENERATED),
        }

        run_id = uuid.uuid4().hex[:12]
        q: queue.Queue = queue.Queue()
        with RUNS_LOCK:
            RUNS[run_id] = {
                "id": run_id,
                "status": "running",
                "started_at": datetime.now(timezone.utc).isoformat(),
                "config": cfg,
                "queue": q,
                "log_lines": [],
                "result": None,
            }
        ONBOARDING_IN_FLIGHT["run_id"] = run_id

        def _on_log(line: str) -> None:
            with RUNS_LOCK:
                RUNS[run_id]["log_lines"].append(line)
            q.put(line)

        def _worker() -> None:
            try:
                _on_log(f"[INFO] Run {run_id} started for '{display_name}' (slug={slug}, provider={provider})")
                _on_log(
                    f"[INFO] Confirmed identity: {acting_as['user_name']} "
                    f"in tenant {acting_as['tenant_id']} "
                    f"(subscription {acting_as['subscription_id'] or '(none)'})"
                )
                result = workflow.run_onboarding(cfg, on_log=_on_log)
                with RUNS_LOCK:
                    RUNS[run_id]["status"] = "succeeded" if result.get("ok") else "failed"
                    RUNS[run_id]["result"] = result
                _on_log(f"[DONE] {RUNS[run_id]['status']}")
            except Exception as exc:  # pragma: no cover - keep the UI alive
                log.exception("workflow crashed")
                with RUNS_LOCK:
                    RUNS[run_id]["status"] = "failed"
                    RUNS[run_id]["result"] = {"ok": False, "error": str(exc)}
                _on_log(f"[FATAL] {exc!r}")
            finally:
                q.put(None)  # sentinel to close SSE
                # Release the global onboarding lock and clear the in-flight pointer.
                ONBOARDING_IN_FLIGHT["run_id"] = None
                try:
                    ONBOARDING_LOCK.release()
                except RuntimeError:
                    pass

        threading.Thread(target=_worker, daemon=True, name=f"onboard-{run_id}").start()
        # Worker now owns the lock; caller must not release it.
        locked = False
        return redirect(url_for("progress_page", run_id=run_id))
    finally:
        if locked:
            try:
                ONBOARDING_LOCK.release()
            except RuntimeError:
                pass


@app.get("/progress/<run_id>")
def progress_page(run_id: str):
    run = RUNS.get(run_id)
    if not run:
        abort(404)
    return render_template("progress.html", run_id=run_id, cfg=run["config"])


@app.get("/progress/<run_id>/stream")
def progress_stream(run_id: str):
    """Server-Sent Events stream of log lines."""
    run = RUNS.get(run_id)
    if not run:
        abort(404)
    q: queue.Queue = run["queue"]

    def _gen():
        # Replay any lines that arrived before the client connected.
        for line in list(run["log_lines"]):
            yield f"data: {json.dumps({'line': line})}\n\n"
        while True:
            try:
                line = q.get(timeout=30)
            except queue.Empty:
                yield ": keepalive\n\n"
                continue
            if line is None:
                payload = {"line": "[END]", "status": run["status"], "done": True}
                yield f"data: {json.dumps(payload)}\n\n"
                break
            yield f"data: {json.dumps({'line': line})}\n\n"

    return Response(_gen(), mimetype="text/event-stream")


@app.get("/result/<run_id>")
def result_page(run_id: str):
    run = RUNS.get(run_id)
    if not run:
        abort(404)
    return render_template(
        "result.html",
        run_id=run_id,
        run=run,
        cfg=run["config"],
        result=run.get("result") or {},
        csrf_token=CSRF_TOKEN,
        csrf_header=CSRF_HEADER,
    )


@app.get("/diagnostics")
def diagnostics_page():
    """Render a standalone diagnostics page (HTML)."""
    return render_template("diagnostics.html")


@app.get("/help/prerequisites")
def prerequisites_page():
    """Render the Prerequisites help content (v1.7.3, modal-friendly in v1.7.4).

    Pure documentation — no side effects, no state mutation, no auth. The
    content lives in a shared partial (`_prereq_body.html`) so the modal and
    the standalone page never drift.

    Modes:
      * default     -> standalone page wrapped in base.html (deep-linkable)
      * ?embed=1    -> raw fragment, fetched lazily by the in-page modal on
                       first open from the home page header button
    """
    if request.args.get("embed") in ("1", "true", "yes"):
        return render_template("prerequisites_embed.html")
    return render_template("prerequisites.html")


@app.post("/api/diagnostics")
def diagnostics_api():
    """Run diagnostics with the (partial) form payload and return JSON.

    The form may POST any subset of fields; we only require what's set.
    Accepts form-data, JSON, or an empty body.
    """
    data: dict = {}
    if request.form:
        data = request.form.to_dict(flat=True)
    else:
        try:
            data = request.get_json(silent=True) or {}
        except Exception:
            data = {}
    cfg = {
        "tenant_id": (data.get("tenant_id") or "").strip(),
        "provider": (data.get("provider") or "").strip().lower(),
        "server": {"port": int(data.get("server_port") or 8080)},
        "kv_vault_name": (data.get("kv_vault_name") or kv.vault_name() or "").strip(),
        "provider_config": {
            "vertex_project": data.get("vertex_project", "").strip(),
            "vertex_location": data.get("vertex_location", "").strip(),
            "vertex_resource_name": data.get("vertex_resource_name", "").strip(),
            "vertex_adc_path": data.get("vertex_adc_path", "").strip(),
            "azure_openai_endpoint": data.get("azure_openai_endpoint", "").strip(),
            "azure_openai_deployment": data.get("azure_openai_deployment", "").strip(),
            "azure_openai_api_version": data.get("azure_openai_api_version", "").strip(),
            "azure_openai_api_key": data.get("azure_openai_api_key", "").strip(),
            "openai_model": data.get("openai_model", "").strip(),
            "openai_api_key": data.get("openai_api_key", "").strip(),
            "http_url": data.get("http_url", "").strip(),
            "http_method": data.get("http_method", "POST").strip().upper(),
            "http_prompt_field": data.get("http_prompt_field", "prompt").strip(),
            "http_response_jsonpath": data.get("http_response_jsonpath", "response").strip(),
            "http_auth_header": data.get("http_auth_header", "").strip(),
            "bedrock_region": data.get("bedrock_region", "").strip(),
            "bedrock_mode": (data.get("bedrock_mode", "") or "model").strip().lower(),
            "bedrock_model_id": data.get("bedrock_model_id", "").strip(),
            "bedrock_agent_id": data.get("bedrock_agent_id", "").strip(),
            "bedrock_agent_alias_id": data.get("bedrock_agent_alias_id", "").strip(),
            "bedrock_session_id": data.get("bedrock_session_id", "").strip(),
            "bedrock_enable_trace": "1" if data.get("bedrock_enable_trace") == "on" else "",
            "bedrock_access_key_id": data.get("bedrock_access_key_id", "").strip(),
            "bedrock_secret_access_key": data.get("bedrock_secret_access_key", "").strip(),
            "bedrock_session_token": data.get("bedrock_session_token", "").strip(),
            "anthropic_model": data.get("anthropic_model", "").strip(),
            "anthropic_api_key": data.get("anthropic_api_key", "").strip(),
            "anthropic_base_url": data.get("anthropic_base_url", "").strip(),
        },
    }
    report = diagnostics.run_all(cfg)
    return jsonify(report)


@app.get("/healthz")
def healthz():
    return {"status": "ok", "runs_in_memory": len(RUNS)}


def main() -> None:
    host = os.environ.get("ONBOARDER_HOST", "127.0.0.1")
    port = int(os.environ.get("ONBOARDER_PORT", "5050"))
    set_bind_address(host, port)
    log.info("Onboarder UI listening on http://%s:%s/", host, port)
    log.info("CSRF token minted for this launch (length=%d)", len(CSRF_TOKEN))
    log.info("Host header allowlist: %s", sorted(_ALLOWED_HOSTS))
    # threaded=True so SSE doesn't block the form post handler
    app.run(host=host, port=port, debug=False, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()

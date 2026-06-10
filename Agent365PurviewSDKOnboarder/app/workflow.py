"""Agent SDK Onboarder — workflow.

This module performs the actual onboarding:
  1. Preflight checks (az login + a365 if requested)
  2. Create Entra app + service principal + client secret
  3. Add + admin-consent Purview API permissions
  4. (Optional) Run a365 setup blueprint
  5. Generate the wrapper code for the user's agent
  6. Write a JSON onboarding report

Every step pushes status to a caller-supplied `on_log` callback so the UI can
stream progress via SSE.

The module is deliberately defensive: every external call is wrapped, and the
overall function never raises -- failures are recorded in the return dict so the
UI can render them.
"""
from __future__ import annotations

import json
import os
import secrets
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import codegen
import identity
from identity import IdentityMismatch

LogFn = Callable[[str], None]

# -----------------------------------------------------------------------------
# Windows: subprocess.run(["az", ...]) without shell=True does NOT find az.cmd
# via PATHEXT. We resolve the full path once at module load and substitute it
# transparently inside _run() so callers can keep using the bare "az"/"a365"
# names. Same gotcha is handled in identity.py for the picker endpoints.
# -----------------------------------------------------------------------------
_AZ_PATH = shutil.which("az") or "az"
_A365_PATH = shutil.which("a365") or "a365"
_EXEC_PATHS = {"az": _AZ_PATH, "a365": _A365_PATH}


def _resolve_argv(cmd):
    """Return cmd with cmd[0] replaced by its full path if known (Windows fix).

    Accepts and returns either a list or a string unchanged. For list commands
    where the first token is a known CLI (`az`, `a365`), the first element is
    swapped for the absolute path resolved at module load. String commands are
    passed through (they go through shell=True anyway).
    """
    if isinstance(cmd, list) and cmd and cmd[0] in _EXEC_PATHS:
        return [_EXEC_PATHS[cmd[0]]] + list(cmd[1:])
    return cmd

# -----------------------------------------------------------------------------
# Microsoft 1st-party app ids we need to grant permissions on
# -----------------------------------------------------------------------------
# Microsoft Graph publishes the Purview governance APIs we need
# (dataSecurityAndGovernance.protectionScopes/compute, processContent,
# activities/contentActivities). The previously-used MIP Sync app
# (870c4f2e-85b6-4d43-bdda-6ed9a579b725) and the standalone Purview Ecosystem
# app no longer expose the right roles for tenant-app calls.
GRAPH_RESOURCE_APP_ID = "00000003-0000-0000-c000-000000000000"
GRAPH_DELEGATED_USER_READ = "User.Read"

# Application roles on Microsoft Graph required by the generated wrapper.
# Names must match the appRole "value" on the Graph SPN.
GRAPH_CONTENT_PROCESS_ROLE = "Content.Process.All"
GRAPH_PROTECTION_SCOPES_ROLE = "ProtectionScopes.Compute.All"

# Office 365 Management APIs (audit log search)
M365_MGMT_RESOURCE_APP_ID = "c5393580-f805-4401-95e8-94b7a6ef2fc2"
M365_MGMT_ACTIVITY_FEED_DLP_ROLE = "ActivityFeed.ReadDlp"


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def _run(cmd: list[str] | str, *, on_log: LogFn, capture: bool = True,
         check: bool = True, timeout: int = 120) -> subprocess.CompletedProcess:
    """Run a shell command, stream a short summary, return the completed process."""
    if isinstance(cmd, list):
        printable = " ".join(shlex.quote(c) for c in cmd)
    else:
        printable = cmd
    on_log(f"$ {printable}")
    resolved = _resolve_argv(cmd)
    try:
        proc = subprocess.run(
            resolved,
            shell=isinstance(resolved, str),
            capture_output=capture,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        on_log(f"[ERR] command not found: {exc}")
        raise
    if proc.returncode != 0:
        # Show stderr tail
        err = (proc.stderr or "").strip().splitlines()
        if err:
            for line in err[-10:]:
                on_log(f"  ! {line}")
        if check:
            raise RuntimeError(f"command failed (exit={proc.returncode}): {printable}")
    return proc


def _az_json(cmd: list[str], *, on_log: LogFn, check: bool = True) -> Any:
    """Run an `az` command that returns JSON; parse it."""
    proc = _run(cmd, on_log=on_log, check=check)
    out = (proc.stdout or "").strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        on_log(f"[WARN] could not parse JSON from: {' '.join(cmd[:3])}")
        return None


def try_get_az_context() -> dict[str, Any]:
    """Best-effort: return `az account show` as dict, or empty.

    Kept as a thin wrapper around identity.get_active_az_context() for
    backwards compatibility with older call sites (e.g. the form view).
    """
    return identity.get_active_az_context() or {}


def _run_destructive_az(
    cmd: list[str],
    *,
    on_log: LogFn,
    cfg: dict,
    check: bool = True,
    timeout: int = 120,
) -> subprocess.CompletedProcess:
    """Run a destructive `az` command after re-verifying tenant identity.

    Per the identity-guard design: re-check `az account show` BEFORE every
    state-mutating call so a context switch in another terminal mid-run is
    caught before we create resources in the wrong tenant.
    """
    expected_tenant = (cfg.get("acting_as") or {}).get("tenant_id")
    expected_user = (cfg.get("acting_as") or {}).get("user_name")
    if not expected_tenant:
        raise RuntimeError(
            "_run_destructive_az: cfg['acting_as']['tenant_id'] is required"
        )
    identity.assert_az_identity(
        expected_tenant,
        expected_user_name=expected_user,
        on_log=on_log,
    )
    return _run(cmd, on_log=on_log, check=check, timeout=timeout)


def _destructive_az_json(
    cmd: list[str],
    *,
    on_log: LogFn,
    cfg: dict,
    check: bool = True,
) -> Any:
    """Like _az_json but routed through _run_destructive_az."""
    proc = _run_destructive_az(cmd, on_log=on_log, cfg=cfg, check=check)
    out = (proc.stdout or "").strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        on_log(f"[WARN] could not parse JSON from: {' '.join(cmd[:3])}")
        return None


# -----------------------------------------------------------------------------
# Workflow steps
# -----------------------------------------------------------------------------
@dataclass
class StepResult:
    name: str
    ok: bool
    detail: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


def _step_preflight(cfg: dict, on_log: LogFn) -> StepResult:
    on_log("=== Preflight checks ===")

    expected_tenant = (cfg.get("acting_as") or {}).get("tenant_id")
    expected_user = (cfg.get("acting_as") or {}).get("user_name")
    if not expected_tenant:
        return StepResult(
            "preflight", False,
            error="cfg['acting_as']['tenant_id'] missing. The form must "
                  "send the identity the user explicitly confirmed.",
        )

    try:
        ctx = identity.assert_az_identity(
            expected_tenant,
            expected_user_name=expected_user,
            on_log=on_log,
        )
    except IdentityMismatch as exc:
        return StepResult("preflight", False, error=str(exc))

    on_log(f"  signed in as: {ctx.get('user', {}).get('name')}")
    on_log(f"  tenant      : {ctx.get('tenantId')}")
    on_log(f"  subscription: {ctx.get('name')} ({ctx.get('id')})")
    on_log(
        f"  confirmed identity matches active az context "
        f"(tenant {expected_tenant})"
    )

    # If the user wants Agent 365 features, make sure the CLI is installed.
    if cfg["monitoring"].get("a365_blueprint") or cfg["monitoring"].get("a365_observability"):
        try:
            v = subprocess.run([_A365_PATH, "--version"], capture_output=True, text=True, timeout=15)
            if v.returncode == 0:
                on_log(f"  a365 CLI    : {v.stdout.strip()}")
            else:
                on_log("[WARN] a365 CLI not available. Install via: dotnet tool install --global Microsoft.Agents.A365.DevTools.Cli")
        except FileNotFoundError:
            on_log("[WARN] a365 CLI not on PATH. Install via: dotnet tool install --global Microsoft.Agents.A365.DevTools.Cli")

    return StepResult("preflight", True, detail={"az": ctx})


def _step_create_entra_app(cfg: dict, on_log: LogFn) -> StepResult:
    on_log("=== Creating Entra app registration (Purview governance SPN) ===")
    display = cfg["agent_display_name"]
    # 1. create app
    app = _destructive_az_json(
        ["az", "ad", "app", "create", "--display-name", display,
         "--sign-in-audience", "AzureADMyOrg", "-o", "json"],
        on_log=on_log, cfg=cfg,
    )
    if not app or "appId" not in app:
        return StepResult("create_entra_app", False, error="az ad app create returned no appId")
    app_id = app["appId"]
    app_object_id = app["id"]
    on_log(f"  appId       : {app_id}")
    on_log(f"  objectId    : {app_object_id}")

    # 2. create the service principal (skip-if-exists)
    sp = _destructive_az_json(
        ["az", "ad", "sp", "create", "--id", app_id, "-o", "json"],
        on_log=on_log, cfg=cfg, check=False,
    )
    if sp:
        sp_object_id = sp.get("id")
        on_log(f"  sp objectId : {sp_object_id}")
    else:
        # already exists -- look it up (read-only, no guard needed)
        existing = _az_json(["az", "ad", "sp", "list", "--filter", f"appId eq '{app_id}'", "-o", "json"],
                            on_log=on_log)
        sp_object_id = (existing or [{}])[0].get("id")

    # 3. create a client secret (10-year, the SPN is for a long-lived service)
    cred = _destructive_az_json(
        ["az", "ad", "app", "credential", "reset", "--id", app_id,
         "--display-name", "onboarder-default", "--years", "2", "-o", "json"],
        on_log=on_log, cfg=cfg,
    )
    if not cred or "password" not in cred:
        return StepResult("create_entra_app", False, error="failed to mint client secret")
    secret = cred["password"]
    on_log("  client secret minted")

    # 4. register the OAuth web redirect URIs for the generated wrapper's /login
    #    flow. Without this every first sign-in fails with AADSTS500113 ("No
    #    reply address is registered for the application"). We register both
    #    127.0.0.1 and localhost variants because browsers + MSAL treat them
    #    as distinct hosts. Idempotent: az ad app update with --web-redirect-uris
    #    REPLACES the list, so we read existing first and merge.
    server_cfg = cfg.get("server") or {}
    host = (server_cfg.get("host") or "127.0.0.1").strip() or "127.0.0.1"
    try:
        port = int(server_cfg.get("port") or 8080)
    except (TypeError, ValueError):
        port = 8080
    redirect_uris = _wrapper_redirect_uris(host, port)
    on_log(f"  registering web redirect URIs: {', '.join(redirect_uris)}")
    _ensure_redirect_uris(app_id, redirect_uris, on_log=on_log, cfg=cfg)

    return StepResult("create_entra_app", True, detail={
        "app_id": app_id,
        "app_object_id": app_object_id,
        "sp_object_id": sp_object_id,
        "client_secret": secret,
        "client_secret_end_date": cred.get("endDateTime"),
        "redirect_uris": redirect_uris,
    })


def _wrapper_redirect_uris(host: str, port: int) -> list[str]:
    """OAuth redirect URIs the generated wrapper's /login flow expects.

    Mirrors server.py.tmpl's `OAUTH_REDIRECT_URI` default. We register both
    127.0.0.1 and localhost forms because:
      * MSAL treats them as different hosts (they are; cookies don't share).
      * If the user clicks the launcher's "Open" link (127.0.0.1) vs types
        "localhost" themselves, both must work.
    """
    uris = [f"http://{host}:{port}/auth/callback"]
    # add the alt form whichever way we picked
    if host in ("127.0.0.1", "::1"):
        uris.append(f"http://localhost:{port}/auth/callback")
    elif host == "localhost":
        uris.append(f"http://127.0.0.1:{port}/auth/callback")
    # de-dup, preserve order
    seen: set[str] = set()
    out: list[str] = []
    for u in uris:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _ensure_redirect_uris(app_id: str, want: list[str], on_log: LogFn, cfg: dict) -> None:
    """Idempotently merge `want` into the Entra app's web.redirectUris.

    `az ad app update --web-redirect-uris` REPLACES the entire list, so we
    first read the current set, union with `want`, then write back. Failures
    are logged but do not raise — sign-in will fail loudly later with a
    clear AADSTS500113, which the user can fix manually with the same
    `az ad app update` command we log here.
    """
    current = _az_json(
        ["az", "ad", "app", "show", "--id", app_id,
         "--query", "web.redirectUris", "-o", "json"],
        on_log=on_log, check=False,
    ) or []
    if not isinstance(current, list):
        current = []
    merged: list[str] = []
    seen: set[str] = set()
    for u in list(current) + list(want):
        if u and u not in seen:
            seen.add(u)
            merged.append(u)
    if set(merged) == set(current):
        on_log("  redirect URIs already up to date, skipping write")
        return
    args = ["az", "ad", "app", "update", "--id", app_id, "--web-redirect-uris", *merged]
    try:
        _run_destructive_az(args, on_log=on_log, cfg=cfg, check=True)
        on_log(f"  ✓ web.redirectUris now: {', '.join(merged)}")
    except Exception as exc:  # noqa: BLE001
        on_log(f"  [WARN] could not register redirect URIs ({exc!s}); "
               f"sign-in will fail with AADSTS500113 until you run:")
        on_log("         " + " ".join(args))


def _resolve_role_id(on_log: LogFn, resource_app_id: str, role_value: str) -> Optional[str]:
    """Look up an appRole id by value on a target resource SP."""
    sp_list = _az_json(
        ["az", "ad", "sp", "list", "--filter", f"appId eq '{resource_app_id}'", "-o", "json"],
        on_log=on_log, check=False,
    )
    if not sp_list:
        return None
    roles = sp_list[0].get("appRoles", [])
    for r in roles:
        if r.get("value") == role_value:
            return r.get("id")
    return None


def _step_grant_purview_perms(cfg: dict, ctx: dict, on_log: LogFn) -> StepResult:
    on_log("=== Granting Purview API permissions ===")
    app_id = ctx["app_id"]

    perms_added: list[dict] = []
    consent_attempted = False
    consent_ok = False

    if cfg["monitoring"].get("purview_dlp"):
        # Both roles live on Microsoft Graph and are required by the wrapper:
        #   - Content.Process.All           -> processContent + contentActivities
        #   - ProtectionScopes.Compute.All  -> protectionScopes/compute
        for role_name in (GRAPH_CONTENT_PROCESS_ROLE, GRAPH_PROTECTION_SCOPES_ROLE):
            on_log(f"  - looking up Microsoft Graph role: {role_name} ...")
            role_id = _resolve_role_id(on_log, GRAPH_RESOURCE_APP_ID, role_name)
            if role_id:
                on_log(f"    role id: {role_id}")
                _run_destructive_az(
                    ["az", "ad", "app", "permission", "add",
                     "--id", app_id,
                     "--api", GRAPH_RESOURCE_APP_ID,
                     "--api-permissions", f"{role_id}=Role"],
                    on_log=on_log, cfg=cfg, check=False,
                )
                perms_added.append({"api": "Microsoft Graph", "role": role_name})
            else:
                on_log(f"[WARN] could not resolve role {role_name} on Microsoft Graph "
                       f"({GRAPH_RESOURCE_APP_ID}). Skipping; admin will need to add it manually.")

    if cfg["monitoring"].get("purview_audit"):
        on_log("  - looking up ActivityFeed.ReadDlp role id...")
        role_id = _resolve_role_id(on_log, M365_MGMT_RESOURCE_APP_ID, M365_MGMT_ACTIVITY_FEED_DLP_ROLE)
        if role_id:
            on_log(f"    role id: {role_id}")
            _run_destructive_az(
                ["az", "ad", "app", "permission", "add",
                 "--id", app_id,
                 "--api", M365_MGMT_RESOURCE_APP_ID,
                 "--api-permissions", f"{role_id}=Role"],
                on_log=on_log, cfg=cfg, check=False,
            )
            perms_added.append({"api": "O365 Mgmt", "role": M365_MGMT_ACTIVITY_FEED_DLP_ROLE})
        else:
            on_log(f"[WARN] could not resolve role {M365_MGMT_ACTIVITY_FEED_DLP_ROLE}")

    # admin consent (will only succeed if the signed-in user is GA / Privileged Role Admin)
    if perms_added:
        on_log("  - attempting admin-consent (requires Global Admin to succeed)...")
        consent_attempted = True
        try:
            _run_destructive_az(
                ["az", "ad", "app", "permission", "admin-consent", "--id", app_id],
                on_log=on_log, cfg=cfg, check=True, timeout=60,
            )
            consent_ok = True
            on_log("    admin consent OK")
        except Exception as exc:
            on_log(f"    admin consent FAILED: {exc}")
            on_log("    -> An admin must manually consent in Entra portal:")
            on_log(f"       https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps/ApplicationMenuBlade/~/CallAnAPI/appId/{app_id}")

    return StepResult("grant_purview_perms", True, detail={
        "permissions": perms_added,
        "consent_attempted": consent_attempted,
        "consent_ok": consent_ok,
    })


def _step_a365_blueprint(cfg: dict, ctx: dict, on_log: LogFn) -> StepResult:
    if not cfg["monitoring"].get("a365_blueprint"):
        on_log("=== Agent 365 Blueprint: SKIPPED (not requested) ===")
        return StepResult("a365_blueprint", True, detail={"skipped": True})

    on_log("=== Creating Agent 365 Blueprint ===")
    slug = cfg["agent_slug"]
    work_dir = Path(cfg["output_root"]) / slug / "a365_blueprint"
    work_dir.mkdir(parents=True, exist_ok=True)
    on_log(f"  working dir: {work_dir}")

    try:
        _run(["a365", "setup", "requirements", "-v"],
             on_log=on_log, check=False, timeout=300)
    except Exception as exc:
        on_log(f"[WARN] requirements check threw: {exc}")

    try:
        proc = subprocess.run(
            [_A365_PATH, "setup", "blueprint", "-n", slug, "--no-endpoint", "-v"],
            cwd=str(work_dir),
            capture_output=True, text=True, timeout=600,
        )
        for line in (proc.stdout or "").splitlines():
            if line.strip():
                on_log(f"  a365| {line}")
        if proc.returncode != 0:
            for line in (proc.stderr or "").splitlines()[-15:]:
                on_log(f"  a365! {line}")
            return StepResult("a365_blueprint", False, error=f"a365 blueprint exited {proc.returncode}")
    except FileNotFoundError:
        return StepResult("a365_blueprint", False,
                          error="a365 CLI not installed. Run: dotnet tool install --global Microsoft.Agents.A365.DevTools.Cli")
    except subprocess.TimeoutExpired:
        return StepResult("a365_blueprint", False, error="a365 blueprint timed out (browser consent likely not completed)")

    # parse the generated config file
    cfg_path = work_dir / "a365.generated.config.json"
    if cfg_path.exists():
        try:
            bp = json.loads(cfg_path.read_text(encoding="utf-8"))
            on_log(f"  blueprint id: {bp.get('agentBlueprintId')}")
            return StepResult("a365_blueprint", True, detail={
                "agent_blueprint_id": bp.get("agentBlueprintId"),
                "agent_blueprint_sp_object_id": bp.get("agentBlueprintServicePrincipalObjectId"),
                "config_path": str(cfg_path),
            })
        except Exception as exc:
            return StepResult("a365_blueprint", False, error=f"could not read blueprint config: {exc}")
    return StepResult("a365_blueprint", False, error="blueprint config file not found")


def _step_codegen(cfg: dict, ctx: dict, on_log: LogFn) -> StepResult:
    on_log("=== Generating wrapper service code ===")
    out_dir = Path(cfg["output_root"]) / cfg["agent_slug"]
    out_dir.mkdir(parents=True, exist_ok=True)

    files_written = codegen.render_project(
        out_dir=out_dir,
        cfg=cfg,
        entra=ctx,
        log=on_log,
    )
    for f in files_written:
        on_log(f"  wrote {f.relative_to(out_dir)}")

    return StepResult("codegen", True, detail={
        "out_dir": str(out_dir),
        "files": [str(f.relative_to(out_dir)) for f in files_written],
    })


def _step_write_report(cfg: dict, all_steps: list[StepResult],
                       out_dir: Path, on_log: LogFn) -> StepResult:
    on_log("=== Writing onboarding report ===")
    acting_as = cfg.get("acting_as") or {}
    report = {
        "agent": {
            "display_name": cfg["agent_display_name"],
            "slug": cfg["agent_slug"],
            "provider": cfg["provider"],
        },
        "tenant_id": cfg["tenant_id"],
        "subscription_id": cfg["subscription_id"],
        "acting_as": {
            "tenant_id": acting_as.get("tenant_id"),
            "subscription_id": acting_as.get("subscription_id"),
            "user_name": acting_as.get("user_name"),
            "tenant_default_domain": acting_as.get("tenant_default_domain"),
            "confirmed_at": acting_as.get("confirmed_at"),
        },
        "monitoring": cfg["monitoring"],
        "server": cfg["server"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "steps": [
            {
                "name": s.name,
                "ok": s.ok,
                "error": s.error,
                "detail": {k: v for k, v in s.detail.items() if k != "client_secret"},
            }
            for s in all_steps
        ],
    }
    report_path = out_dir / "_onboarding-report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    on_log(f"  report: {report_path}")
    return StepResult("write_report", True, detail={"report_path": str(report_path)})


# -----------------------------------------------------------------------------
# Public entrypoint
# -----------------------------------------------------------------------------
def run_onboarding(cfg: dict, on_log: LogFn) -> dict[str, Any]:
    """Drive the full workflow. Always returns a dict (never raises)."""
    all_steps: list[StepResult] = []
    entra_ctx: dict[str, Any] = {}
    out_dir = Path(cfg["output_root"]) / cfg["agent_slug"]

    def _bail(reason: str) -> dict[str, Any]:
        on_log(f"[STOP] {reason}")
        return {"ok": False, "error": reason, "steps": [_s.__dict__ for _s in all_steps],
                "out_dir": str(out_dir)}

    try:
        # 1. preflight
        s = _step_preflight(cfg, on_log)
        all_steps.append(s)
        if not s.ok:
            return _bail(s.error or "preflight failed")

        # 2. Entra app
        s = _step_create_entra_app(cfg, on_log)
        all_steps.append(s)
        if not s.ok:
            return _bail(s.error or "entra app failed")
        entra_ctx = s.detail

        # 3. Purview perms
        s = _step_grant_purview_perms(cfg, entra_ctx, on_log)
        all_steps.append(s)
        # (non-fatal: continue even if consent failed -- user can do it manually)

        # 4. Optional Agent 365 blueprint
        s = _step_a365_blueprint(cfg, entra_ctx, on_log)
        all_steps.append(s)
        if not s.ok and cfg["monitoring"].get("a365_blueprint"):
            on_log(f"[WARN] a365 blueprint failed: {s.error}. Continuing with code generation.")

        # 5. Codegen
        s = _step_codegen(cfg, entra_ctx, on_log)
        all_steps.append(s)
        if not s.ok:
            return _bail(s.error or "codegen failed")

        # 6. Report
        _step_write_report(cfg, all_steps, out_dir, on_log)
    except IdentityMismatch as exc:
        # Live az context drifted mid-run. Bail safely before doing more
        # destructive work; preceding partial state may need manual cleanup.
        return _bail(f"identity drift detected: {exc}")

    # Summary
    on_log("")
    on_log(" Onboarding complete.")
    on_log(f"   Output folder : {out_dir}")
    on_log(f"   Entra app id  : {entra_ctx.get('app_id')}")
    on_log("   Next steps:")
    on_log(f"     cd \"{out_dir}\"")
    on_log("     python -m venv .venv && .venv\\Scripts\\activate")
    on_log("     pip install -r requirements.txt")
    on_log("     python -m uvicorn server:app --host 127.0.0.1 --port "
           f"{cfg['server']['port']}")

    return {
        "ok": True,
        "out_dir": str(out_dir),
        "app_id": entra_ctx.get("app_id"),
        "sp_object_id": entra_ctx.get("sp_object_id"),
        "steps": [_s.__dict__ for _s in all_steps],
    }

"""Identity management for the Agent SDK Onboarder.

Centralizes everything related to *which* Azure identity the workflow will run
as: listing `az` accounts, switching active subscription, kicking off a fresh
`az login`, and (critically) verifying the live identity before any destructive
operation.

Threading model
---------------
- All public functions are safe to call from any Flask request thread.
- Background `az login` runs in its own daemon thread; the worker enforces its
  own timeout via `proc.communicate(timeout=...)` (do NOT rely on polling).
- A single module-level `_LOCK` guards `_LOGINS` and serializes account
  mutations (`set_az_account`, `start_az_login`) so multiple browser tabs
  cannot corrupt each other.

Identity model
--------------
Tenant-first, NOT subscription-first. A user can have an Entra identity in a
tenant with zero subscriptions (directory-only / guest). The onboarder needs
the tenant; subscription is optional context.
"""
from __future__ import annotations

import json
import logging
import secrets
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

log = logging.getLogger("onboarder.identity")

# Resolve `az` to a full path once. On Windows, `subprocess.run(["az", ...])`
# without shell=True won't find az.cmd via PATHEXT, so we must hand it the
# full path that shutil.which() returns (which respects PATHEXT).
_AZ_PATH = shutil.which("az") or "az"

# How long an interactive `az login` may take before we kill the orphan.
LOGIN_TIMEOUT_SECONDS = 300

# How long we keep a finished login record around for polling.
LOGIN_RECORD_TTL_SECONDS = 30 * 60

_LOCK = threading.Lock()
_LOGINS: dict[str, "LoginState"] = {}


# -----------------------------------------------------------------------------
# Data types
# -----------------------------------------------------------------------------
@dataclass
class AccountListResult:
    """Structured result of `list_az_accounts()`.

    Distinguishes between "az not installed", "az installed but no accounts",
    and "we got a real account list". The Flask layer turns this into JSON.
    """
    ok: bool
    az_installed: bool
    accounts: list[dict] = field(default_factory=list)
    active_subscription_id: Optional[str] = None
    active_tenant_id: Optional[str] = None
    active_user_name: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "az_installed": self.az_installed,
            "accounts": self.accounts,
            "active_subscription_id": self.active_subscription_id,
            "active_tenant_id": self.active_tenant_id,
            "active_user_name": self.active_user_name,
            "error": self.error,
        }


@dataclass
class LoginState:
    """Bookkeeping for a single background `az login` invocation."""
    login_id: str
    tenant_id: Optional[str]
    status: str = "running"  # running | succeeded | failed | cancelled | timeout
    error: Optional[str] = None
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    proc: Optional[subprocess.Popen] = None
    # On success, we refresh the account list onto the record so the client
    # gets it in one round-trip.
    accounts_after: Optional[AccountListResult] = None

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "login_id": self.login_id,
            "tenant_id": self.tenant_id,
            "status": self.status,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "accounts": (
                self.accounts_after.to_dict() if self.accounts_after else None
            ),
        }


# -----------------------------------------------------------------------------
# az helpers (read-only)
# -----------------------------------------------------------------------------
def _run_az_json(args: list[str], timeout: int = 20) -> tuple[bool, Any, str]:
    """Run `az <args> -o json` and return (ok, parsed, error_text).

    Returns (False, None, "az_not_found") if the az CLI is missing — callers
    are expected to surface that distinctly.
    """
    cmd = [_AZ_PATH, *args]
    if "-o" not in args and "--output" not in args:
        cmd += ["-o", "json"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return False, None, "az_not_found"
    except subprocess.TimeoutExpired:
        return False, None, f"timeout after {timeout}s: {' '.join(cmd)}"
    if proc.returncode != 0:
        return False, None, (proc.stderr or proc.stdout or "az command failed").strip()
    out = (proc.stdout or "").strip()
    if not out:
        return True, None, ""
    try:
        return True, json.loads(out), ""
    except json.JSONDecodeError as exc:
        return False, None, f"could not parse az output as JSON: {exc}"


def list_az_accounts() -> AccountListResult:
    """List every Azure account the user is signed into (all tenants).

    Returns a structured result distinguishing the failure modes the UI cares
    about. We never raise — the Flask layer always gets a renderable result.
    """
    ok, parsed, err = _run_az_json(["account", "list", "--all"])
    if not ok:
        if err == "az_not_found":
            return AccountListResult(
                ok=False, az_installed=False,
                error="Azure CLI ('az') is not installed or not on PATH. "
                      "Install from https://aka.ms/installazurecli, then refresh.",
            )
        return AccountListResult(
            ok=False, az_installed=True,
            error=err or "az account list failed",
        )

    raw = parsed if isinstance(parsed, list) else []
    accounts = []
    active_sub = None
    active_tenant = None
    active_user = None
    for a in raw:
        entry = {
            "subscription_id": a.get("id"),
            "subscription_name": a.get("name"),
            "tenant_id": a.get("tenantId"),
            "tenant_default_domain": a.get("tenantDefaultDomain")
                                    or a.get("tenantDisplayName")
                                    or "",
            "user_name": (a.get("user") or {}).get("name"),
            "user_type": (a.get("user") or {}).get("type"),
            "state": a.get("state"),
            "is_default": bool(a.get("isDefault")),
            "home_tenant_id": a.get("homeTenantId"),
            "environment": a.get("environmentName") or "AzureCloud",
        }
        accounts.append(entry)
        if entry["is_default"]:
            active_sub = entry["subscription_id"]
            active_tenant = entry["tenant_id"]
            active_user = entry["user_name"]

    return AccountListResult(
        ok=True, az_installed=True,
        accounts=accounts,
        active_subscription_id=active_sub,
        active_tenant_id=active_tenant,
        active_user_name=active_user,
    )


def get_active_az_context() -> Optional[dict]:
    """Return current `az account show` as a dict, or None if not signed in."""
    ok, parsed, _ = _run_az_json(["account", "show"])
    if ok and isinstance(parsed, dict):
        return parsed
    return None


# -----------------------------------------------------------------------------
# az mutations (locked)
# -----------------------------------------------------------------------------
def set_az_account(subscription_id: str) -> AccountListResult:
    """Switch the global active Azure CLI account by subscription id.

    NOTE: this mutates the user's `~/.azure/azureProfile.json` and therefore
    affects every other terminal that uses az. The form warns the user about
    this — the safety net for the workflow itself is `assert_az_identity`,
    not this call.
    """
    if not subscription_id:
        return AccountListResult(
            ok=False, az_installed=True,
            error="subscription_id is required",
        )
    with _LOCK:
        try:
            proc = subprocess.run(
                [_AZ_PATH, "account", "set", "--subscription", subscription_id],
                capture_output=True, text=True, timeout=15,
            )
        except FileNotFoundError:
            return AccountListResult(
                ok=False, az_installed=False,
                error="Azure CLI ('az') not found on PATH.",
            )
        except subprocess.TimeoutExpired:
            return AccountListResult(
                ok=False, az_installed=True,
                error="az account set timed out",
            )
        if proc.returncode != 0:
            return AccountListResult(
                ok=False, az_installed=True,
                error=(proc.stderr or proc.stdout or "az account set failed").strip(),
            )
    # Re-read outside the lock — list_az_accounts has its own subprocess call.
    return list_az_accounts()


# -----------------------------------------------------------------------------
# az login (background, self-timing-out)
# -----------------------------------------------------------------------------
def start_az_login(tenant_id: Optional[str] = None) -> str:
    """Kick off `az login` in a background thread; return a login_id to poll.

    The worker enforces its own timeout via proc.communicate(timeout=...). If
    the user closes the browser without finishing, the orphan az process is
    killed when the timeout fires.

    Only one login may be in flight at a time. A request to start a second
    one while another is running returns the existing login_id.
    """
    with _LOCK:
        # Reject if a login is already in flight (one-at-a-time, per rubber-duck).
        for lid, st in _LOGINS.items():
            if st.status == "running":
                log.info("start_az_login: reusing in-flight login_id=%s", lid)
                return lid
        login_id = uuid.uuid4().hex[:12]
        state = LoginState(login_id=login_id, tenant_id=tenant_id)
        _LOGINS[login_id] = state
        _gc_logins_locked()

    cmd = [_AZ_PATH, "login", "--allow-no-subscriptions"]
    if tenant_id:
        cmd += ["--tenant", tenant_id]

    def _worker():
        try:
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True,
                )
            except FileNotFoundError:
                with _LOCK:
                    state.status = "failed"
                    state.error = "Azure CLI ('az') not found on PATH."
                    state.finished_at = time.time()
                return
            with _LOCK:
                state.proc = proc
            try:
                _stdout, stderr = proc.communicate(timeout=LOGIN_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                # Worker enforces the timeout — no polling required.
                try:
                    proc.kill()
                    proc.communicate(timeout=5)
                except Exception:
                    pass
                with _LOCK:
                    if state.status == "running":
                        state.status = "timeout"
                        state.error = (
                            f"az login did not complete within "
                            f"{LOGIN_TIMEOUT_SECONDS}s. Did you finish the "
                            f"browser sign-in?"
                        )
                        state.finished_at = time.time()
                return

            with _LOCK:
                # cancel_login may have flipped status already.
                if state.status != "running":
                    state.finished_at = state.finished_at or time.time()
                    return
                if proc.returncode == 0:
                    state.status = "succeeded"
                else:
                    state.status = "failed"
                    state.error = (
                        (stderr or "").strip()
                        or f"az login exited {proc.returncode}"
                    )
                state.finished_at = time.time()
        finally:
            if state.status == "succeeded":
                state.accounts_after = list_az_accounts()

    t = threading.Thread(target=_worker, name=f"az-login-{login_id}", daemon=True)
    t.start()
    return login_id


def get_login_status(login_id: str) -> Optional[LoginState]:
    with _LOCK:
        st = _LOGINS.get(login_id)
        # Defensive copy: callers serialize via to_public_dict(); no need to
        # deep-copy the actual proc handle here.
        return st


def cancel_login(login_id: str) -> bool:
    """Best-effort: terminate the az login subprocess for this login_id."""
    with _LOCK:
        st = _LOGINS.get(login_id)
        if not st or st.status != "running":
            return False
        st.status = "cancelled"
        st.error = "cancelled by user"
        st.finished_at = time.time()
        proc = st.proc
    if proc is not None:
        try:
            proc.kill()
        except Exception as exc:
            log.warning("cancel_login %s: kill failed: %s", login_id, exc)
            return False
    return True


def _gc_logins_locked() -> None:
    """Drop finished login records older than the TTL. Caller must hold _LOCK."""
    cutoff = time.time() - LOGIN_RECORD_TTL_SECONDS
    drop = [
        lid for lid, st in _LOGINS.items()
        if st.status != "running" and (st.finished_at or 0) < cutoff
    ]
    for lid in drop:
        _LOGINS.pop(lid, None)


# -----------------------------------------------------------------------------
# Identity-guard: the load-bearing safety net
# -----------------------------------------------------------------------------
class IdentityMismatch(RuntimeError):
    """Raised when the live az context drifted from what the user confirmed."""


def assert_az_identity(
    expected_tenant_id: str,
    *,
    expected_user_name: Optional[str] = None,
    on_log: Optional[Callable[[str], None]] = None,
) -> dict:
    """Re-verify the live az context still matches what the user confirmed.

    Returns the live `az account show` dict on success. Raises
    IdentityMismatch if the tenant changed (and, if expected_user_name is
    given, if the user changed too).

    Per the rubber-duck critique, this MUST be invoked before every
    destructive `az` operation, not just once at preflight — otherwise a
    user could `az account set` in another terminal mid-run and shift the
    rest of the workflow into the wrong tenant.
    """
    ctx = get_active_az_context()
    if not ctx:
        raise IdentityMismatch(
            "Not signed in to az. Re-launch the onboarder after `az login`."
        )
    actual_tenant = ctx.get("tenantId")
    actual_user = (ctx.get("user") or {}).get("name")
    if expected_tenant_id and actual_tenant != expected_tenant_id:
        msg = (
            f"Active az tenant changed since you confirmed. "
            f"Expected {expected_tenant_id}, got {actual_tenant}. "
            f"Re-launch the onboarder to pick the right identity."
        )
        if on_log:
            on_log(f"[ABORT] {msg}")
        raise IdentityMismatch(msg)
    if expected_user_name and actual_user and actual_user != expected_user_name:
        msg = (
            f"Active az user changed since you confirmed. "
            f"Expected {expected_user_name}, got {actual_user}. "
            f"Re-launch the onboarder."
        )
        if on_log:
            on_log(f"[ABORT] {msg}")
        raise IdentityMismatch(msg)
    return ctx


# -----------------------------------------------------------------------------
# CSRF token (one per onboarder process launch)
# -----------------------------------------------------------------------------
_CSRF_TOKEN: Optional[str] = None


def csrf_token() -> str:
    """Return (and lazily mint) the per-launch CSRF token."""
    global _CSRF_TOKEN
    if _CSRF_TOKEN is None:
        _CSRF_TOKEN = secrets.token_urlsafe(32)
    return _CSRF_TOKEN

"""Azure ARM (management plane) helpers for the multi-vault picker.

Where ``keyvault.py`` owns the *data plane* (set_secret / get_secret against a
single vault we already know about), this module owns the *management plane*:

  * "who am I signed in as right now?"
  * "what subscriptions can I see?"
  * "what Key Vaults exist in subscription X that I can access?"
  * "create a new Key Vault in resource group Y at location Z."
  * "what's my object id, so I can grant myself Secrets Officer on the
     vault I just created?"

The form's vault picker calls into here through a small set of /api/azure/*
endpoints in onboarder.py. Azure is the source of truth for the vault list
- the onboarder keeps no local registry. This avoids drift, RBAC surprises,
and the "I added a vault then revoked my access in the portal" footgun.

Auth: DefaultAzureCredential. In practice that resolves to AzureCliCredential
(``az login``) on the user's workstation. The same identity that is used by
the generated agent at runtime to RESOLVE secrets is used here to MANAGE
vaults - so if it works in the picker, it will work at agent boot.

All list_* calls memoise into a tiny in-process LRU with a 60s TTL keyed by
(function, args). ARM list calls take 1-3s so this matters for UI snappiness.
The cache is invalidated on any auth error (best-effort) so a fresh ``az
login`` is picked up without restarting the onboarder.

The provisioning entry point ``create_vault`` is intentionally synchronous
from the caller's perspective: it kicks off the begin_create_or_update LRO
and ``.result()`` blocks until the vault is ready (~1-3 minutes). The HTTP
endpoint dispatches this on a thread.

This module never logs subscription secrets or user tokens. It does log
subscription IDs, vault names, and resource group names - those are not
secrets but rather routine cloud identifiers visible in any portal session.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import threading
import time
from typing import Any, Optional

log = logging.getLogger("azure_vaults")

# Resolve `az` to a full path once. On Windows, `subprocess.run(["az", ...])`
# without shell=True won't find az.cmd via PATHEXT, so we must hand it the
# full path that shutil.which() returns. Matches identity.py:_AZ_PATH.
_AZ_PATH = shutil.which("az") or "az"


# -----------------------------------------------------------------------------
# Vault-name validation (Azure constraint)
# -----------------------------------------------------------------------------
# https://learn.microsoft.com/azure/key-vault/general/about-keys-secrets-certificates#objects-identifiers-and-versioning
# Vault names: 3-24 chars, letters/digits/hyphens, must start with a letter,
# must end with letter or digit, no consecutive hyphens.
_VAULT_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9-]{1,22}[a-zA-Z0-9]$")


def validate_vault_name(name: str) -> tuple[bool, str]:
    """Return (ok, error_msg). Empty error_msg when ok=True."""
    if not name:
        return False, "Vault name is required."
    if len(name) < 3 or len(name) > 24:
        return False, "Vault name must be 3-24 characters."
    if not _VAULT_NAME_RE.match(name):
        return False, (
            "Vault name must start with a letter, end with a letter or digit, "
            "and contain only letters, digits, and hyphens."
        )
    if "--" in name:
        return False, "Vault name cannot contain consecutive hyphens."
    return True, ""


# -----------------------------------------------------------------------------
# Tiny TTL cache for list_* calls
# -----------------------------------------------------------------------------
_CACHE: dict[tuple, tuple[float, Any]] = {}
_CACHE_TTL = 60.0
_CACHE_LOCK = threading.Lock()


def _cache_get(key: tuple) -> Optional[Any]:
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if not entry:
            return None
        ts, value = entry
        if (time.monotonic() - ts) > _CACHE_TTL:
            _CACHE.pop(key, None)
            return None
        return value


def _cache_set(key: tuple, value: Any) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = (time.monotonic(), value)


def invalidate_cache() -> None:
    """Drop every cached ARM response. Called on auth error or user demand."""
    with _CACHE_LOCK:
        _CACHE.clear()


# -----------------------------------------------------------------------------
# Lazy SDK imports - keeps the module importable even if azure-mgmt-* isn't
# installed (the onboarder boots, and the API endpoints surface the import
# error cleanly to the UI instead of crashing at module load).
# -----------------------------------------------------------------------------
def _get_credential():
    try:
        from azure.identity import DefaultAzureCredential
    except ImportError as exc:
        raise RuntimeError(
            "azure-identity must be installed for the vault picker. "
            "Run: pip install azure-identity"
        ) from exc
    # Allow interactive browser fallback if `az login` token is bust.
    return DefaultAzureCredential(exclude_interactive_browser_credential=False)


def _get_mgmt_keyvault_client(subscription_id: str):
    try:
        from azure.mgmt.keyvault import KeyVaultManagementClient
    except ImportError as exc:
        raise RuntimeError(
            "azure-mgmt-keyvault must be installed for vault enumeration. "
            "Run: pip install azure-mgmt-keyvault"
        ) from exc
    return KeyVaultManagementClient(_get_credential(), subscription_id)


def _get_subscription_client():
    try:
        from azure.mgmt.subscription import SubscriptionClient
    except ImportError as exc:
        raise RuntimeError(
            "azure-mgmt-subscription must be installed for subscription "
            "enumeration. Run: pip install azure-mgmt-subscription"
        ) from exc
    return SubscriptionClient(_get_credential())


def _get_resource_client(subscription_id: str):
    try:
        from azure.mgmt.resource import ResourceManagementClient
    except ImportError as exc:
        raise RuntimeError(
            "azure-mgmt-resource must be installed for resource-group "
            "enumeration. Run: pip install azure-mgmt-resource"
        ) from exc
    return ResourceManagementClient(_get_credential(), subscription_id)


def _get_authorization_client(subscription_id: str):
    try:
        from azure.mgmt.authorization import AuthorizationManagementClient
    except ImportError as exc:
        raise RuntimeError(
            "azure-mgmt-authorization must be installed for role-assignment "
            "automation. Run: pip install azure-mgmt-authorization"
        ) from exc
    return AuthorizationManagementClient(_get_credential(), subscription_id)


# -----------------------------------------------------------------------------
# Whoami (uses az CLI - matches the existing identity probe path and avoids
# a second auth-scope round trip to MS Graph).
# -----------------------------------------------------------------------------
def whoami() -> dict:
    """Return the active az identity, or {} if not signed in.

    Shape (matches the keys used elsewhere in this codebase):
      {
        "user_name": "admin@M365DS419742.onmicrosoft.com",
        "tenant_id": "23affcf0-...",
        "tenant_default_domain": "M365DS419742.onmicrosoft.com",
        "subscription_id": "...",
        "subscription_name": "...",
        "object_id": "...",   # signed-in user's directory object id
      }
    """
    cached = _cache_get(("whoami",))
    if cached is not None:
        return cached

    try:
        out = subprocess.check_output(
            [_AZ_PATH, "account", "show", "-o", "json"],
            stderr=subprocess.PIPE,
            timeout=15,
            shell=False,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        log.warning("whoami: az account show failed: %s", exc)
        return {}

    try:
        acct = json.loads(out)
    except json.JSONDecodeError:
        return {}

    info = {
        "user_name": (acct.get("user") or {}).get("name") or "",
        "tenant_id": acct.get("tenantId") or "",
        "tenant_default_domain": acct.get("tenantDefaultDomain") or "",
        "subscription_id": acct.get("id") or "",
        "subscription_name": acct.get("name") or "",
        "object_id": "",
    }

    # Try to enrich with the signed-in user's directory object id (needed for
    # post-provision role assignment). Best-effort - service principal logins
    # won't have an aad user object so this can return empty.
    try:
        oid = subprocess.check_output(
            [_AZ_PATH, "ad", "signed-in-user", "show", "--query", "id", "-o", "tsv"],
            stderr=subprocess.PIPE,
            timeout=15,
            shell=False,
        ).decode("utf-8").strip()
        if oid:
            info["object_id"] = oid
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        log.info("whoami: signed-in-user show failed (non-fatal): %s", exc)

    _cache_set(("whoami",), info)
    return info


# -----------------------------------------------------------------------------
# Subscription list
# -----------------------------------------------------------------------------
def list_subscriptions() -> list[dict]:
    """Return every subscription the current identity can see.

    [{"id", "name", "tenant_id", "state"}]

    The first entry is the subscription the user is currently "in" (matches
    ``az account show``) so the UI can sensibly pre-select it.
    """
    cached = _cache_get(("subs",))
    if cached is not None:
        return cached

    client = _get_subscription_client()
    items: list[dict] = []
    try:
        for sub in client.subscriptions.list():
            # Tenant id is NOT a field on the Subscription model in
            # azure-mgmt-subscription 3.x - it's on TenantIdDescription from
            # client.tenants.list(). Since this onboarder runs against the
            # currently-logged-in tenant only, we leave tenant_id empty here
            # and the caller (UI) reads the tenant from whoami() once.
            items.append({
                "id": getattr(sub, "subscription_id", "") or "",
                "name": getattr(sub, "display_name", "") or getattr(sub, "subscription_id", "") or "",
                "tenant_id": getattr(sub, "tenant_id", "") or "",
                "state": (getattr(sub, "state", "") or "").lower(),
            })
    except Exception as exc:
        invalidate_cache()
        log.warning("list_subscriptions failed: %s", exc)
        raise

    # Prefer the active az subscription first
    current = whoami().get("subscription_id")
    if current:
        items.sort(key=lambda s: 0 if s["id"] == current else 1)
    _cache_set(("subs",), items)
    return items


# -----------------------------------------------------------------------------
# Vault list (per subscription)
# -----------------------------------------------------------------------------
def list_vaults(subscription_id: str) -> list[dict]:
    """List every Key Vault in a subscription that the current identity can see.

    [{"name", "id", "location", "resource_group", "subscription_id"}]

    The id is the fully-qualified Azure resource ID, useful for RBAC
    role-assignment scopes.
    """
    if not subscription_id:
        raise ValueError("subscription_id is required")

    cached = _cache_get(("vaults", subscription_id))
    if cached is not None:
        return cached

    client = _get_mgmt_keyvault_client(subscription_id)
    items: list[dict] = []
    try:
        for v in client.vaults.list_by_subscription():
            # v.id looks like:
            # /subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.KeyVault/vaults/<name>
            rg = ""
            if v.id:
                m = re.search(r"/resourceGroups/([^/]+)/", v.id, re.IGNORECASE)
                if m:
                    rg = m.group(1)
            items.append({
                "name": v.name,
                "id": v.id,
                "location": v.location or "",
                "resource_group": rg,
                "subscription_id": subscription_id,
            })
    except Exception as exc:
        invalidate_cache()
        log.warning("list_vaults(%s) failed: %s", subscription_id, exc)
        raise

    items.sort(key=lambda x: (x["name"] or "").lower())
    _cache_set(("vaults", subscription_id), items)
    return items


# -----------------------------------------------------------------------------
# Resource group list (for the Provision-New form)
# -----------------------------------------------------------------------------
def list_resource_groups(subscription_id: str) -> list[dict]:
    """List resource groups in a subscription. [{"name", "location"}]."""
    if not subscription_id:
        raise ValueError("subscription_id is required")
    cached = _cache_get(("rgs", subscription_id))
    if cached is not None:
        return cached
    client = _get_resource_client(subscription_id)
    items: list[dict] = []
    try:
        for rg in client.resource_groups.list():
            items.append({"name": rg.name, "location": rg.location or ""})
    except Exception as exc:
        invalidate_cache()
        log.warning("list_resource_groups(%s) failed: %s", subscription_id, exc)
        raise
    items.sort(key=lambda x: (x["name"] or "").lower())
    _cache_set(("rgs", subscription_id), items)
    return items


def list_locations(subscription_id: str) -> list[dict]:
    """List Azure regions available to a subscription. [{"name", "display_name"}]."""
    if not subscription_id:
        raise ValueError("subscription_id is required")
    cached = _cache_get(("locs", subscription_id))
    if cached is not None:
        return cached
    client = _get_subscription_client()
    items: list[dict] = []
    try:
        for loc in client.subscriptions.list_locations(subscription_id):
            # Skip extended/edge locations - keep the picker compact for the
            # 99% case of standard public-cloud regions.
            if (getattr(loc, "metadata", None) and
                    getattr(loc.metadata, "region_type", "") == "EdgeZone"):
                continue
            items.append({
                "name": loc.name,
                "display_name": loc.display_name or loc.name,
            })
    except Exception as exc:
        invalidate_cache()
        log.warning("list_locations(%s) failed: %s", subscription_id, exc)
        raise
    items.sort(key=lambda x: (x["display_name"] or "").lower())
    _cache_set(("locs", subscription_id), items)
    return items


# -----------------------------------------------------------------------------
# Vault provisioning + post-create role assignment
# -----------------------------------------------------------------------------
# Built-in role: "Key Vault Secrets Officer"
# https://learn.microsoft.com/azure/role-based-access-control/built-in-roles#key-vault-secrets-officer
_SECRETS_OFFICER_ROLE_ID = "b86a8fe4-44ce-4948-aee5-eccb2c155cd7"


def create_vault(
    *,
    subscription_id: str,
    resource_group: str,
    vault_name: str,
    location: str,
    tenant_id: str,
) -> dict:
    """Provision a new Key Vault in RBAC mode, then attempt to grant the
    current signed-in user 'Key Vault Secrets Officer' on it.

    Returns a result dict:
      {
        "ok": True,
        "vault": {"name", "id", "location", "resource_group", "subscription_id"},
        "role_assignment": {
          "attempted": True,
          "ok": True|False,
          "role_id": "...",
          "principal_id": "...",
          "error": "..."  # only if ok=False
        }
      }

    Raises RuntimeError on provision failure. Role-assignment failure is
    NON-FATAL: returned as ok=False in the role_assignment block so the UI
    can show actionable manual steps without rolling back the vault.
    """
    ok, err = validate_vault_name(vault_name)
    if not ok:
        raise ValueError(err)
    if not (subscription_id and resource_group and location and tenant_id):
        raise ValueError("subscription_id, resource_group, location, tenant_id are all required")

    try:
        from azure.mgmt.keyvault.models import (
            VaultCreateOrUpdateParameters, VaultProperties, Sku, SkuName,
        )
    except ImportError as exc:
        raise RuntimeError(
            "azure-mgmt-keyvault must be installed. Run: pip install azure-mgmt-keyvault"
        ) from exc

    log.info(
        "create_vault: provisioning %s in rg=%s sub=%s loc=%s tenant=%s",
        vault_name, resource_group, subscription_id, location, tenant_id,
    )

    client = _get_mgmt_keyvault_client(subscription_id)
    params = VaultCreateOrUpdateParameters(
        location=location,
        properties=VaultProperties(
            tenant_id=tenant_id,
            sku=Sku(name=SkuName.standard, family="A"),
            enable_rbac_authorization=True,
            enable_soft_delete=True,
            soft_delete_retention_in_days=90,
            access_policies=[],  # empty under RBAC mode
        ),
    )
    try:
        poller = client.vaults.begin_create_or_update(
            resource_group_name=resource_group,
            vault_name=vault_name,
            parameters=params,
        )
        vault = poller.result()  # blocks until LRO completes (1-3 min)
    except Exception as exc:
        log.warning("create_vault: provisioning failed: %s", exc)
        raise RuntimeError(
            f"Failed to provision vault {vault_name!r}: {type(exc).__name__}: {exc}"
        ) from exc

    invalidate_cache()  # the new vault should appear on next list

    out: dict = {
        "ok": True,
        "vault": {
            "name": vault.name,
            "id": vault.id,
            "location": vault.location or location,
            "resource_group": resource_group,
            "subscription_id": subscription_id,
        },
        "role_assignment": {"attempted": False, "ok": False},
    }

    # Best-effort: grant the signed-in user Secrets Officer so they can
    # immediately write to it from the data plane. If the user lacks the
    # privilege to make role assignments (User Access Admin or Owner), this
    # WILL fail - surface a clear actionable error rather than rolling back.
    principal_id = whoami().get("object_id")
    if not principal_id:
        out["role_assignment"] = {
            "attempted": False,
            "ok": False,
            "error": (
                "Could not determine your directory object id "
                "(`az ad signed-in-user show` failed). The vault was created "
                "but you'll need to grant yourself 'Key Vault Secrets Officer' "
                "manually before the onboarder can write secrets to it."
            ),
        }
        return out

    out["role_assignment"] = _assign_role(
        subscription_id=subscription_id,
        scope=vault.id,
        principal_id=principal_id,
        role_id=_SECRETS_OFFICER_ROLE_ID,
    )
    return out


def _assign_role(
    *,
    subscription_id: str,
    scope: str,
    principal_id: str,
    role_id: str,
) -> dict:
    """Attempt to create a role assignment. Returns an outcome dict; never raises."""
    import uuid
    try:
        from azure.mgmt.authorization.models import RoleAssignmentCreateParameters
    except ImportError as exc:
        return {
            "attempted": False, "ok": False, "role_id": role_id,
            "principal_id": principal_id,
            "error": f"azure-mgmt-authorization missing: {exc}",
        }

    client = _get_authorization_client(subscription_id)
    role_def_id = (
        f"/subscriptions/{subscription_id}/providers/Microsoft.Authorization/"
        f"roleDefinitions/{role_id}"
    )
    assignment_name = str(uuid.uuid4())
    try:
        client.role_assignments.create(
            scope=scope,
            role_assignment_name=assignment_name,
            parameters=RoleAssignmentCreateParameters(
                role_definition_id=role_def_id,
                principal_id=principal_id,
                principal_type="User",
            ),
        )
    except Exception as exc:
        msg = str(exc)
        log.warning("_assign_role failed for principal=%s scope=%s: %s",
                    principal_id, scope, msg[:300])
        # Most common cause: caller lacks Microsoft.Authorization/roleAssignments/write
        return {
            "attempted": True, "ok": False, "role_id": role_id,
            "principal_id": principal_id,
            "error": (
                "Vault was created, but the onboarder could not auto-grant you "
                "'Key Vault Secrets Officer'. Most likely you don't have "
                "'User Access Administrator' or 'Owner' on the resource group. "
                "Ask an admin to run:\n"
                f"  az role assignment create --assignee {principal_id} "
                f"--role 'Key Vault Secrets Officer' --scope {scope}\n"
                f"Underlying error: {type(exc).__name__}: {msg[:300]}"
            ),
        }

    log.info("_assign_role: granted role=%s to principal=%s scope=%s",
             role_id, principal_id, scope)
    return {
        "attempted": True, "ok": True, "role_id": role_id,
        "principal_id": principal_id,
    }


# -----------------------------------------------------------------------------
# Capability probe (data plane) - "can I actually write to this vault?"
# -----------------------------------------------------------------------------
def probe_vault(vault_name: str) -> dict:
    """Quick connectivity + capability probe for a vault.

    Tries: set_secret -> get_secret -> begin_delete_secret (best-effort).
    Returns:
      {
        "ok": True|False,
        "can_write": True|False,
        "can_read": True|False,
        "can_delete": True|False,
        "error_code": "ok"|"auth"|"forbidden_write"|"network"|"unknown",
        "error_msg": "..."  # human-readable, empty when ok
      }
    """
    import uuid
    name_ok, name_err = validate_vault_name(vault_name)
    if not name_ok:
        return {
            "ok": False, "can_write": False, "can_read": False, "can_delete": False,
            "error_code": "name", "error_msg": name_err,
        }

    try:
        from azure.keyvault.secrets import SecretClient
        from azure.core.exceptions import (
            ClientAuthenticationError, HttpResponseError, ResourceNotFoundError,
        )
    except ImportError as exc:
        return {
            "ok": False, "can_write": False, "can_read": False, "can_delete": False,
            "error_code": "deps", "error_msg": f"azure-keyvault-secrets missing: {exc}",
        }

    url = f"https://{vault_name}.vault.azure.net/"
    cred = _get_credential()
    client = SecretClient(vault_url=url, credential=cred)

    probe_name = f"onboarder-probe-{uuid.uuid4().hex[:8]}"
    out = {
        "ok": False, "can_write": False, "can_read": False, "can_delete": False,
        "error_code": "unknown", "error_msg": "",
    }

    # 1. WRITE
    try:
        client.set_secret(probe_name, "ping", content_type="text/plain", tags={
            "created_by": "agent-sdk-onboarder",
            "purpose": "vault-picker-probe",
        })
        out["can_write"] = True
    except ClientAuthenticationError as exc:
        invalidate_cache()
        out["error_code"] = "auth"
        out["error_msg"] = (
            f"Not signed in to Azure (or token expired). Run `az login` and retry. "
            f"({type(exc).__name__})"
        )
        return out
    except HttpResponseError as exc:
        if exc.status_code in (401, 403):
            out["error_code"] = "forbidden_write"
            out["error_msg"] = (
                f"You can reach vault {vault_name!r}, but you do NOT have permission "
                f"to write secrets to it. You need 'Key Vault Secrets Officer' (or "
                f"equivalent set/list/delete via access policies). HTTP {exc.status_code}."
            )
        elif exc.status_code in (404,):
            out["error_code"] = "not_found"
            out["error_msg"] = f"Vault {vault_name!r} not found at {url}."
        else:
            out["error_code"] = "network"
            out["error_msg"] = f"{type(exc).__name__}: {exc}"
        return out
    except Exception as exc:
        out["error_code"] = "network"
        out["error_msg"] = f"{type(exc).__name__}: {exc}"
        return out

    # 2. READ
    try:
        got = client.get_secret(probe_name)
        out["can_read"] = bool(got and got.value)
    except HttpResponseError as exc:
        out["error_code"] = "forbidden_read"
        out["error_msg"] = (
            f"Wrote the probe secret but couldn't read it back. You need "
            f"'get' permission too. HTTP {exc.status_code}."
        )
        # Don't return - still try cleanup
    except Exception as exc:
        out["error_msg"] = f"read-back failed: {type(exc).__name__}: {exc}"

    # 3. DELETE (best-effort cleanup; not blocking)
    try:
        client.begin_delete_secret(probe_name)
        out["can_delete"] = True
    except Exception as exc:
        log.info("probe_vault: cleanup delete failed (non-fatal): %s", exc)

    if out["can_write"] and out["can_read"]:
        out["ok"] = True
        out["error_code"] = "ok"
    return out

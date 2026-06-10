"""Azure Key Vault integration for the onboarder.

This module owns the WRITE side of the @KV: reference scheme:
    .env line:  PURVIEW_CLIENT_SECRET=@KV:SDKOnboarder/bedrock-agent-purview-client-secret
                                        ^^^^^^^^^^^^ vault name
                                                     ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^ secret name

At onboarding time, the onboarder takes the literal secret values typed into
the form (Purview client secret, AWS keys, LLM API keys) and pushes them to
the vault using DefaultAzureCredential. The .env that lands on disk only
ever contains the @KV: reference - never the literal value.

At agent runtime, the generated agent's own keyvault.py (rendered from the
template at codegen_templates/keyvault.py.tmpl) resolves the @KV: references
back to live secrets - in-memory only, never written to disk.

Architectural decisions (validated by rubber-duck review):
  * KV mode is MANDATORY when AGENT_KV_VAULT_NAME is set. The onboarder
    blocks new submissions if the env var is missing (no silent fallback
    to writing literal secrets to disk).
  * Explicit env-var -> KV-name suffix mapping. We never derive KV secret
    names from internal template-variable identifiers, because some of those
    differ from the actual .env key (e.g. template var CLIENT_SECRET maps to
    .env var PURVIEW_CLIENT_SECRET).
  * Long-slug guard: KV secret names cap at 127 chars, so we truncate +
    append a deterministic 8-char hash when needed.
  * This module never logs the literal secret value - only the secret NAME.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from typing import Optional

log = logging.getLogger("keyvault")

# Reference prefix used inside .env values. Resolution is keyed off this.
KV_REF_PREFIX = "@KV:"

# Maximum length of an Azure Key Vault secret name.
# (KV regex: ^[0-9a-zA-Z-]{1,127}$)
_KV_NAME_MAX = 127
_KV_NAME_RE = re.compile(r"^[0-9a-zA-Z-]{1,127}$")


# -----------------------------------------------------------------------------
# Mapping: template-variable key -> KV secret-name SUFFIX
# -----------------------------------------------------------------------------
# Why explicit rather than derived: the template variable name in
# codegen._build_vars() is sometimes different from the .env key it
# eventually emits. For example, the template var CLIENT_SECRET is
# rendered into .env as PURVIEW_CLIENT_SECRET, and BEDROCK_SECRET_ACCESS_KEY
# is rendered as AWS_SECRET_ACCESS_KEY. The KV name should reflect the
# .env key (which is what an operator browsing the vault would expect),
# not the internal template identifier.
#
# Format: each KV secret name will be: f"{slug}-{suffix}"
# -----------------------------------------------------------------------------
SECRET_KEY_MAP: dict[str, str] = {
    # Microsoft Entra / Purview SPN
    "CLIENT_SECRET":              "purview-client-secret",
    # Azure OpenAI
    "AZURE_OPENAI_API_KEY":       "azure-openai-api-key",
    # OpenAI public
    "OPENAI_API_KEY":             "openai-api-key",
    # AWS Bedrock - both halves of the AWS credential pair are sensitive
    "BEDROCK_ACCESS_KEY_ID":      "aws-access-key-id",
    "BEDROCK_SECRET_ACCESS_KEY":  "aws-secret-access-key",
    "BEDROCK_SESSION_TOKEN":      "aws-session-token",
    # Anthropic Claude
    "ANTHROPIC_API_KEY":          "anthropic-api-key",
    # Custom HTTP - the auth header may carry a bearer token / basic auth /
    # api-key style header. Whole-value is the simplest correct treatment.
    "HTTP_AUTH_HEADER":           "http-auth-header",
}


# -----------------------------------------------------------------------------
# Public helpers
# -----------------------------------------------------------------------------
def vault_name() -> Optional[str]:
    """Return the configured vault name from AGENT_KV_VAULT_NAME, or None.

    The vault URL is derived from the name as f"https://{name}.vault.azure.net/"
    which is the standard public-cloud pattern.
    """
    raw = (os.environ.get("AGENT_KV_VAULT_NAME") or "").strip()
    return raw or None


def is_kv_configured() -> bool:
    """True when KV mode is on and onboarding can write secrets to KV."""
    return vault_name() is not None


def vault_url(name: Optional[str] = None) -> str:
    """Build the data-plane URL for a vault name (public cloud)."""
    n = (name or vault_name() or "").strip()
    if not n:
        raise RuntimeError(
            "AGENT_KV_VAULT_NAME is not set. The onboarder requires a Key "
            "Vault to be configured before any new agent can be created. "
            "Set AGENT_KV_VAULT_NAME=<vault-name> and restart."
        )
    return f"https://{n}.vault.azure.net/"


def format_ref(vault: str, secret_name: str) -> str:
    """Return the @KV: reference string to embed in .env."""
    return f"{KV_REF_PREFIX}{vault}/{secret_name}"


def is_ref(value: str) -> bool:
    """True if `value` is a @KV: reference (not a literal secret)."""
    return isinstance(value, str) and value.startswith(KV_REF_PREFIX)


def secret_name_for(slug: str, env_key: str) -> str:
    """Build a Key-Vault secret name for (slug, env_key).

    Convention: f"{slug}-{SECRET_KEY_MAP[env_key]}"

    Falls back to a sanitized lower-dash version of env_key if it's not in
    the mapping (defensive - shouldn't happen for keys we vault).

    Length guard: KV secret names cap at 127 chars. If the combined name
    exceeds 127, we truncate the slug to leave room and append an 8-char
    SHA-1 prefix of the original slug to keep names unique and deterministic.
    """
    if not slug:
        raise ValueError("slug must be non-empty")

    suffix = SECRET_KEY_MAP.get(env_key)
    if not suffix:
        # Defensive fallback (lowercased, underscores -> dashes)
        suffix = env_key.lower().replace("_", "-")

    name = f"{slug}-{suffix}"
    if len(name) <= _KV_NAME_MAX:
        if not _KV_NAME_RE.match(name):
            raise ValueError(f"Computed KV secret name is not valid: {name!r}")
        return name

    # Truncate slug to leave room for: "-" + suffix + "-" + 8-char hash
    digest = hashlib.sha1(slug.encode("utf-8")).hexdigest()[:8]
    overhead = len(suffix) + len(digest) + 2  # 2 dashes
    keep = _KV_NAME_MAX - overhead
    if keep < 1:
        # suffix alone is too long; truncate suffix as well
        keep_suffix = _KV_NAME_MAX - len(digest) - 2
        truncated = f"{digest}-{suffix[:keep_suffix]}"
    else:
        truncated = f"{slug[:keep]}-{suffix}-{digest}"
    if not _KV_NAME_RE.match(truncated):
        raise ValueError(f"Truncated KV secret name is not valid: {truncated!r}")
    return truncated


# -----------------------------------------------------------------------------
# Live KV writes
# -----------------------------------------------------------------------------
# The SDK import is deferred so the onboarder can boot even when the deps
# haven't been installed yet (clearer error than ImportError at import time).
def _get_client(vault: Optional[str] = None):
    try:
        from azure.identity import DefaultAzureCredential
        from azure.keyvault.secrets import SecretClient
    except ImportError as exc:
        raise RuntimeError(
            "azure-keyvault-secrets and azure-identity must be installed for "
            "KV mode. Run: pip install azure-keyvault-secrets azure-identity"
        ) from exc
    url = vault_url(vault)
    cred = DefaultAzureCredential(exclude_interactive_browser_credential=False)
    return SecretClient(vault_url=url, credential=cred)


def put_secret(secret_name: str, value: str, *, vault: Optional[str] = None) -> str:
    """Push a secret to Key Vault and return the @KV: reference string.

    Raises RuntimeError on any failure (caller fails the onboarding run).
    Never logs the literal value, only the secret NAME.
    """
    if not value:
        raise ValueError("Refusing to put_secret with empty value")
    if not _KV_NAME_RE.match(secret_name):
        raise ValueError(f"Invalid KV secret name: {secret_name!r}")
    v = vault or vault_name()
    if not v:
        raise RuntimeError(
            "Cannot put_secret: AGENT_KV_VAULT_NAME is not set. The onboarder "
            "must be launched with a configured vault."
        )
    client = _get_client(v)
    try:
        client.set_secret(secret_name, value)
    except Exception as exc:
        raise RuntimeError(
            f"Failed to write secret {secret_name!r} to vault {v!r}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    log.info("wrote secret %s to vault %s", secret_name, v)
    return format_ref(v, secret_name)


def best_effort_delete(secret_name: str, *, vault: Optional[str] = None) -> bool:
    """Best-effort cleanup helper - used when an onboarding run fails partway.

    Returns True if delete succeeded, False otherwise. Never raises.
    """
    v = vault or vault_name()
    if not v:
        return False
    try:
        client = _get_client(v)
        client.begin_delete_secret(secret_name)
    except Exception as exc:
        log.warning("best-effort delete of secret %s failed: %s", secret_name, exc)
        return False
    log.info("best-effort deleted secret %s from vault %s", secret_name, v)
    return True

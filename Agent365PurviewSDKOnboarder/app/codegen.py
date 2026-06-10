"""Render the per-agent wrapper project from templates.

Templates live in ./codegen_templates/ and use a small `${var}` substitution
scheme (we deliberately avoid Jinja2 here because the wrapper code itself
contains Jinja-like `{...}` tokens that would conflict).
"""
from __future__ import annotations

import keyword
import logging
import re
import secrets
import string
from pathlib import Path
from typing import Any, Callable

import keyvault as kv

log = logging.getLogger("codegen")

TEMPLATE_DIR = Path(__file__).resolve().parent / "codegen_templates"

# The wrapper output name is computed per-run (v1.3): `<safe_module>_wrapper.py`.
# The other templates emit to fixed filenames.
COMMON_TEMPLATES: list[tuple[str, str]] = [
    ("server.py.tmpl", "server.py"),
    # wrapper.py.tmpl is handled separately so we can name it after the agent
    ("agent_observability.py.tmpl", "agent_observability.py"),
    ("keyvault.py.tmpl", "keyvault.py"),
    ("env.tmpl", ".env"),
    ("requirements.txt.tmpl", "requirements.txt"),
    ("README.md.tmpl", "README.md"),
    (".gitignore.tmpl", ".gitignore"),
]
WRAPPER_TEMPLATE = "wrapper.py.tmpl"

PROVIDER_ADAPTER: dict[str, str] = {
    "vertex":      "adapter_vertex.py.tmpl",
    "vertex_ai":   "adapter_vertex.py.tmpl",
    "azure_openai":"adapter_azure_openai.py.tmpl",
    "openai":      "adapter_openai.py.tmpl",
    "bedrock":     "adapter_bedrock.py.tmpl",
    "aws_bedrock": "adapter_bedrock.py.tmpl",
    "anthropic":   "adapter_anthropic.py.tmpl",
    "claude":      "adapter_anthropic.py.tmpl",
    "http":        "adapter_http.py.tmpl",
    "custom":      "adapter_http.py.tmpl",
}

# -----------------------------------------------------------------------------
# Provider branding for the generated chat UI (v1.7.7 aesthetic refresh).
# Each entry yields one inline SVG that fits into a 44x44 .agent-icon container
# (we render at 28x28 and let the container's gradient act as a chip backdrop).
# Kept as flat strings (no ${...}) so the template renderer never recurses.
# -----------------------------------------------------------------------------
_PROVIDER_LABELS: dict[str, str] = {
    "vertex":       "Google Vertex AI",
    "vertex_ai":    "Google Vertex AI",
    "azure_openai": "Azure OpenAI",
    "openai":       "OpenAI",
    "bedrock":      "AWS Bedrock",
    "aws_bedrock":  "AWS Bedrock",
    "anthropic":    "Anthropic Claude",
    "claude":       "Anthropic Claude",
    "http":         "Custom HTTP",
    "custom":       "Custom HTTP",
}

_PROVIDER_LOGOS: dict[str, str] = {
    "vertex": (
        "<svg viewBox='0 0 24 24' xmlns='http://www.w3.org/2000/svg' width='28' height='28'>"
        "<path fill='#4285F4' d='M22.5 12.27c0-.78-.07-1.53-.2-2.27H12v4.51h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.22-4.74 3.22-8.32z'/>"
        "<path fill='#34A853' d='M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.15-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z'/>"
        "<path fill='#FBBC05' d='M5.85 14.1c-.22-.66-.35-1.36-.35-2.1s.13-1.44.35-2.1V7.06H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.94l3.67-2.84z'/>"
        "<path fill='#EA4335' d='M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.06l3.67 2.84C6.71 7.31 9.14 5.38 12 5.38z'/>"
        "</svg>"
    ),
    "azure_openai": (
        "<svg viewBox='0 0 24 24' xmlns='http://www.w3.org/2000/svg' width='28' height='28'>"
        "<defs><linearGradient id='_pazg' x1='0' y1='0' x2='1' y2='1'>"
        "<stop offset='0%' stop-color='#FFFFFF'/><stop offset='100%' stop-color='#E6F1FB'/>"
        "</linearGradient></defs>"
        "<path fill='url(#_pazg)' d='M9.1 3.5h6L23 21h-7.4l-1-2.8H9l-1.4 2.8H1L9.1 3.5zm.6 11.8h4.6L12 8 9.7 15.3z'/>"
        "</svg>"
    ),
    "openai": (
        "<svg viewBox='0 0 24 24' xmlns='http://www.w3.org/2000/svg' width='28' height='28'>"
        "<path fill='#FFFFFF' d='M22.28 9.82a5.99 5.99 0 0 0-.52-4.91A6.05 6.05 0 0 0 15.26 2 6.07 6.07 0 0 0 4.98 4.18a5.98 5.98 0 0 0-4 2.9 6.05 6.05 0 0 0 .74 7.1A5.98 5.98 0 0 0 2.24 19.1a6.05 6.05 0 0 0 6.51 2.9A5.98 5.98 0 0 0 13.26 24a6.06 6.06 0 0 0 5.77-4.21 5.99 5.99 0 0 0 4-2.9 6.06 6.06 0 0 0-.75-7.07zm-9.03 12.61c-1.06 0-2.08-.37-2.88-1.04l.14-.08 4.78-2.76a.79.79 0 0 0 .4-.68v-6.74l2.02 1.17a.07.07 0 0 1 .04.05v5.58a4.5 4.5 0 0 1-4.5 4.5zM3.6 18.3a4.47 4.47 0 0 1-.53-3.02l.14.09 4.78 2.76a.77.77 0 0 0 .78 0l5.85-3.37v2.33a.08.08 0 0 1-.03.06l-4.83 2.79A4.5 4.5 0 0 1 3.6 18.3zM2.34 7.9a4.49 4.49 0 0 1 2.37-1.98v5.7c0 .28.15.54.39.68l5.81 3.36-2.02 1.17a.08.08 0 0 1-.07 0L3.99 14.04A4.5 4.5 0 0 1 2.34 7.87zm16.6 3.86l-5.84-3.39 2.02-1.16a.08.08 0 0 1 .07 0l4.83 2.79a4.49 4.49 0 0 1-.68 8.11v-5.68a.79.79 0 0 0-.4-.67zm2.01-3.03l-.14-.08-4.78-2.78a.78.78 0 0 0-.78 0L9.41 9.24V6.9a.07.07 0 0 1 .03-.06l4.83-2.79a4.5 4.5 0 0 1 6.68 4.66zm-12.64 4.14l-2.02-1.17a.08.08 0 0 1-.04-.05V6.07a4.5 4.5 0 0 1 7.37-3.45l-.14.08-4.78 2.76a.79.79 0 0 0-.4.68zm1.1-2.37l2.6-1.5 2.61 1.5v3l-2.6 1.5-2.61-1.5z'/>"
        "</svg>"
    ),
    "bedrock": (
        "<svg viewBox='0 0 24 24' xmlns='http://www.w3.org/2000/svg' width='28' height='28'>"
        "<text x='12' y='11.5' text-anchor='middle' fill='#FFFFFF' "
        "font-family='Arial, sans-serif' font-size='7' font-weight='900' letter-spacing='-0.3'>aws</text>"
        "<path d='M3.5 15.3 Q12 19.8 20.5 15.3' stroke='#FF9900' stroke-width='1.7' fill='none' stroke-linecap='round'/>"
        "<path d='M18.2 14.4 L20.5 15.3 L19.7 17.6' stroke='#FF9900' stroke-width='1.7' fill='none' stroke-linecap='round' stroke-linejoin='round'/>"
        "</svg>"
    ),
    "anthropic": (
        "<svg viewBox='0 0 24 24' xmlns='http://www.w3.org/2000/svg' width='28' height='28'>"
        "<path fill='#FFFFFF' d='M13.5 4h-2.9L5.5 20h3l1-3h5l1 3h3L13.5 4zm-2.8 9 1.5-4.8L13.7 13h-3z'/>"
        "</svg>"
    ),
    "custom": (
        "<svg viewBox='0 0 24 24' xmlns='http://www.w3.org/2000/svg' width='28' height='28'>"
        "<path fill='#FFFFFF' d='M9.1 7.6 5 12l4.1 4.4 1.4-1.3L7.8 12l2.7-3.1zm5.8 0-1.4 1.3L16.2 12l-2.7 3.1 1.4 1.3L19 12z'/>"
        "</svg>"
    ),
}
# Aliases (same logo as the canonical key).
_PROVIDER_LOGOS["vertex_ai"]   = _PROVIDER_LOGOS["vertex"]
_PROVIDER_LOGOS["aws_bedrock"] = _PROVIDER_LOGOS["bedrock"]
_PROVIDER_LOGOS["claude"]      = _PROVIDER_LOGOS["anthropic"]
_PROVIDER_LOGOS["http"]        = _PROVIDER_LOGOS["custom"]


# -----------------------------------------------------------------------------
# Provider config normalization (used by both onboarder /submit and codegen)
# -----------------------------------------------------------------------------
GCP_DEFAULT_LOCATION = "us-central1"


def normalize_gcp_location(loc: str | None) -> str:
    """Trim/normalize a GCP location string.

    * Strips whitespace and lowercases.
    * Empty -> default `us-central1`.
    * Collapses a stray hyphen before a trailing digit (`us-central-1` ->
      `us-central1`, `europe-west-4` -> `europe-west4`) -- the most common
      typo we see in form submissions.
    """
    s = (loc or "").strip().lower()
    if not s:
        return GCP_DEFAULT_LOCATION
    return re.sub(r"-(\d+)$", r"\1", s)


def _safe_module_name(slug: str) -> str:
    """Convert a slug into a valid Python module identifier.

    Rules:
      * `-` -> `_`
      * strip anything outside [a-z0-9_]
      * if empty or starts with a digit, prefix `agent_`
      * if it collides with a Python keyword, prefix `agent_`
    """
    s = slug.strip().lower()
    s = re.sub(r"[^a-z0-9_-]+", "", s).replace("-", "_")
    s = re.sub(r"_+", "_", s).strip("_")
    if not s or s[0].isdigit() or keyword.iskeyword(s):
        s = f"agent_{s}" if s else "agent"
    # Final safety net
    if not s.isidentifier():
        s = "agent_" + re.sub(r"[^a-z0-9_]", "", s)
    return s


def _safe_class_name(slug: str) -> str:
    """Convert a slug into a PascalCase Python class identifier ending in `Agent`."""
    s = re.sub(r"[^a-zA-Z0-9-]+", "", slug.strip())
    parts = [p for p in re.split(r"[-_]+", s) if p]
    pascal = "".join(p[:1].upper() + p[1:] for p in parts)
    if not pascal or pascal[0].isdigit():
        pascal = "Agent" + pascal
    cls = pascal + "Agent"
    if keyword.iskeyword(cls):
        cls = "Agent" + cls
    if not cls.isidentifier():
        cls = "Agent" + re.sub(r"[^A-Za-z0-9]", "", cls)
    return cls


def _render(text: str, vars_: dict[str, Any]) -> str:
    """Substitute ${name} placeholders with str(vars_[name]).

    Unknown placeholders are left intact (so a code template can keep things
    like Python format strings or ${} from bash unchanged if needed).
    """
    out = []
    i = 0
    while i < len(text):
        # find next ${
        if text[i:i+2] == "${":
            j = text.find("}", i + 2)
            if j == -1:
                out.append(text[i])
                i += 1
                continue
            key = text[i+2:j]
            if key in vars_:
                out.append(str(vars_[key]))
                i = j + 1
                continue
            # unknown -> keep as-is
            out.append(text[i:j+1])
            i = j + 1
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _build_vars(cfg: dict, entra: dict) -> dict[str, Any]:
    """Build the variable bag passed to every template."""
    p = cfg["provider"].lower()
    pc = cfg["provider_config"]
    session_secret = "".join(
        secrets.choice(string.ascii_letters + string.digits) for _ in range(48)
    )
    display_name = cfg["agent_display_name"]
    slug = cfg["agent_slug"]
    description = cfg["agent_description"] or display_name
    safe_module = _safe_module_name(slug)
    safe_class = _safe_class_name(slug)
    wrapper_module = f"{safe_module}_wrapper"

    def _docsafe(text: str) -> str:
        """Make text safe to drop inside a Python triple-quoted docstring."""
        return (text or "").replace('"""', '\u201c\u201d\u201c').replace("\r", "").replace("\n", " ").strip()

    vars_ = {
        "AGENT_DISPLAY_NAME":  display_name,
        # Docstring-safe (no triple-quotes, no newlines) for use inside `"""..."""` blocks.
        "AGENT_DISPLAY_NAME_DOC": _docsafe(display_name),
        "AGENT_DESCRIPTION_DOC":  _docsafe(description),
        # Python-safe repr for embedding inside .py string literals.
        # Handles quotes, backslashes, newlines that would otherwise break source.
        "AGENT_DISPLAY_NAME_REPR": repr(display_name),
        "AGENT_DESCRIPTION_REPR":  repr(description),
        "AGENT_SLUG":          slug,
        "AGENT_DESCRIPTION":   description,
        "AGENT_WRAPPER_MODULE": wrapper_module,
        "AGENT_CLASS_NAME":     safe_class,
        "PROVIDER":            p,
        "PROVIDER_LABEL":      _PROVIDER_LABELS.get(p, p.replace("_", " ").title()),
        "PROVIDER_LOGO_SVG":   _PROVIDER_LOGOS.get(p, _PROVIDER_LOGOS["custom"]),
        "TENANT_ID":           cfg["tenant_id"],
        "CLIENT_ID":           entra.get("app_id", ""),
        "CLIENT_SECRET":       entra.get("client_secret", ""),
        "SP_OBJECT_ID":        entra.get("sp_object_id", ""),
        "SERVER_HOST":         cfg["server"]["host"],
        "SERVER_PORT":         cfg["server"]["port"],
        "SESSION_SECRET":      session_secret,
        "FORCE_AUDIT":         "1" if cfg["monitoring"].get("force_audit") else "0",
        "PURVIEW_DLP_ENABLED": "1" if cfg["monitoring"].get("purview_dlp") else "0",
        "A365_ENABLED":        "1" if cfg["monitoring"].get("a365_observability") else "0",
        # Provider-specific
        "VERTEX_PROJECT":           pc.get("vertex_project", ""),
        "VERTEX_LOCATION":          normalize_gcp_location(pc.get("vertex_location")),
        "VERTEX_RESOURCE_NAME":     pc.get("vertex_resource_name", ""),
        "VERTEX_ADC_PATH":          pc.get("vertex_adc_path", ""),
        "AZURE_OPENAI_ENDPOINT":    pc.get("azure_openai_endpoint", ""),
        "AZURE_OPENAI_DEPLOYMENT":  pc.get("azure_openai_deployment", ""),
        "AZURE_OPENAI_API_VERSION": pc.get("azure_openai_api_version", ""),
        "AZURE_OPENAI_API_KEY":     pc.get("azure_openai_api_key", ""),
        "OPENAI_MODEL":             pc.get("openai_model", ""),
        "OPENAI_API_KEY":           pc.get("openai_api_key", ""),
        "HTTP_URL":                 pc.get("http_url", ""),
        "HTTP_METHOD":              pc.get("http_method", "POST"),
        "HTTP_PROMPT_FIELD":        pc.get("http_prompt_field", "prompt"),
        "HTTP_RESPONSE_JSONPATH":   pc.get("http_response_jsonpath", "response"),
        "HTTP_AUTH_HEADER":         pc.get("http_auth_header", ""),
        # AWS Bedrock
        "BEDROCK_REGION":           pc.get("bedrock_region", "").strip() or "us-east-1",
        "BEDROCK_MODE":             (pc.get("bedrock_mode", "") or "model").strip().lower(),
        "BEDROCK_MODEL_ID":         pc.get("bedrock_model_id", ""),
        "BEDROCK_AGENT_ID":         pc.get("bedrock_agent_id", ""),
        "BEDROCK_AGENT_ALIAS_ID":   pc.get("bedrock_agent_alias_id", ""),
        "BEDROCK_SESSION_ID":       pc.get("bedrock_session_id", ""),
        "BEDROCK_ENABLE_TRACE":     pc.get("bedrock_enable_trace", ""),
        "BEDROCK_ACCESS_KEY_ID":    pc.get("bedrock_access_key_id", ""),
        "BEDROCK_SECRET_ACCESS_KEY": pc.get("bedrock_secret_access_key", ""),
        "BEDROCK_SESSION_TOKEN":    pc.get("bedrock_session_token", ""),
        # Anthropic Claude (direct API)
        "ANTHROPIC_MODEL":          pc.get("anthropic_model", "") or "claude-3-5-sonnet-20241022",
        "ANTHROPIC_API_KEY":        pc.get("anthropic_api_key", ""),
        "ANTHROPIC_BASE_URL":       pc.get("anthropic_base_url", ""),
    }

    # Provider sanitization: keys that identify or authenticate a specific
    # provider deployment MUST be blanked when that provider is not the
    # selected one. Without this, a user who fills in (say) AWS Bedrock
    # creds then switches the radio to Vertex would still have the Bedrock
    # access key + agent ID land in the generated Vertex project's .env
    # because the hidden form fields still POST their values.
    #
    # Note: AGENT_RESOURCE_NAME is shared across modes (vertex uses it for
    # the reasoning-engine path) so it lives in the vertex group only.
    _PROVIDER_GROUPS: dict[str, list[str]] = {
        "vertex": [
            "VERTEX_PROJECT", "VERTEX_LOCATION",
            "VERTEX_RESOURCE_NAME", "VERTEX_ADC_PATH",
        ],
        "azure_openai": [
            "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_DEPLOYMENT",
            "AZURE_OPENAI_API_VERSION", "AZURE_OPENAI_API_KEY",
        ],
        "openai": ["OPENAI_MODEL", "OPENAI_API_KEY"],
        "bedrock": [
            "BEDROCK_MODE", "BEDROCK_MODEL_ID",
            "BEDROCK_AGENT_ID", "BEDROCK_AGENT_ALIAS_ID",
            "BEDROCK_SESSION_ID", "BEDROCK_ENABLE_TRACE",
            "BEDROCK_ACCESS_KEY_ID", "BEDROCK_SECRET_ACCESS_KEY",
            "BEDROCK_SESSION_TOKEN",
            # BEDROCK_REGION intentionally not in this list: it is
            # always emitted as AWS_REGION (with default us-east-1)
            # in env.tmpl, so leave it harmlessly set.
        ],
        "anthropic": [
            "ANTHROPIC_MODEL", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL",
        ],
        "custom": [
            "HTTP_URL", "HTTP_METHOD", "HTTP_PROMPT_FIELD",
            "HTTP_RESPONSE_JSONPATH", "HTTP_AUTH_HEADER",
        ],
    }
    # "aws_bedrock" is sometimes used as an alias for "bedrock" in the
    # provider value -- normalize so we don't accidentally blank the
    # Bedrock group when the user actually picked Bedrock.
    selected = "bedrock" if p == "aws_bedrock" else p
    for prov, keys in _PROVIDER_GROUPS.items():
        if prov != selected:
            for k in keys:
                if k in vars_:
                    vars_[k] = ""

    # Intra-Bedrock sanitization: when Bedrock is selected, the user has
    # also picked one of two Bedrock targets (model vs agent). Blank the
    # fields belonging to the OTHER target so leftovers from the user
    # toggling between modes don't land in .env. The form already disables
    # hidden inputs in JS, but defense-in-depth at the codegen layer
    # protects against JS-disabled browsers and test/script callers.
    if selected == "bedrock":
        mode = vars_.get("BEDROCK_MODE", "model")
        if mode == "agent":
            vars_["BEDROCK_MODEL_ID"] = ""
        elif mode == "model":
            vars_["BEDROCK_AGENT_ID"] = ""
            vars_["BEDROCK_AGENT_ALIAS_ID"] = ""
            vars_["BEDROCK_SESSION_ID"] = ""
            vars_["BEDROCK_ENABLE_TRACE"] = ""
    return vars_


def _materialize_secret_refs(
    vars_: dict[str, Any],
    cfg: dict,
    log: Callable[[str], None],
) -> list[str]:
    """Push every secret value in vars_ to Key Vault and replace with @KV: refs.

    Required when AGENT_KV_VAULT_NAME is set on the onboarder host OR the
    submitter explicitly picked a vault via cfg["kv_vault_name"]. Raises
    RuntimeError if no vault can be resolved. The caller in onboarder.py is
    expected to have already gated this before reaching codegen.

    Why this is a separate step from _build_vars():
      * Keeps _build_vars() pure - testable without network calls.
      * Surfaces KV failures with explicit logging at the codegen boundary.
      * On partial failure, we best-effort delete the secrets we already
        wrote in THIS run, so the vault is not left with orphans.

    Vault selection precedence (most specific wins):
      1. cfg["kv_vault_name"]   (per-onboarding user pick from the form picker)
      2. AGENT_KV_VAULT_NAME    (process-wide default set by the launcher)

    Returns the list of KV secret names that were written (for the report).
    """
    # Resolve which vault this onboarding writes to. Per-onboarding override
    # from the form picker takes precedence over the launcher default - this
    # is the multi-vault entry point.
    vault = (cfg.get("kv_vault_name") or "").strip() or kv.vault_name()
    if not vault:
        raise RuntimeError(
            "Key Vault is not configured on the onboarder. Either set "
            "AGENT_KV_VAULT_NAME=<vault-name> in the launcher environment "
            "or pick a vault in the form. The onboarder refuses to write "
            "literal secrets to disk."
        )

    slug = cfg["agent_slug"]
    log(f"[KV] using vault {vault!r} for secret materialization")

    written: list[str] = []
    try:
        for env_key, suffix in kv.SECRET_KEY_MAP.items():
            value = vars_.get(env_key, "")
            if not value:
                # Empty secret -> nothing to push; emit empty literal
                continue
            if kv.is_ref(value):
                # Already a @KV: ref (e.g. re-run with pre-vaulted values)
                continue
            secret_name = kv.secret_name_for(slug, env_key)
            ref = kv.put_secret(secret_name, value, vault=vault)
            vars_[env_key] = ref
            written.append(secret_name)
            log(f"[KV] pushed secret: {secret_name} (env={env_key}) -> {ref}")
    except Exception as exc:
        # Best-effort cleanup of what we wrote this run, so the vault is
        # not left with orphans pointing at a failed onboarding.
        if written:
            log(f"[KV] partial failure - rolling back {len(written)} secret(s)")
            for secret_name in written:
                kv.best_effort_delete(secret_name, vault=vault)
        raise RuntimeError(
            f"Key Vault materialization failed: {type(exc).__name__}: {exc}. "
            f"No files were written to disk."
        ) from exc

    log(f"[KV] materialized {len(written)} secret(s) for slug={slug}")
    return written


def render_project(
    out_dir: Path,
    cfg: dict,
    entra: dict,
    log: Callable[[str], None],
) -> list[Path]:
    """Render every required template into out_dir. Returns list of files written."""
    out_dir.mkdir(parents=True, exist_ok=True)
    vars_ = _build_vars(cfg, entra)

    # Materialize secrets to Key Vault (writes), replacing the literal
    # values in vars_ with @KV: references. Runs BEFORE any template
    # is written to disk - so a KV failure aborts with no plaintext
    # secrets ever landing in the generated folder.
    _materialize_secret_refs(vars_, cfg, log)

    written: list[Path] = []

    # core (server, env, README, etc.)
    for tname, oname in COMMON_TEMPLATES:
        tpath = TEMPLATE_DIR / tname
        if not tpath.exists():
            log(f"[WARN] template not found: {tname}")
            continue
        text = tpath.read_text(encoding="utf-8")
        rendered = _render(text, vars_)
        out_path = out_dir / oname
        out_path.write_text(rendered, encoding="utf-8")
        written.append(out_path)

    # wrapper — file name is personalized: `<safe_module>_wrapper.py`
    wrapper_tpath = TEMPLATE_DIR / WRAPPER_TEMPLATE
    if wrapper_tpath.exists():
        text = wrapper_tpath.read_text(encoding="utf-8")
        rendered = _render(text, vars_)
        wrapper_out = out_dir / f"{vars_['AGENT_WRAPPER_MODULE']}.py"
        wrapper_out.write_text(rendered, encoding="utf-8")
        written.append(wrapper_out)
    else:
        log(f"[WARN] wrapper template missing: {WRAPPER_TEMPLATE}")

    # provider adapter
    adapter_tmpl = PROVIDER_ADAPTER.get(cfg["provider"].lower(), "adapter_http.py.tmpl")
    tpath = TEMPLATE_DIR / adapter_tmpl
    if tpath.exists():
        text = tpath.read_text(encoding="utf-8")
        rendered = _render(text, vars_)
        out_path = out_dir / "adapter.py"
        out_path.write_text(rendered, encoding="utf-8")
        written.append(out_path)
    else:
        log(f"[WARN] adapter template missing: {adapter_tmpl}")

    return written

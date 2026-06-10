"""Environment & prerequisite checks for Agent SDK Onboarder.

Each check returns a structured `CheckResult` so we can render it nicely both
on the console (Diagnose.bat) and in the web UI.

Levels:
    "ok"      -- ready to go
    "warn"    -- not fatal, but the user should know (e.g. optional tool missing)
    "fail"    -- onboarding will fail until this is resolved
    "skipped" -- check not applicable to current config

Categories:
    "local"     -- machine-level (Python, az, dotnet, network)
    "tenant"    -- the Entra tenant the user is signed into
    "keyvault"  -- the Azure Key Vault selected for this onboarding
    "provider"  -- agent provider configuration (Vertex / AOAI / OpenAI / HTTP)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable

import urllib.request
import urllib.error

LogFn = Callable[[str], None]


@dataclass
class CheckResult:
    name: str
    category: str          # local | tenant | provider
    level: str             # ok | warn | fail | skipped
    summary: str           # one-line headline
    detail: str = ""       # multi-line context
    fix: str = ""          # suggested remediation
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------- helpers ----------
def _which(cmd: str) -> str | None:
    return shutil.which(cmd)


def _run(args: list[str], timeout: int = 30) -> tuple[int, str, str]:
    # On Windows, subprocess won't resolve .cmd / .bat extensions for the
    # executable unless we hand it the absolute path. shutil.which does.
    if args:
        resolved = shutil.which(args[0])
        if resolved:
            args = [resolved] + args[1:]
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except FileNotFoundError:
        return 127, "", f"{args[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"{args[0]}: timed out after {timeout}s"


def _http_head(url: str, timeout: int = 8) -> tuple[int, str]:
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, ""
    except urllib.error.HTTPError as e:
        # any HTTP response (even 401) means the host is reachable
        return e.code, ""
    except Exception as e:
        return 0, str(e)


# ---------- LOCAL checks ----------
def check_python() -> CheckResult:
    major, minor = sys.version_info[:2]
    summary = f"Python {major}.{minor}.{sys.version_info[2]}"
    if (major, minor) < (3, 10):
        return CheckResult("Python 3.10+", "local", "fail",
                           summary + " -- too old",
                           fix="Install Python 3.10 or newer from https://www.python.org/downloads/")
    return CheckResult("Python 3.10+", "local", "ok", summary,
                       data={"version": f"{major}.{minor}.{sys.version_info[2]}"})


def check_pip() -> CheckResult:
    rc, out, err = _run([sys.executable, "-m", "pip", "--version"])
    if rc != 0:
        return CheckResult("pip", "local", "fail", "pip not available",
                           detail=err,
                           fix="Run: python -m ensurepip --upgrade")
    return CheckResult("pip", "local", "ok", out.strip())


def check_az_cli() -> CheckResult:
    if not _which("az"):
        return CheckResult("Azure CLI", "local", "fail", "`az` not found on PATH",
                           fix="Install: https://learn.microsoft.com/cli/azure/install-azure-cli")
    rc, out, err = _run(["az", "version", "-o", "json"])
    if rc != 0:
        return CheckResult("Azure CLI", "local", "warn", "`az` installed but version check failed",
                           detail=err.strip()[:300])
    try:
        v = json.loads(out)
        ver = v.get("azure-cli", "?")
        return CheckResult("Azure CLI", "local", "ok", f"az {ver}", data={"version": ver})
    except Exception:
        return CheckResult("Azure CLI", "local", "ok", "az installed")


def check_az_login() -> CheckResult:
    if not _which("az"):
        return CheckResult("az login", "local", "skipped",
                           "Azure CLI missing -- can't check sign-in state")
    rc, out, err = _run(["az", "account", "show", "-o", "json"])
    if rc != 0:
        return CheckResult("az login", "local", "fail", "Not signed in to az",
                           detail=err.strip()[:300],
                           fix="Run: az login   (use the tenant where the agent will live)")
    try:
        acc = json.loads(out)
        user = (acc.get("user") or {}).get("name", "?")
        tenant = acc.get("tenantId", "?")
        sub = acc.get("name", "?")
        return CheckResult("az login", "local", "ok",
                           f"Signed in as {user}",
                           detail=f"Tenant: {tenant}\nSubscription: {sub}",
                           data={"user": user, "tenantId": tenant, "subscriptionId": acc.get("id"),
                                 "subscriptionName": sub})
    except Exception as e:
        return CheckResult("az login", "local", "warn", "Could not parse az output", detail=str(e))


def check_dotnet() -> CheckResult:
    if not _which("dotnet"):
        return CheckResult(".NET SDK (optional)", "local", "warn",
                           "dotnet not found -- needed only for Agent 365 blueprint",
                           fix="Install the .NET 8 SDK from https://dotnet.microsoft.com/download")
    rc, out, _ = _run(["dotnet", "--version"])
    if rc != 0:
        return CheckResult(".NET SDK (optional)", "local", "warn", "dotnet present but version failed")
    return CheckResult(".NET SDK (optional)", "local", "ok", f"dotnet {out.strip()}")


def check_a365_cli() -> CheckResult:
    if not _which("a365"):
        return CheckResult("a365 CLI (optional)", "local", "warn",
                           "`a365` CLI not installed -- needed only for Agent 365 blueprint",
                           fix="Install: dotnet tool install --global Microsoft.Agents.A365.DevTools.Cli")
    rc, out, _ = _run(["a365", "--version"], timeout=20)
    if rc != 0:
        return CheckResult("a365 CLI (optional)", "local", "warn", "`a365` present but version check failed")
    return CheckResult("a365 CLI (optional)", "local", "ok", f"a365 {out.strip().splitlines()[0]}")


def check_network() -> list[CheckResult]:
    targets = [
        ("https://login.microsoftonline.com/",        "Entra (login)"),
        ("https://graph.microsoft.com/v1.0/$metadata", "Microsoft Graph"),
        ("https://management.azure.com/",             "Azure ARM"),
        ("https://purview.microsoft.com/",            "Purview portal"),
    ]
    out: list[CheckResult] = []
    for url, label in targets:
        code, err = _http_head(url, timeout=6)
        if code == 0:
            out.append(CheckResult(f"Reachable: {label}", "local", "fail",
                                   f"Could not reach {url}",
                                   detail=err,
                                   fix="Check proxy / firewall / corp network settings."))
        else:
            out.append(CheckResult(f"Reachable: {label}", "local", "ok",
                                   f"{url} -> HTTP {code}"))
    return out


def check_port_free(port: int = 8080) -> CheckResult:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        s.close()
        return CheckResult(f"Port {port} free", "local", "ok",
                           f"Port {port} on 127.0.0.1 is available for the generated server")
    except OSError as e:
        return CheckResult(f"Port {port} free", "local", "warn",
                           f"Port {port} is in use -- pick a different one on the form",
                           detail=str(e))


# ---------- TENANT checks ----------
def check_tenant_id_format(tenant_id: str) -> CheckResult:
    if not tenant_id:
        return CheckResult("Tenant ID provided", "tenant", "fail", "No tenant ID",
                           fix="Sign in with `az login` so the tenant is auto-filled.")
    if len(tenant_id) != 36 or tenant_id.count("-") != 4:
        return CheckResult("Tenant ID format", "tenant", "warn",
                           f"`{tenant_id}` doesn't look like a GUID",
                           detail="Expected format: 8-4-4-4-12 hex characters")
    return CheckResult("Tenant ID format", "tenant", "ok", tenant_id)


def check_app_create_permission() -> CheckResult:
    """Best-effort: ask Graph whether the signed-in user can create apps."""
    if not _which("az"):
        return CheckResult("Can create Entra apps", "tenant", "skipped", "az missing")
    rc, out, err = _run(
        ["az", "rest", "--method", "get",
         "--url", "https://graph.microsoft.com/v1.0/me/memberOf?$select=displayName,id"],
        timeout=20,
    )
    if rc != 0:
        return CheckResult("Can create Entra apps", "tenant", "warn",
                           "Couldn't enumerate user roles",
                           detail=err.strip()[:200],
                           fix="The onboarder will still attempt to create the app; "
                               "if it fails, an admin must run the creation.")
    try:
        data = json.loads(out)
        roles = [g.get("displayName", "") for g in data.get("value", [])]
        privileged = {"Global Administrator", "Application Administrator",
                      "Cloud Application Administrator", "Privileged Role Administrator"}
        matched = [r for r in roles if r in privileged]
        if matched:
            return CheckResult("Can create Entra apps", "tenant", "ok",
                               "User holds: " + ", ".join(matched),
                               data={"roles": matched})
        # Most tenants still allow standard users to create app registrations by default,
        # but admin consent for API permissions will fail.
        return CheckResult("Can create Entra apps", "tenant", "warn",
                           "User is not a directory admin",
                           detail="App creation may work, but **admin consent** for "
                                  "Purview API permissions requires a Global / Application / "
                                  "Cloud Application Administrator.",
                           fix="Either sign in as an admin, or accept that an admin will "
                               "need to consent to the API permissions afterwards.",
                           data={"roles": roles})
    except Exception as e:
        return CheckResult("Can create Entra apps", "tenant", "warn",
                           "Could not parse Graph response", detail=str(e))


def check_purview_api_reachable() -> CheckResult:
    """Try a benign Graph call against the Purview activities namespace."""
    if not _which("az"):
        return CheckResult("Purview API reachable", "tenant", "skipped", "az missing")
    rc, out, err = _run(
        ["az", "rest", "--method", "get",
         "--url", "https://graph.microsoft.com/beta/security/dataSecurityAndGovernance/sensitivityLabels?$top=1"],
        timeout=20,
    )
    if rc == 0:
        return CheckResult("Purview Graph API reachable", "tenant", "ok",
                           "Sensitivity labels query succeeded")
    # 403 / 401 here typically means token is fine but our user lacks read on that endpoint --
    # which is OK for onboarding (the app itself will use app-only tokens).
    return CheckResult("Purview Graph API reachable", "tenant", "warn",
                       "Couldn't read sensitivity labels from Graph",
                       detail=err.strip()[:300],
                       fix="Onboarding can still proceed; the wrapper uses its own app-only "
                           "token at runtime. If runtime calls also fail, ensure your tenant "
                           "is licensed for Purview / Compliance E5.")


# ---------- KEY VAULT checks ----------
def check_keyvault_selection(vault_name: str) -> CheckResult:
    """Verify the user has chosen an Azure Key Vault for secret storage.

    Non-blocking warning if nothing is selected: codegen will fall back to the
    AGENT_KV_VAULT_NAME env-var default. But surface this so users running with
    no default see an explicit prompt to pick one.
    """
    name = (vault_name or "").strip()
    if not name:
        return CheckResult(
            "Key Vault selected", "keyvault", "fail",
            "No Key Vault chosen",
            detail="Secrets cannot be materialised into the generated .env without a vault.",
            fix="Click 'Change vault' in the 'Secrets storage' card on Step 1 and pick a vault, "
                "or set AGENT_KV_VAULT_NAME before launching the onboarder.",
            data={"vault_name": ""},
        )
    return CheckResult(
        "Key Vault selected", "keyvault", "ok",
        f"Vault '{name}' will receive this agent's secrets",
        data={"vault_name": name},
    )


def check_keyvault_access(vault_name: str) -> CheckResult:
    """Probe read+write+delete capability against the selected vault.

    Uses ``azure_vaults.probe_vault`` — same code path the picker uses — so any
    auth/role gap surfaces here BEFORE the user reaches the Onboard button.
    """
    name = (vault_name or "").strip()
    if not name:
        return CheckResult("Key Vault access", "keyvault", "skipped",
                           "No vault selected (see above)")
    # Local import: azure-keyvault-* / azure-mgmt-* deps may not be present in
    # bare CLI invocations of diagnostics.py. The web flow always has them.
    try:
        import azure_vaults as av  # type: ignore
    except Exception as e:
        return CheckResult("Key Vault access", "keyvault", "warn",
                           "Couldn't import azure_vaults module",
                           detail=str(e),
                           fix="Run `pip install -r requirements.txt` in the onboarder venv.")
    try:
        res = av.probe_vault(name)
    except Exception as e:
        return CheckResult("Key Vault access", "keyvault", "fail",
                           f"Probe of vault '{name}' raised",
                           detail=str(e)[:400],
                           fix="Run `az login` and ensure your identity has at least "
                               "'Key Vault Secrets Officer' on this vault.")
    if not isinstance(res, dict):
        return CheckResult("Key Vault access", "keyvault", "warn",
                           "Unexpected probe response", detail=repr(res)[:200])
    if res.get("ok") and res.get("can_write") and res.get("can_read"):
        delete_note = "" if res.get("can_delete") else " (delete missing — non-fatal)"
        return CheckResult(
            "Key Vault access", "keyvault", "ok",
            f"Read + write OK on vault '{name}'{delete_note}",
            data={k: res.get(k) for k in ("can_read", "can_write", "can_delete")},
        )
    code = (res.get("error_code") or "").strip()
    msg = (res.get("error") or "probe failed").strip()
    if code == "auth":
        return CheckResult("Key Vault access", "keyvault", "fail",
                           f"Cannot authenticate to vault '{name}'",
                           detail=msg[:400],
                           fix="Run `az login` (any tenant works as long as it has "
                               "RBAC on this vault) and retry.")
    if code in ("forbidden_write", "forbidden"):
        return CheckResult("Key Vault access", "keyvault", "fail",
                           f"Permission denied writing secrets to '{name}'",
                           detail=msg[:400],
                           fix="Grant your identity the 'Key Vault Secrets Officer' role on "
                               "this vault, then wait ~30s for RBAC propagation. Use the "
                               "'Provision new vault' picker option to auto-assign on creation.")
    if code == "not_found":
        return CheckResult("Key Vault access", "keyvault", "fail",
                           f"Vault '{name}' not found in your subscriptions",
                           detail=msg[:400],
                           fix="Pick a different vault from the picker, or create one via "
                               "'Provision new vault'.")
    return CheckResult("Key Vault access", "keyvault", "fail",
                       f"Vault '{name}' not usable for secret storage",
                       detail=msg[:400])


def run_keyvault_checks(vault_name: str) -> list[CheckResult]:
    """Run the suite of Key Vault checks."""
    sel = check_keyvault_selection(vault_name)
    if sel.level == "fail":
        # Don't bother probing a vault that wasn't picked — produce a single
        # skipped follow-up so the section still shows two slots and the user
        # sees what we WOULD check after they pick.
        return [sel, CheckResult("Key Vault access", "keyvault", "skipped",
                                 "Will probe read+write once a vault is selected")]
    return [sel, check_keyvault_access(vault_name)]


# ---------- PROVIDER checks ----------

# GCP project IDs: 6-30 chars, must start with lowercase letter, then
# lowercase letters / digits / hyphens, cannot end with hyphen.
# https://cloud.google.com/resource-manager/docs/creating-managing-projects
_GCP_PROJECT_ID_RE = re.compile(r"^[a-z][-a-z0-9]{4,28}[a-z0-9]$")

# projects/<project-number>/locations/<region>/reasoningEngines/<engine-id>
_REASONING_ENGINE_RE = re.compile(
    r"^projects/(\d+)/locations/([a-z0-9-]+)/reasoningEngines/(\d+)$"
)


def _looks_like_gcp_project_id(value: str) -> bool:
    """True iff ``value`` matches the GCP project-ID grammar."""
    return bool(_GCP_PROJECT_ID_RE.match(value or ""))


def _parse_reasoning_engine_id(resource: str) -> str | None:
    """Return the numeric engine-id from a Vertex Reasoning Engine resource
    name, or None if it doesn't parse."""
    m = _REASONING_ENGINE_RE.match((resource or "").strip())
    return m.group(3) if m else None


def _find_resource_collision(resource: str) -> str | None:
    """If ``resource`` matches any already-onboarded agent's AGENT_RESOURCE_NAME,
    return that agent's slug; otherwise None.

    Walks generated/*/.env so we can warn the user that what they typed
    is already wired to a *different* onboarded wrapper (the failure mode
    that caused the 'customer-service agent answers like the financial
    agent' incident).
    """
    target = (resource or "").strip()
    if not target:
        return None
    # `generated` lives one level up from this file's app/ directory.
    here = Path(__file__).resolve().parent
    gen = here.parent / "generated"
    if not gen.is_dir():
        return None
    for env_path in gen.glob("*/.env"):
        try:
            for line in env_path.read_text(encoding="utf-8").splitlines():
                if not line.startswith("AGENT_RESOURCE_NAME="):
                    continue
                if line.split("=", 1)[1].strip() == target:
                    return env_path.parent.name
        except Exception:  # noqa: BLE001
            continue
    return None


def check_provider(provider: str, pc: dict) -> list[CheckResult]:
    p = (provider or "").lower()
    if p in ("vertex", "vertex_ai"):
        return _check_vertex(pc)
    if p == "azure_openai":
        return _check_azure_openai(pc)
    if p == "openai":
        return _check_openai(pc)
    if p in ("bedrock", "aws_bedrock"):
        return _check_bedrock(pc)
    if p in ("anthropic", "claude"):
        return _check_anthropic(pc)
    if p in ("http", "custom"):
        return _check_http(pc)
    return [CheckResult("Provider config", "provider", "warn",
                        f"Unknown provider: {provider!r}")]


def _check_vertex(pc: dict) -> list[CheckResult]:
    out: list[CheckResult] = []
    required = {
        "vertex_project": "GCP project",
        "vertex_location": "Location",
        "vertex_resource_name": "Reasoning Engine resource name",
    }
    missing = [label for k, label in required.items() if not pc.get(k)]
    if missing:
        out.append(CheckResult("Vertex AI config", "provider", "fail",
                               "Missing: " + ", ".join(missing),
                               fix="Fill the Vertex AI fields on the form."))
    else:
        out.append(CheckResult("Vertex AI config", "provider", "ok",
                               f"Project={pc['vertex_project']}  Loc={pc['vertex_location']}"))

    # GCP project ID format check. Real project IDs are 6-30 lowercase
    # letters / digits / hyphens (must start with a letter). A value
    # containing spaces or uppercase is almost certainly the agent's
    # *display name* typed into the wrong field (caused at least one
    # production binding to the wrong reasoning engine).
    proj = (pc.get("vertex_project") or "").strip()
    if proj and not _looks_like_gcp_project_id(proj):
        out.append(CheckResult("GCP project ID format", "provider", "warn",
                               f"{proj!r} does not look like a GCP project ID",
                               fix="GCP project IDs are 6-30 chars, lowercase letters / "
                                   "digits / hyphens, start with a letter, no spaces "
                                   "(e.g. `active-gasket-361109`). You may have pasted the "
                                   "agent display name. Find the real ID at "
                                   "https://console.cloud.google.com/iam-admin/settings"))

    # Resource-name format + collision check. Catches "I typed the
    # financial agent's resource into the customer-service form" silently.
    res = (pc.get("vertex_resource_name") or "").strip()
    if res:
        engine_id = _parse_reasoning_engine_id(res)
        if engine_id is None:
            out.append(CheckResult("Resource name format", "provider", "warn",
                                   "Resource name doesn't match expected pattern",
                                   detail=res,
                                   fix="Expected: "
                                       "projects/<NUMBER>/locations/<REGION>/reasoningEngines/<ID>. "
                                       "Copy it from GCP Console -> Vertex AI -> Agent Builder "
                                       "-> your agent -> Resource name."))
        else:
            collision = _find_resource_collision(res)
            if collision:
                out.append(CheckResult("Resource name collision", "provider", "warn",
                                       f"This resource is already wired to onboarded agent "
                                       f"{collision!r}",
                                       fix="If you intended a NEW agent in GCP, you have the "
                                           "wrong resource name. Open GCP Vertex AI -> Agent "
                                           "Builder, find your new agent, and copy its full "
                                           "resource name (a different reasoningEngines/<ID>)."))

    # ADC file?
    adc = pc.get("vertex_adc_path") or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if adc:
        if os.path.isfile(adc):
            out.append(CheckResult("Vertex ADC file", "provider", "ok", adc))
        else:
            out.append(CheckResult("Vertex ADC file", "provider", "fail",
                                   f"File not found: {adc}",
                                   fix="Run: gcloud auth application-default login"))
    else:
        out.append(CheckResult("Vertex ADC file", "provider", "warn",
                               "No ADC path provided",
                               fix="Run `gcloud auth application-default login` or set "
                                   "GOOGLE_APPLICATION_CREDENTIALS so the wrapper can call Vertex."))

    if _which("gcloud") is None:
        out.append(CheckResult("gcloud CLI", "provider", "warn",
                               "gcloud not installed -- only needed to obtain ADC",
                               fix="https://cloud.google.com/sdk/docs/install"))
    return out


def _check_azure_openai(pc: dict) -> list[CheckResult]:
    out: list[CheckResult] = []
    required = ("azure_openai_endpoint", "azure_openai_deployment", "azure_openai_api_key")
    missing = [k for k in required if not pc.get(k)]
    if missing:
        out.append(CheckResult("Azure OpenAI config", "provider", "fail",
                               "Missing: " + ", ".join(missing)))
        return out
    endpoint = pc["azure_openai_endpoint"].rstrip("/")
    code, err = _http_head(endpoint, timeout=8)
    if code in (200, 401, 403, 404, 405):
        out.append(CheckResult("Azure OpenAI endpoint reachable", "provider", "ok",
                               f"{endpoint} -> HTTP {code}"))
    else:
        out.append(CheckResult("Azure OpenAI endpoint reachable", "provider", "fail",
                               f"Could not reach {endpoint}",
                               detail=err,
                               fix="Verify the endpoint URL and your network."))
    return out


def _check_openai(pc: dict) -> list[CheckResult]:
    out: list[CheckResult] = []
    if not pc.get("openai_api_key"):
        out.append(CheckResult("OpenAI API key", "provider", "fail", "Missing OPENAI_API_KEY"))
    else:
        key = pc["openai_api_key"]
        masked = key[:7] + "…" + key[-4:] if len(key) > 12 else "(set)"
        out.append(CheckResult("OpenAI API key", "provider", "ok", masked))
    code, err = _http_head("https://api.openai.com/v1/models", timeout=8)
    if code in (200, 401, 403):
        out.append(CheckResult("api.openai.com reachable", "provider", "ok", f"HTTP {code}"))
    else:
        out.append(CheckResult("api.openai.com reachable", "provider", "fail",
                               "Could not reach api.openai.com", detail=err))
    return out


def _check_bedrock(pc: dict) -> list[CheckResult]:
    out: list[CheckResult] = []
    region = (pc.get("bedrock_region") or "us-east-1").strip()
    mode = (pc.get("bedrock_mode") or "model").strip().lower()
    if mode not in ("model", "agent"):
        out.append(CheckResult("AWS Bedrock config", "provider", "fail",
                               f"Unsupported BEDROCK_MODE={mode!r}",
                               fix="Pick either 'Foundation model' or 'Deployed Bedrock Agent' on the form."))
        return out

    if mode == "agent":
        agent_id = (pc.get("bedrock_agent_id") or "").strip()
        alias_id = (pc.get("bedrock_agent_alias_id") or "").strip()
        missing = []
        if not agent_id: missing.append("Bedrock Agent ID")
        if not alias_id: missing.append("Agent alias ID")
        if missing:
            out.append(CheckResult("AWS Bedrock config", "provider", "fail",
                                   "Missing: " + ", ".join(missing),
                                   fix="Bedrock console -> Agents -> <agent> -> Aliases. "
                                       "Copy the agent ID (AGENT1234ABCD) and the alias ID."))
        else:
            out.append(CheckResult("AWS Bedrock config", "provider", "ok",
                                   f"Region={region}  Mode=agent  "
                                   f"AgentId={agent_id}  AliasId={alias_id}"))
    else:
        model_id = (pc.get("bedrock_model_id") or "").strip()
        if not model_id:
            out.append(CheckResult("AWS Bedrock config", "provider", "fail",
                                   "Missing: Bedrock model ID",
                                   fix="Pick a model in the Bedrock console (e.g. "
                                       "us.anthropic.claude-3-5-sonnet-20241022-v2:0) and paste its ID."))
        else:
            out.append(CheckResult("AWS Bedrock config", "provider", "ok",
                                   f"Region={region}  Mode=model  Model={model_id}"))

    has_key = bool(pc.get("bedrock_access_key_id") and pc.get("bedrock_secret_access_key"))
    aws_creds = Path.home() / ".aws" / "credentials"
    env_key = os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY")
    if has_key:
        key_id = pc["bedrock_access_key_id"]
        masked = key_id[:4] + "…" + key_id[-4:] if len(key_id) > 8 else "(set)"
        out.append(CheckResult("AWS credentials", "provider", "ok",
                               f"Inline access key {masked}"))
    elif aws_creds.is_file():
        out.append(CheckResult("AWS credentials", "provider", "ok",
                               f"Default credential file present: {aws_creds}"))
    elif env_key:
        out.append(CheckResult("AWS credentials", "provider", "ok",
                               "AWS_ACCESS_KEY_ID/SECRET set in this environment"))
    else:
        out.append(CheckResult("AWS credentials", "provider", "warn",
                               "No AWS credentials configured",
                               fix="Either fill the access key + secret on the form, run "
                                   "`aws configure`, or attach an IAM role to the host."))

    # Reachability — any HTTP response from the regional Bedrock host
    # means the endpoint resolves and is online (it will 403 for unsigned HEADs).
    if mode == "agent":
        host = f"https://bedrock-agent-runtime.{region}.amazonaws.com/"
        label = "Bedrock Agent runtime reachable"
    else:
        host = f"https://bedrock-runtime.{region}.amazonaws.com/"
        label = "Bedrock runtime reachable"
    code, err = _http_head(host, timeout=8)
    if code in (200, 400, 401, 403, 404, 405):
        out.append(CheckResult(label, "provider", "ok",
                               f"{host} -> HTTP {code}",
                               detail="(any HTTP response means the regional endpoint is online)"))
    elif code == 0:
        out.append(CheckResult(label, "provider", "warn",
                               f"Could not reach {host}",
                               detail=err,
                               fix="Check network / proxy. The runtime adapter still needs DNS to "
                                   f"{host.split('//',1)[1].rstrip('/')} at chat time."))
    else:
        out.append(CheckResult(label, "provider", "ok",
                               f"{host} -> HTTP {code}"))
    return out


def _check_anthropic(pc: dict) -> list[CheckResult]:
    out: list[CheckResult] = []
    key = pc.get("anthropic_api_key") or ""
    if not key:
        out.append(CheckResult("Anthropic API key", "provider", "fail",
                               "Missing ANTHROPIC_API_KEY",
                               fix="Generate one at https://console.anthropic.com/settings/keys"))
    else:
        masked = key[:8] + "…" + key[-4:] if len(key) > 14 else "(set)"
        out.append(CheckResult("Anthropic API key", "provider", "ok", masked))

    model = pc.get("anthropic_model") or "claude-3-5-sonnet-20241022"
    out.append(CheckResult("Anthropic model", "provider", "ok", model))

    base = (pc.get("anthropic_base_url") or "https://api.anthropic.com").rstrip("/")
    code, err = _http_head(f"{base}/v1/messages", timeout=8)
    # /v1/messages is POST-only -> 405 means reachable; 401/403 also means
    # the host answered (we didn't sign the request).
    if code in (200, 400, 401, 403, 404, 405):
        out.append(CheckResult("Anthropic API reachable", "provider", "ok",
                               f"{base} -> HTTP {code}",
                               detail="(HEAD on /v1/messages returns 405; that confirms reachability)"))
    elif code == 0:
        out.append(CheckResult("Anthropic API reachable", "provider", "warn",
                               f"Could not reach {base}",
                               detail=err,
                               fix="Check network / proxy / firewall."))
    else:
        out.append(CheckResult("Anthropic API reachable", "provider", "ok",
                               f"{base} -> HTTP {code}"))
    return out


def _check_http(pc: dict) -> list[CheckResult]:
    out: list[CheckResult] = []
    url = pc.get("http_url")
    if not url:
        out.append(CheckResult("Custom HTTP URL", "provider", "fail", "Missing endpoint URL"))
        return out
    if not (url.startswith("http://") or url.startswith("https://")):
        out.append(CheckResult("Custom HTTP URL format", "provider", "fail",
                               "URL must start with http:// or https://"))
        return out
    code, err = _http_head(url, timeout=8)
    if code in (200, 401, 403, 404, 405):
        out.append(CheckResult("Custom HTTP endpoint reachable", "provider", "ok",
                               f"{url} -> HTTP {code}",
                               detail="(any HTTP response means the host is reachable)"))
    elif code == 0:
        out.append(CheckResult("Custom HTTP endpoint reachable", "provider", "warn",
                               f"Could not reach {url}",
                               detail=err,
                               fix="If the endpoint isn't deployed yet, you can still onboard; "
                                   "just remember to fix HTTP_URL in the generated .env later."))
    else:
        out.append(CheckResult("Custom HTTP endpoint reachable", "provider", "ok",
                               f"{url} -> HTTP {code}"))
    return out


# ---------- runners ----------
def run_local_checks(server_port: int = 8080) -> list[CheckResult]:
    results = [
        check_python(),
        check_pip(),
        check_az_cli(),
        check_az_login(),
        check_dotnet(),
        check_a365_cli(),
        check_port_free(server_port),
    ]
    results.extend(check_network())
    return results


def run_tenant_checks(tenant_id: str) -> list[CheckResult]:
    return [
        check_tenant_id_format(tenant_id),
        check_app_create_permission(),
        check_purview_api_reachable(),
    ]


def run_all(cfg: dict | None = None) -> dict:
    """Run all applicable checks. cfg is optional and matches onboarder.py shape."""
    cfg = cfg or {}
    server_port = (cfg.get("server") or {}).get("port", 8080)
    tenant_id = cfg.get("tenant_id", "")
    provider = cfg.get("provider", "")
    pc = cfg.get("provider_config") or {}
    kv_vault_name = cfg.get("kv_vault_name", "")

    results: list[CheckResult] = []
    results.extend(run_local_checks(server_port))
    if tenant_id:
        results.extend(run_tenant_checks(tenant_id))
    # KV checks always run — picking the right secret store is required for
    # codegen regardless of provider, and a missing vault is itself a finding.
    results.extend(run_keyvault_checks(kv_vault_name))
    if provider:
        results.extend(check_provider(provider, pc))

    counts = {"ok": 0, "warn": 0, "fail": 0, "skipped": 0}
    for r in results:
        counts[r.level] = counts.get(r.level, 0) + 1
    overall = "fail" if counts["fail"] else ("warn" if counts["warn"] else "ok")
    return {
        "overall": overall,
        "counts": counts,
        "results": [r.to_dict() for r in results],
        "ready_to_onboard": counts["fail"] == 0,
    }


# ---------- console pretty-printer (used by Diagnose.bat) ----------
_LEVEL_COLORS = {
    "ok":      ("\x1b[32m", "[OK]"),
    "warn":    ("\x1b[33m", "[!!]"),
    "fail":    ("\x1b[31m", "[XX]"),
    "skipped": ("\x1b[90m", "[--]"),
}
_RESET = "\x1b[0m"


def print_report(report: dict, color: bool = True) -> None:
    cur_cat = None
    for r in report["results"]:
        if r["category"] != cur_cat:
            cur_cat = r["category"]
            print(f"\n-- {cur_cat.upper()} --")
        col, glyph = _LEVEL_COLORS.get(r["level"], ("", "?"))
        prefix = f"{col}{glyph}{_RESET}" if color else glyph
        # Replace any non-ASCII in summary (em-dash etc.) for safe cp1252 console.
        summary = r["summary"].encode("ascii", "replace").decode("ascii")
        print(f"  {prefix} {r['name']:38s} {summary}")
        if r["detail"]:
            for line in r["detail"].splitlines():
                line = line.encode("ascii", "replace").decode("ascii")
                print(f"      {line}")
        if r["level"] in ("warn", "fail") and r["fix"]:
            fix = r["fix"].encode("ascii", "replace").decode("ascii")
            print(f"      -> {fix}")
    c = report["counts"]
    print()
    print(f"  Summary: {c['ok']} ok | {c['warn']} warn | {c['fail']} fail | {c['skipped']} skipped")
    print(f"  Overall: {report['overall'].upper()}  "
          f"({'READY' if report['ready_to_onboard'] else 'NOT READY'} to onboard)")


def main() -> int:
    """CLI entrypoint used by Diagnose.bat."""
    # Enable ANSI on Windows consoles + force UTF-8 so emoji print correctly.
    if os.name == "nt":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
            kernel32.SetConsoleOutputCP(65001)  # UTF-8
        except Exception:
            pass
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    print("\n>>> Agent SDK Onboarder - Environment Diagnostics\n")
    report = run_all()
    print_report(report)

    # Persist a JSON copy next to the script for support tickets.
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        # write into the parent (AgentSDK-Onboarder/) folder
        out = os.path.join(os.path.dirname(here), "diagnostics-report.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
        print(f"\n  Full report saved to: {out}")
    except Exception as e:
        print(f"  (Could not save report: {e})")
    return 0 if report["overall"] != "fail" else 1


if __name__ == "__main__":
    sys.exit(main())

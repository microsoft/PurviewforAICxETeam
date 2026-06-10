"""One-shot migration: move plaintext secrets from generated/*/.env to Azure Key Vault.

Run with the project's onboarder venv:
    .\.venv\Scripts\python.exe scripts\migrate_existing_to_kv.py

For each existing generated agent folder, this script:
  1. Reads .env, identifies literal secret values (matching SECRET_KEY_MAP).
  2. Pushes each to KV with name f"{slug}-{suffix}".
  3. Rewrites .env in-place: replaces literal with @KV: ref. NO plaintext backup
     (the rubber-duck specifically called this out as violating the no-local-secrets
     rule). A redacted manifest is written instead.
  4. Writes kv-migration-<ts>.txt manifest listing the env-key -> KV-secret-name
     mapping for audit. Manifest contains NO secret values.
  5. Copies keyvault.py from app/codegen_templates/keyvault.py.tmpl into the
     agent folder so it can resolve refs at startup.
  6. Updates the agent's requirements.txt to include azure-keyvault-secrets +
     azure-identity (so a future pip install -r will get them).
  7. Installs azure-keyvault-secrets + azure-identity into the agent's existing
     venv (so we don't have to rebuild it).
  8. Patches server.py to:
       - import secrets as _secrets
       - call resolve_env_refs(os.environ) after load_dotenv(override=True)
       - auto-generate SESSION_SECRET if missing

This script is idempotent: secrets already in @KV: form are skipped; already-
patched server.py files are detected and not re-patched.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
sys.path.insert(0, str(APP))

import keyvault as kv  # noqa: E402

GENERATED = ROOT / "generated"
KV_TEMPLATE = APP / "codegen_templates" / "keyvault.py.tmpl"


def _load_env(path: Path) -> list[tuple[str, str | None, str]]:
    """Parse a .env file into a list of (key, value_or_None, raw_line) tuples.

    Comment / blank lines have key="" and value=None. Preserves order.
    """
    entries: list[tuple[str, str | None, str]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip("\r")
        if not line.strip() or line.lstrip().startswith("#"):
            entries.append(("", None, line))
            continue
        if "=" not in line:
            entries.append(("", None, line))
            continue
        k, _, v = line.partition("=")
        entries.append((k.strip(), v, line))
    return entries


def _write_env(path: Path, entries: list[tuple[str, str | None, str]]) -> None:
    out: list[str] = []
    for k, v, raw in entries:
        if not k:
            out.append(raw)
        else:
            out.append(f"{k}={v if v is not None else ''}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


# Env-var name (as it appears in .env) -> KV-name suffix
ENV_TO_SUFFIX: dict[str, str] = {
    "PURVIEW_CLIENT_SECRET":  "purview-client-secret",
    "AZURE_OPENAI_API_KEY":   "azure-openai-api-key",
    "OPENAI_API_KEY":         "openai-api-key",
    "AWS_ACCESS_KEY_ID":      "aws-access-key-id",
    "AWS_SECRET_ACCESS_KEY":  "aws-secret-access-key",
    "AWS_SESSION_TOKEN":      "aws-session-token",
    "ANTHROPIC_API_KEY":      "anthropic-api-key",
    "HTTP_AUTH_HEADER":       "http-auth-header",
}


def migrate_agent(folder: Path) -> dict:
    slug = folder.name
    env_path = folder / ".env"
    report: dict = {"slug": slug, "pushed": [], "skipped": [], "errors": []}
    if not env_path.exists():
        report["errors"].append(".env not found")
        return report

    entries = _load_env(env_path)

    # Step 1+2+3: push literal secrets to KV, rewrite values in entries.
    for i, (k, v, raw) in enumerate(entries):
        if k not in ENV_TO_SUFFIX:
            continue
        if not v or not v.strip():
            report["skipped"].append(f"{k}: empty")
            continue
        if v.strip().startswith(kv.KV_REF_PREFIX):
            report["skipped"].append(f"{k}: already @KV: ref")
            continue
        suffix = ENV_TO_SUFFIX[k]
        secret_name = f"{slug}-{suffix}"
        if len(secret_name) > 127:
            import hashlib
            digest = hashlib.sha1(slug.encode()).hexdigest()[:8]
            keep = 127 - len(suffix) - len(digest) - 2
            secret_name = f"{slug[:keep]}-{suffix}-{digest}"
        print(f"  [{slug}] pushing {k} to KV as {secret_name}")
        try:
            ref = kv.put_secret(secret_name, v.strip())
        except Exception as exc:
            report["errors"].append(f"{k}: {type(exc).__name__}: {exc}")
            continue
        entries[i] = (k, ref, raw)
        report["pushed"].append({"env_key": k, "secret_name": secret_name, "ref": ref})

    if report["errors"]:
        print(f"  [{slug}] ERRORS, refusing to rewrite .env: {report['errors']}")
        return report

    _write_env(env_path, entries)
    print(f"  [{slug}] .env rewritten with {len(report['pushed'])} @KV: refs")

    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    manifest_path = folder / f"kv-migration-{ts}.txt"
    lines = [
        f"# Key Vault migration manifest for {slug}",
        f"# Generated at {datetime.now(timezone.utc).isoformat()}",
        f"# Vault: {kv.vault_name()} (https://{kv.vault_name().lower()}.vault.azure.net/)",
        "#",
        "# This file is an audit trail listing which env vars were moved to KV.",
        "# It contains NO secret values.",
        "",
    ]
    for r in report["pushed"]:
        lines.append(f"{r['env_key']} -> {r['secret_name']}")
    for s in report["skipped"]:
        lines.append(f"# skipped: {s}")
    manifest_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"  [{slug}] manifest: {manifest_path.name}")

    if KV_TEMPLATE.exists():
        (folder / "keyvault.py").write_text(
            KV_TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8"
        )
        print(f"  [{slug}] copied keyvault.py")

    req_path = folder / "requirements.txt"
    if req_path.exists():
        req_text = req_path.read_text(encoding="utf-8")
        if "azure-keyvault-secrets" not in req_text:
            req_text = req_text.rstrip() + (
                "\n\n# Azure Key Vault integration (read side; resolves @KV: env "
                "references at boot)\nazure-identity>=1.15,<2.0\n"
                "azure-keyvault-secrets>=4.7,<5.0\n"
            )
            req_path.write_text(req_text, encoding="utf-8")
            print(f"  [{slug}] requirements.txt updated")

    venv_py = folder / ".venv" / "Scripts" / "python.exe"
    if venv_py.exists():
        print(f"  [{slug}] installing azure-* into venv ...")
        result = subprocess.run(
            [str(venv_py), "-m", "pip", "install", "--quiet",
             "azure-keyvault-secrets>=4.7,<5.0", "azure-identity>=1.15,<2.0"],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            print(f"  [{slug}] venv installed OK")
        else:
            report["errors"].append(f"pip install failed: {result.stderr[:200]}")
            print(f"  [{slug}] pip install FAILED: {result.stderr[:200]}")

    srv_path = folder / "server.py"
    if srv_path.exists():
        srv_text = srv_path.read_text(encoding="utf-8")
        if "resolve_env_refs" not in srv_text:
            srv_text = _patch_server(srv_text)
            srv_path.write_text(srv_text, encoding="utf-8")
            print(f"  [{slug}] server.py patched")
        else:
            print(f"  [{slug}] server.py already patched")

    return report


def _patch_server(text: str) -> str:
    """Add KV resolve hook and SESSION_SECRET auto-gen, keeping existing structure."""

    if "import secrets as _secrets" not in text:
        text = text.replace(
            "import uuid\n",
            "import secrets as _secrets\nimport uuid\n",
            1,
        )

    load_marker = "load_dotenv(override=True)"
    if load_marker in text and "from keyvault import resolve_env_refs" not in text:
        kv_block = (
            "load_dotenv(override=True)\n"
            "\n"
            "# Resolve any @KV:<vault>/<secret> references from Azure Key Vault\n"
            "# into their literal values, in-memory only. Fail-closed: raises if any\n"
            "# cannot be resolved. Must run AFTER load_dotenv (so refs are in\n"
            "# os.environ) and BEFORE any module-level os.environ[...] read below.\n"
            "from keyvault import resolve_env_refs  # noqa: E402\n"
            "resolve_env_refs(os.environ)\n"
        )
        text = text.replace(load_marker, kv_block, 1)

    text = re.sub(
        r'SESSION_SECRET\s*=\s*os\.environ\.get\("SESSION_SECRET"\s*,\s*"[^"]*"\s*\)',
        'SESSION_SECRET = os.environ.get("SESSION_SECRET") or _secrets.token_urlsafe(48)\n'
        'if not os.environ.get("SESSION_SECRET"):\n'
        '    log.info("SESSION_SECRET auto-generated at startup (in-memory only)")',
        text,
        count=1,
    )
    return text


if __name__ == "__main__":
    if not kv.is_kv_configured():
        print("ERROR: AGENT_KV_VAULT_NAME is not set. Refusing to run.")
        sys.exit(2)
    print(f"Vault: {kv.vault_name()}")
    print(f"Generated root: {GENERATED}")
    if not GENERATED.exists():
        print("No generated/ folder found.")
        sys.exit(0)
    overall_ok = True
    for folder in sorted(GENERATED.iterdir()):
        if not folder.is_dir() or folder.name.startswith("."):
            continue
        if ".archived-" in folder.name:
            continue
        print(f"\n=== Migrating {folder.name} ===")
        rep = migrate_agent(folder)
        print(f"  Result: pushed={len(rep['pushed'])} skipped={len(rep['skipped'])} errors={len(rep['errors'])}")
        if rep["errors"]:
            overall_ok = False
    print()
    print("=" * 60)
    print("Migration complete." if overall_ok else "Migration finished with errors.")
    sys.exit(0 if overall_ok else 1)

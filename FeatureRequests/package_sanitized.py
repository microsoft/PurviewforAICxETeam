#!/usr/bin/env python3
"""Build a sanitized, shareable package of this project (no embedded data/caches)."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ARRAY_VARS = {
    "rawData",
    "FEATURE_REQUESTS_DATA",
}

OBJECT_VARS = {
    "ENRICHMENT",
    "ADO_CUSTOMERS",
    "ADO_MATCHES",
    "ADO_TITLES",
    "ENG_ADO_MATCHES",
    "ENG_ADO_TITLES",
    "LINKED_FR",
    "SIMILAR_FRS",
    "RENEWAL_DATA",
}

DROP_FILES = {
    ".refresh_server.log",
    ".purview-refresh.log",
    ".refresh_progress.json",
    ".llm_score_cache.json",
    ".llm_score_cache.json.bak",
    "salesdata.xlsx",
}


def _replace_var_assignment(text: str, var_name: str, replacement: str) -> str:
    marker = f"const {var_name} ="
    start = text.find(marker)
    if start < 0:
        return text

    i = start + len(marker)
    while i < len(text) and text[i].isspace():
        i += 1
    if i >= len(text) or text[i] not in "[{":
        return text

    open_ch = text[i]
    close_ch = "]" if open_ch == "[" else "}"
    depth = 0
    in_str = False
    str_ch = ""
    esc = False
    j = i
    while j < len(text):
        c = text[j]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == str_ch:
                in_str = False
        else:
            if c in ('"', "'", "`"):
                in_str = True
                str_ch = c
            elif c == open_ch:
                depth += 1
            elif c == close_ch:
                depth -= 1
                if depth == 0:
                    j += 1
                    break
        j += 1

    while j < len(text) and text[j].isspace():
        j += 1
    if j < len(text) and text[j] == ";":
        j += 1

    return text[:start] + f"const {var_name} = {replacement};" + text[j:]


def sanitize_embedded_data(file_path: Path) -> None:
    text = file_path.read_text(encoding="utf-8", errors="ignore")
    original = text
    for var in ARRAY_VARS:
        text = _replace_var_assignment(text, var, "[]")
    for var in OBJECT_VARS:
        text = _replace_var_assignment(text, var, "{}")
    if text != original:
        file_path.write_text(text, encoding="utf-8")


def write_safe_admin_config(target_dir: Path) -> None:
    config = {
        "products": [{"id": "eee1947c-0ea7-ef11-8a69-6045bdee9a10", "name": "Purview for AI"}],
        "productId": "eee1947c-0ea7-ef11-8a69-6045bdee9a10",
        "productName": "Purview for AI",
        "areaPaths": ["IP Engineering\\Purview for AI\\1P Copilots\\M365 Copilot"],
        "areaPath": "IP Engineering\\Purview for AI\\1P Copilots\\M365 Copilot",
        "uatServiceNames": ["Purview for AI"],
        "uatServiceName": "Purview for AI",
    }
    (target_dir / "purview-admin-config.json").write_text(
        json.dumps(config, indent=2) + "\n",
        encoding="utf-8",
    )
    (target_dir / "purview-admin-config.example.json").write_text(
        json.dumps(config, indent=2) + "\n",
        encoding="utf-8",
    )


def write_package_readme(target_dir: Path) -> None:
    text = """Sanitized package notes
=======================

This package was generated to remove local/private artifacts before sharing.

What was sanitized:
- Embedded dashboard datasets in HTML/JS were cleared (rawData/ENRICHMENT/etc.).
- Local caches/logs were removed (.refresh_server.log, .llm_score_cache*.json, .purview-refresh.log).
- salesdata.xlsx was excluded.
- Admin config was reset to a safe default template.

Setup on a new machine:
1. Install prerequisites: Python 3.10+, Azure CLI, GitHub CLI (optional for LLM scoring).
2. Sign in: az login
3. Optional tenant scoping:
   export AZURE_TENANT_ID="<your-tenant-guid>"
4. Start server:
   python refresh_server.py
5. Open the HTML dashboard files in a browser and use Refresh.
"""
    (target_dir / "SANITIZED_PACKAGE_README.txt").write_text(text, encoding="utf-8")


def copy_project(src: Path, dst: Path) -> None:
    def _ignore(_dir: str, names: list[str]) -> set[str]:
        ignored = set()
        for name in names:
            if name in DROP_FILES:
                ignored.add(name)
            if name == "__pycache__":
                ignored.add(name)
            if name.endswith(".pyc"):
                ignored.add(name)
        return ignored

    shutil.copytree(src, dst, ignore=_ignore)


def sanitize_tree(target_dir: Path) -> None:
    for ext in ("*.html", "*.js"):
        for file_path in target_dir.glob(ext):
            sanitize_embedded_data(file_path)
    write_safe_admin_config(target_dir)
    write_package_readme(target_dir)


def make_zip(target_dir: Path, output_zip: Path) -> None:
    output_zip.parent.mkdir(parents=True, exist_ok=True)
    base_name = str(output_zip.with_suffix(""))
    shutil.make_archive(base_name, "zip", root_dir=target_dir.parent, base_dir=target_dir.name)


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a sanitized distribution package.")
    parser.add_argument("--source", default=str(Path(__file__).resolve().parent), help="Source project directory")
    parser.add_argument("--output", default="", help="Output zip path (optional)")
    args = parser.parse_args()

    src = Path(args.source).resolve()
    if not src.exists():
        raise SystemExit(f"Source directory does not exist: {src}")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_zip = Path(args.output).resolve() if args.output else src.parent / f"{src.name}-sanitized-{stamp}.zip"

    with tempfile.TemporaryDirectory(prefix="frs-sanitize-") as tmp:
        tmp_root = Path(tmp)
        staged = tmp_root / f"{src.name}-sanitized"
        copy_project(src, staged)
        sanitize_tree(staged)
        make_zip(staged, out_zip)

    print(f"✅ Created sanitized package: {out_zip}")


if __name__ == "__main__":
    main()

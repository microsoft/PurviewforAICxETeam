# Agent 365 + Purview SDK Onboarder

> One-click onboarding for AI agents into **Microsoft Purview** + **Agent 365 SDK**.
> A local web wizard that wraps your existing agent (Vertex AI, AWS Bedrock, OpenAI, etc.),
> registers it with Entra, wires up DSPM for AI / DLP / audit, and gives you a runnable
> Python wrapper — all without leaving the browser.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Status: Beta](https://img.shields.io/badge/status-beta-orange.svg)](#status)

---

## ✨ What it does

- 🪪 **Identity gate** — confirms which `az` account / tenant / subscription you're
  onboarding *as* before creating any real resources.
- 🔐 **Key Vault integration** — picks (or provisions) an Azure Key Vault for
  storing the agent's credentials so nothing lands in plain text.
- ✅ **Live prerequisite checks** — environment, tenant, Key Vault access, and
  provider endpoint, all with a sleek progress bar.
- ⚙️ **Codegen** — generates a Flask wrapper for your agent that streams
  prompts through Purview classification + DLP + audit logging.
- 🚀 **One-click launch** — spawns the wrapped agent in its own venv, with
  a live install/start log and phase-aware progress indicator.
- 📜 **History** — every onboarded agent recorded with quick re-launch
  and open-folder shortcuts.

## 🚀 Quick start (testable copy)

You have **three** ways to run it. Pick the one that fits your environment.

### Option 1 — `pipx install` from the wheel (most reliable on managed devices)

The wheel uses your system Python, which is already trusted by any
WDAC / Application-Control policy. Recommended for corporate-managed Windows.

```powershell
python -m pip install --user --upgrade pipx
python -m pipx install .\dist\agent365_purview_sdk_onboarder-0.1.0-py3-none-any.whl
agent365-onboarder
```

Generated agents land in `%LOCALAPPDATA%\Agent365PurviewSDKOnboarder\generated\`.

### Option 2 — Standalone folder (`-OneDir` build)

Self-contained — does **not** need Python installed. DLLs live in a stable
folder beside the `.exe`, so WDAC policies that block `%TEMP%` extraction
still allow it. Distribute as a `.zip`.

```powershell
# Build:
.\scripts\build_exe.ps1 -OneDir
# Result:
#   dist\Agent365PurviewSDKOnboarder\Agent365PurviewSDKOnboarder.exe   (15 MB exe + 27 MB libs)
#   dist\Agent365PurviewSDKOnboarder.zip                              (~26 MB zipped)

# Run:
.\dist\Agent365PurviewSDKOnboarder\Agent365PurviewSDKOnboarder.exe
```

### Option 3 — One-file `.exe` (single file, may be blocked by WDAC)

```powershell
.\scripts\build_exe.ps1
.\dist\Agent365PurviewSDKOnboarder.exe
```

> ⚠️ One-file mode extracts `python3XX.dll` to `%TEMP%` on launch. Strict
> Windows Defender Application Control policies (common on corporate Windows)
> will block this with:
> *"An Application Control policy has blocked this file."*
> Use Option 1 or Option 2 in that case.

### Option 4 — Run from source

```powershell
cd Agent365PurviewSDKOnboarder
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .
agent365-onboarder
# (or:  python agent365_onboarder.py)
```

In all four cases a browser tab opens automatically at `http://127.0.0.1:5050/`
(or the next free port if 5050 is in use).

## 🛠️ Prerequisites

| Prerequisite | Required for | Install |
|---|---|---|
| Azure CLI (`az`) | All onboarding flows (identity, perms, KV) | <https://aka.ms/installazurecli> |
| Python 3.10+ | Source runs + running generated agents | `winget install Python.Python.3.12` |
| Internet access | Azure ARM / Graph / Purview API calls | — |

Optional (only if you'll use the corresponding feature):

| Prereq | Feature |
|---|---|
| `.NET 8 SDK` + Agent 365 CLI | Agent 365 blueprint registration |
| `gcloud` CLI | Vertex AI provider |
| `aws` CLI | AWS Bedrock provider |

## 🏗️ Building distributables

```powershell
# Default = one-file .exe (~26 MB):
.\scripts\build_exe.ps1
# → dist\Agent365PurviewSDKOnboarder.exe

# WDAC-safe folder layout:
.\scripts\build_exe.ps1 -OneDir
# → dist\Agent365PurviewSDKOnboarder\
# → dist\Agent365PurviewSDKOnboarder.zip (manually zip the folder to share)

# Force-clean rebuild:
.\scripts\build_exe.ps1 -Clean

# Wheel (for pipx / PyPI):
.\.venv-build\Scripts\python.exe -m build --wheel
# → dist\agent365_purview_sdk_onboarder-0.1.0-py3-none-any.whl
```

## 📂 Layout

```
Agent365PurviewSDKOnboarder/
├── agent365_onboarder.py     # Top-level launcher (auto-port, browser-open)
├── app/                      # Flask app + diagnostics + codegen
│   ├── onboarder.py          # Flask routes
│   ├── diagnostics.py        # Prereq check engine
│   ├── azure_vaults.py       # Key Vault picker / probe
│   ├── identity.py           # az CLI integration
│   ├── codegen.py            # Agent wrapper generator
│   ├── workflow.py           # Entra app + Purview onboarding orchestration
│   ├── templates/            # Jinja2 HTML
│   ├── static/               # CSS + assets
│   └── codegen_templates/    # Wrapper templates for generated agents
├── installer/                # PyInstaller spec + icons
├── scripts/                  # Build + helper scripts
├── pyproject.toml            # Package metadata + deps + entry point
├── LICENSE                   # MIT
└── README.md
```

## 🔒 Security

- All POST endpoints are CSRF-protected and Origin-gated to `127.0.0.1`.
- Agent credentials are written to Azure Key Vault — never to plain-text files.
- The onboarder writes one short-lived `onboarder-probe-<uuid>` secret to your
  selected Key Vault during prereq checks; it is best-effort deleted immediately.
- No telemetry. No external network calls beyond Azure APIs your `az` session
  is already authorized for.

See the [Security section below](#-security) for vulnerability reporting.

## 📦 Status

**Beta.** The onboarder is being used internally for live agent onboarding;
the core flow is stable. The standalone `.exe` build is fresh — please report
any "works from source but not from .exe" issues with the contents of
`%LOCALAPPDATA%\Agent365PurviewSDKOnboarder\logs\onboarder.log`.

## 📄 License

[MIT License](LICENSE) — © Microsoft Corporation.

## 🛡️ Security

For security issues, please follow the disclosure process in the parent repo's
[`SECURITY.md`](https://github.com/microsoft/PurviewforAICxETeam/blob/main/SECURITY.md).
Do **not** open public GitHub issues for security vulnerabilities — report them
to MSRC at <https://msrc.microsoft.com>.

## ™️ Trademarks

This project may contain trademarks or logos for projects, products, or services.
Authorized use of Microsoft trademarks or logos is subject to and must follow
[Microsoft's Trademark & Brand Guidelines](https://www.microsoft.com/en-us/legal/intellectualproperty/trademarks/usage/general).
Use of Microsoft trademarks or logos in modified versions of this project must
not cause confusion or imply Microsoft sponsorship. Any use of third-party
trademarks or logos are subject to those third-party's policies.

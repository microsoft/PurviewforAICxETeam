# Changelog

All notable changes to the **Agent 365 + Purview SDK Onboarder** are documented
here. Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] — 2026-06-10

### Added — first packaged release

- **Distribution paths** — three ways to install:
  - `pipx` wheel (`agent365_purview_sdk_onboarder-0.1.0-py3-none-any.whl`)
    — uses system Python, works on WDAC-locked corporate devices
  - One-DIR `.exe` folder + `.zip` — self-contained Windows distribution
    that passes Application-Control policies (DLLs live in a stable path
    rather than `%TEMP%`)
  - One-FILE `.exe` — convenient single binary for non-WDAC environments
- **Top-level launcher** (`agent365_onboarder.py`) with auto port selection
  (5050 → first free), browser auto-open once the server socket binds, and
  a per-runtime workspace at `%LOCALAPPDATA%\Agent365PurviewSDKOnboarder\`
  for generated agents and logs.
- **Apache 2.0 → MIT** license alignment with the parent
  `microsoft/PurviewforAICxETeam` repository.
- **PyInstaller spec** with WDAC-aware hooks override (neutralizes the
  conflicting `pyinstaller-hooks-contrib\hook-workflow.py`) and a frozen-only
  Flask `root_path` patch that ensures bundled `templates/` + `static/` are
  resolved correctly inside the PyInstaller bundle.
- **Build automation** (`scripts/build_exe.ps1`) with `-OneDir` and `-Clean`
  switches.
- **Security hygiene**: tightened `[tool.setuptools.package-data]` patterns
  and explicit `[tool.setuptools.exclude-package-data]` so dev artifacts
  (`*.bak`, `*.log`, `*.pid`, `.env*`) can never sneak into a wheel.

### Carried forward from the source project (`AgentSDK-Onboarder` v1.12)

The full onboarder feature set is preserved verbatim — no behavior changes
were made during packaging. Notable capabilities:

- Identity confirmation gate before any real Azure resources are created.
- Live prerequisite checks with phased progress UI (env / tenant / Key Vault
  / provider).
- Multi-vault Key Vault picker with first-run wizard.
- Codegen for Vertex AI, AWS Bedrock (Converse + InvokeAgent), Azure
  OpenAI, OpenAI public, Anthropic Claude, and custom HTTP wrappers.
- One-click launch with phase-aware progress bar parsing live pip /
  uvicorn output.
- Per-agent history with quick re-launch and open-folder shortcuts.

### Known limitations

- One-FILE `.exe` is blocked by Windows Defender Application Control (WDAC)
  policies on managed corporate devices. Use the pipx wheel or one-DIR build
  in those environments.
- Generated wrapped-agents require **system Python 3.10+** as a prerequisite
  (not bundled). The first-run wizard in a future release will detect a
  missing interpreter and link to `winget install Python.Python.3.12`.
- Closing the onboarder console window leaves spawned wrapped-agent
  processes running (intentional — they survive onboarder restarts), but a
  future system-tray release will offer a "Stop all" action.
- No code signing yet. Customer ship outside the team will trigger a
  Windows SmartScreen "Unknown publisher" warning.

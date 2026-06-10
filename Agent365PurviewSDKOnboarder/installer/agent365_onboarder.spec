# PyInstaller spec for Agent 365 + Purview SDK Onboarder.
#
# Builds a one-file Windows .exe that bundles the launcher + the entire
# app/ tree (templates, static, codegen_templates). At runtime PyInstaller
# extracts everything to ``sys._MEIPASS`` and the launcher
# (``agent365_onboarder.py``) re-roots itself there.
#
# Build with:
#   pyinstaller installer/agent365_onboarder.spec --clean --noconfirm
# Produces:
#   dist/Agent365PurviewSDKOnboarder.exe
# pylint: disable=undefined-variable

from pathlib import Path

# When PyInstaller evaluates the spec, the cwd is the project root (where the
# user invoked `pyinstaller`). SPEC refers to this spec file inside
# installer/, so the project root is its parent.
project_root = Path(SPEC).resolve().parent.parent  # type: ignore[name-defined]

# Bundle every template/static/codegen file. We use a directory-recursive
# collection so future additions don't need spec edits.
def _datas(src_rel: str):
    src = project_root / src_rel
    if not src.exists():
        return []
    pairs = []
    for f in src.rglob("*"):
        if f.is_file():
            rel_parent = f.parent.relative_to(project_root)
            pairs.append((str(f), str(rel_parent)))
    return pairs


datas = (
    _datas("app/templates")
    + _datas("app/static")
    + _datas("app/codegen_templates")
)

# Hidden imports — Azure SDK and Flask occasionally hide imports behind
# string-based lookups that PyInstaller's static analyzer misses.
hiddenimports = [
    "flask",
    "jinja2",
    "werkzeug",
    "itsdangerous",
    "click",
    "blinker",
    "azure.identity",
    "azure.keyvault.secrets",
    "azure.mgmt.keyvault",
    "azure.mgmt.resource",
    "azure.mgmt.subscription",
    "azure.mgmt.authorization",
    "azure.core",
    "azure.core.pipeline",
    "azure.core.pipeline.policies",
    "azure.core.pipeline.transport",
    "msal",
    "msal_extensions",
    # Modules the launcher inserts onto sys.path from inside app/.
    "onboarder",
    "diagnostics",
    "identity",
    "azure_vaults",
    "keyvault",
    "workflow",
    "codegen",
]

block_cipher = None

a = Analysis(  # noqa: F821
    [str(project_root / "agent365_onboarder.py")],
    pathex=[str(project_root), str(project_root / "app")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[str(project_root / "installer" / "hooks")],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "PIL",
        "pytest",
        "IPython",
        "notebook",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="Agent365PurviewSDKOnboarder",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX often triggers AV false-positives — leave it off.
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,  # Keep a console window so users can see the URL + Ctrl+C to stop.
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # icon=str(project_root / "installer" / "icons" / "onboarder.ico"),
)

# PyInstaller spec — one-DIR variant (DLLs stable on disk, no temp extraction).
#
# Some WDAC / Application-Control policies block DLLs loaded from %TEMP% (the
# default --onefile extraction target). One-dir builds put python3XX.dll
# alongside the .exe in a stable user-app folder, which a number of policies
# allow even when temp-extraction is blocked.
#
# Build with:
#   pyinstaller installer/agent365_onboarder_onedir.spec --clean --noconfirm
# Produces:
#   dist/Agent365PurviewSDKOnboarder/
#     Agent365PurviewSDKOnboarder.exe
#     python3XX.dll
#     ...
# Distribute as a zip of that folder.
# pylint: disable=undefined-variable

from pathlib import Path

project_root = Path(SPEC).resolve().parent.parent  # type: ignore[name-defined]


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

# ---- one-DIR layout: EXE bootloader only, then COLLECT bundles everything --
exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Agent365PurviewSDKOnboarder",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Agent365PurviewSDKOnboarder",
)

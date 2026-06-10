"""Agent 365 + Purview SDK Onboarder — top-level launcher.

This is the entry point for both:
  * ``python agent365_onboarder.py`` — development / source runs
  * ``agent365-onboarder`` console script after ``pipx install``
  * The PyInstaller one-file ``.exe`` — frozen distribution

Responsibilities:
  1. Resolve where the bundled ``app/`` directory lives (works in both
     source-checkout and PyInstaller ``_MEIPASS`` extraction layouts).
  2. Put ``app/`` on ``sys.path`` so the legacy flat imports inside it
     (``import diagnostics`` etc.) resolve.
  3. Pick a writable workspace dir for generated agents + logs.
     * Source run: defaults to repo root (current behavior).
     * Frozen ``.exe`` run: defaults to ``%LOCALAPPDATA%\\Agent365PurviewSDKOnboarder``.
     * Env var ``ONBOARDER_ROOT`` overrides in either case.
  4. Find a free TCP port (preferred 5050, falls back to 5051+).
  5. Schedule a browser auto-open once the server is listening.
  6. Boot the Flask app.
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

# --------------------------------------------------------------------------- #
# Path / packaging helpers
# --------------------------------------------------------------------------- #
def _bundle_root() -> Path:
    """Return the directory containing the bundled ``app/`` folder.

    * PyInstaller one-file: ``sys._MEIPASS`` (temp extraction dir).
    * PyInstaller one-dir:  parent of ``sys.executable``.
    * Source checkout:      parent of this file.
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return Path(__file__).resolve().parent


def _workspace_root() -> Path:
    """Where generated agents and logs live (must be writable).

    Resolution order:
      1. ``ONBOARDER_ROOT`` env var (explicit override, always wins).
      2. PyInstaller frozen .exe → ``%LOCALAPPDATA%\\Agent365PurviewSDKOnboarder``.
      3. Installed-as-package (pipx / pip / wheel) → also LOCALAPPDATA — site-packages
         is NOT a sensible place to drop generated user data.
      4. Source checkout (this file's parent contains ``pyproject.toml``) → repo root.
      5. Fallback → LOCALAPPDATA.
    """
    override = os.environ.get("ONBOARDER_ROOT")
    if override:
        return Path(override).resolve()

    def _local_app_dir() -> Path:
        local_app = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return (Path(local_app) / "Agent365PurviewSDKOnboarder").resolve()

    if getattr(sys, "frozen", False):
        return _local_app_dir()

    here = Path(__file__).resolve().parent
    # Source checkout: pyproject.toml sits beside this file.
    if (here / "pyproject.toml").is_file():
        return here

    # Otherwise we're inside site-packages — use a writable, user-scoped dir.
    return _local_app_dir()


def _find_free_port(preferred: int = 5050, attempts: int = 20) -> int:
    """Bind-test ports starting at ``preferred`` and return the first free one."""
    for offset in range(attempts):
        port = preferred + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(
        f"No free port found in range {preferred}-{preferred + attempts - 1}"
    )


def _wait_for_server_then_open(url: str, port: int, timeout_s: float = 15.0) -> None:
    """Poll the port until it's listening, then open the default browser."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                try:
                    webbrowser.open(url)
                except Exception:
                    pass
                return
        time.sleep(0.15)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main() -> int:
    bundle = _bundle_root()
    app_dir = bundle / "app"
    if not app_dir.is_dir():
        sys.stderr.write(
            f"ERROR: bundled app/ directory not found at {app_dir}\n"
            "       Did the build complete successfully?\n"
        )
        return 2

    # Make the legacy flat imports inside app/ resolve.
    sys.path.insert(0, str(app_dir))

    # Workspace dir for generated agents + log files.
    workspace = _workspace_root()
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "generated").mkdir(parents=True, exist_ok=True)
    (workspace / "logs").mkdir(parents=True, exist_ok=True)
    # onboarder.py honors ONBOARDER_ROOT when computing GENERATED.
    os.environ["ONBOARDER_ROOT"] = str(workspace)

    # Sensible defaults for first-run UX. The user can still override
    # via environment variables before launching.
    os.environ.setdefault("AGENT_KV_VAULT_NAME", "SDKOnboarder")

    host = os.environ.get("ONBOARDER_HOST", "127.0.0.1")
    preferred_port = int(os.environ.get("ONBOARDER_PORT", "5050"))
    port = _find_free_port(preferred_port)
    os.environ["ONBOARDER_PORT"] = str(port)

    url = f"http://{host}:{port}/"
    print()
    print("=" * 64)
    print(" Agent 365 + Purview SDK Onboarder")
    print("=" * 64)
    print(f"  Listening on:  {url}")
    print(f"  Workspace:     {workspace}")
    print(f"  Browser:       opening automatically in ~1 second")
    print(f"  Stop:          Ctrl+C in this window")
    print("=" * 64)
    print()

    # Open the browser once the Flask socket is actually accepting.
    threading.Thread(
        target=_wait_for_server_then_open,
        args=(url, port),
        daemon=True,
    ).start()

    # Importing onboarder runs all its module-level code, which now sees the
    # ONBOARDER_ROOT we just set.
    import onboarder  # type: ignore[import-not-found]

    # In a PyInstaller bundle there's no onboarder.py on disk, so Flask's
    # auto-derived ``root_path`` (and thus template_folder / static_folder)
    # points to a nonexistent location. Re-root the Flask app at the bundled
    # ``app/`` directory so the relative ``templates`` and ``static`` paths
    # passed to ``Flask(...)`` resolve correctly. No-op in source/pipx mode.
    if getattr(sys, "frozen", False):
        onboarder.app.root_path = str(app_dir)
        onboarder.app.template_folder = "templates"
        onboarder.app.static_folder = "static"
        # Drop Jinja's cached loader so it picks up the new template_folder.
        onboarder.app.jinja_env.loader = onboarder.app.create_global_jinja_loader()

    # Flask dev server is fine for the single-user local use case.
    onboarder.app.run(
        host=host,
        port=port,
        threaded=True,
        debug=False,
        use_reloader=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Cross-platform auto-refresh for Purview for AI dashboard."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> int:
    script_dir = Path(__file__).resolve().parent
    log_file = script_dir / ".purview-refresh.log"
    dashboard_script = script_dir / "refresh_dashboard.py"

    with log_file.open("a", encoding="utf-8") as f:
        f.write(f"=== Auto-refresh started ===\n")
        result = subprocess.run([sys.executable, str(dashboard_script)], stdout=f, stderr=subprocess.STDOUT)
        f.write(f"=== Auto-refresh completed (rc={result.returncode}) ===\n\n")

    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())

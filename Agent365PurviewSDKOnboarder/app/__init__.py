"""Agent 365 + Purview SDK Onboarder — internal package.

The modules in this package import each other with flat imports
(e.g. ``import diagnostics``) for historical reasons. The launcher
script (``agent365_onboarder.py`` at the repo root) puts this
directory on ``sys.path`` before importing so those flat imports
resolve correctly whether running from source or from a PyInstaller
one-file bundle.
"""

__version__ = "0.1.0"

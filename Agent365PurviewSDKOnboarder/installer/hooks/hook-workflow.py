# PyInstaller hook override.
#
# pyinstaller-hooks-contrib ships hook-workflow.py for the unrelated PyPI
# package "workflow" (https://pypi.org/project/workflow/). Our codebase has
# its own flat module ``app/workflow.py`` that PyInstaller resolves as
# ``workflow`` (because the launcher inserts ``app/`` onto sys.path before
# importing it). That makes the contrib hook fire against OUR module, which
# crashes the build with ImportErrorWhenRunningHook.
#
# This empty file shadows the contrib hook so nothing third-party touches our
# flat ``workflow`` module. There is nothing to collect for it — it's a
# single pure-Python module already included via the spec's Analysis target.

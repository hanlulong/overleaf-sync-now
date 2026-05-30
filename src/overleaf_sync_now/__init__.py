"""overleaf-sync-now: keep local Overleaf files fresh before AI edits."""
# Single source of truth for the package version. pyproject.toml derives its
# version from this attribute via `[tool.setuptools.dynamic]`, and cli.py's
# `--version` reads it, so the three can never drift (the 0.3.1-vs-0.3.2 skew
# this replaced). Bump here on release; CITATION.cff is the only other place
# that needs a manual bump (a test pins the two together).
__version__ = "0.4.0"

from .cli import main

__all__ = ["main", "__version__"]

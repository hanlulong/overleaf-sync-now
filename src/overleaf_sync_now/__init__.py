"""overleaf-sync-now: keep local Overleaf files fresh before AI edits."""
__version__ = "0.2.1"

from .cli import main

__all__ = ["main", "__version__"]

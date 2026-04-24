"""overleaf-sync-now: trigger Overleaf's Dropbox sync on demand."""
__version__ = "0.1.1"

from .cli import main

__all__ = ["main", "__version__"]

"""arcgis-inventory --- portal inventory, dependency graph, and audits.

Portal-agnostic and configuration-driven by construction: no organization's
name, item ids, service URLs, domains, layer names, or business rules appear
anywhere in this package. Everything site-specific arrives through the
environment or a user-supplied config file.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version

try:  # installed
    __version__ = _version("arcgis-inventory")
except PackageNotFoundError:  # running from a source checkout
    __version__ = "0.0.0.dev0"

__all__ = ["__version__"]

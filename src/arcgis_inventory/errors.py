"""Exception types.

Failures during a crawl are results, not noise: most of them land in the
``crawl_error`` table and the run continues with status ``partial``. Only
configuration problems and programmer errors abort.
"""

from __future__ import annotations

__all__ = [
    "ArcgisInventoryError",
    "ConfigError",
    "FixtureMissingError",
    "PortalError",
    "RateLimitedError",
    "SchemaError",
]


class ArcgisInventoryError(Exception):
    """Base class for everything this package raises."""


class ConfigError(ArcgisInventoryError):
    """Missing or contradictory configuration. Aborts before any network call."""


class SchemaError(ArcgisInventoryError):
    """The database on disk is not a shape this version can work with."""


class PortalError(ArcgisInventoryError):
    """The portal returned an error. Usually recorded, not raised to the top."""

    def __init__(self, message: str, *, url: str | None = None, status: int | None = None) -> None:
        super().__init__(message)
        self.url = url
        self.status = status


class RateLimitedError(PortalError):
    """The portal asked us to slow down. Retried with backoff by the transport."""


class FixtureMissingError(ArcgisInventoryError):
    """A fixture transport was asked for a URL it has no file for.

    Loud on purpose. A silently-empty response makes a broken crawler look like
    a clean org, which is the worst possible test outcome.
    """

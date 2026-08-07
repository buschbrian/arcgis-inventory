"""Configuration --- environment variables and user-supplied files only.

No organization's name, item ids, service URLs, domains, layer names, or
business rules appear anywhere in this repo. Everything site-specific arrives
through the environment or a config file the user writes. See ``env.example``.

Credentials are never persisted: not to the database, not to logs, not to
exports. This module is the only place they exist in memory.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigError

__all__ = ["PortalConfig", "RuntimeConfig", "load_config"]

ENV_PREFIX = "ARCGIS_"


@dataclass(slots=True)
class PortalConfig:
    """How to reach one portal, and as whom."""

    url: str
    username: str | None = None
    password: str | None = field(default=None, repr=False)
    token: str | None = field(default=None, repr=False)
    referer: str = "arcgis-inventory"

    # Enterprise deployments behind an internal CA are the common case, not the
    # exception. Turning verification off entirely stays possible but loud.
    verify_ssl: bool = True
    ca_bundle: str | None = None

    @property
    def is_anonymous(self) -> bool:
        return not (self.token or (self.username and self.password))

    def __repr__(self) -> str:  # pragma: no cover - defensive against log leaks
        return f"PortalConfig(url={self.url!r}, username={self.username!r}, auth=<redacted>)"


@dataclass(slots=True)
class RuntimeConfig:
    """Everything that is not a credential."""

    portal: PortalConfig
    database: Path = Path("output/inventory.sqlite")
    output_dir: Path = Path("output")

    timeout_seconds: float = 30.0
    max_retries: int = 3
    # Requests per second, applied across the whole crawl. Portals rate-limit,
    # and a 5,000-item org is somebody's production system.
    max_rps: float = 8.0
    page_size: int = 100

    # Probe service endpoints for reachability. Off by default: it multiplies
    # the request count and hits hosts outside the portal.
    probe_services: bool = False

    rules_dir: Path | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def load_config(*, env: dict[str, str] | None = None) -> RuntimeConfig:
    """Build a :class:`RuntimeConfig` from the environment.

    Raises :class:`ConfigError` rather than guessing. A crawl pointed at the
    wrong portal is worse than a crawl that refuses to start.
    """
    source = dict(os.environ if env is None else env)

    url = _get(source, "PORTAL_URL")
    if not url:
        raise ConfigError(
            f"{ENV_PREFIX}PORTAL_URL is not set. Copy env.example, fill it in, and export it "
            "--- this tool never stores a portal URL in the repo."
        )

    username = _get(source, "USERNAME")
    password = _get(source, "PASSWORD")
    token = _get(source, "TOKEN")

    if password and not username:
        raise ConfigError(f"{ENV_PREFIX}PASSWORD is set without {ENV_PREFIX}USERNAME.")
    if username and not password and not token:
        raise ConfigError(
            f"{ENV_PREFIX}USERNAME is set but neither {ENV_PREFIX}PASSWORD nor "
            f"{ENV_PREFIX}TOKEN is. Refusing to fall back to an anonymous crawl, which would "
            "silently under-report every non-public item."
        )

    portal = PortalConfig(
        url=url.rstrip("/"),
        username=username,
        password=password,
        token=token,
        verify_ssl=_bool(source, "VERIFY_SSL", default=True),
        ca_bundle=_get(source, "CA_BUNDLE"),
    )

    return RuntimeConfig(
        portal=portal,
        database=Path(_get(source, "DB") or "output/inventory.sqlite"),
        output_dir=Path(_get(source, "OUTPUT_DIR") or "output"),
        timeout_seconds=_float(source, "TIMEOUT", 30.0),
        max_retries=int(_float(source, "MAX_RETRIES", 3)),
        max_rps=_float(source, "MAX_RPS", 8.0),
        page_size=int(_float(source, "PAGE_SIZE", 100)),
        probe_services=_bool(source, "PROBE_SERVICES", default=False),
        rules_dir=Path(p) if (p := _get(source, "RULES_DIR")) else None,
    )


def _get(source: dict[str, str], name: str) -> str | None:
    value = source.get(ENV_PREFIX + name, "").strip()
    return value or None


def _bool(source: dict[str, str], name: str, *, default: bool) -> bool:
    raw = _get(source, name)
    if raw is None:
        return default
    if raw.lower() in ("1", "true", "yes", "on"):
        return True
    if raw.lower() in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{ENV_PREFIX}{name} must be a boolean, got {raw!r}")


def _float(source: dict[str, str], name: str, default: float) -> float:
    raw = _get(source, name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{ENV_PREFIX}{name} must be a number, got {raw!r}") from exc

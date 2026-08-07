"""Canonical URL normalization.

One function, used everywhere, or edges will silently fail to dedupe and the
dependency graph will be wrong in a way that is hard to see. Fifty lines of code
that everything downstream depends on --- hence the property-based tests in
``tests/test_urls.py``.

Rules:

* lowercase scheme and host; strip default ports; strip trailing slash
* preserve case in the path after ``/rest/services/`` --- Enterprise paths are
  case-sensitive in practice
* separate the layer index from the service URL and return it alongside, rather
  than keeping it in the URL. Otherwise ``.../FeatureServer/0`` and
  ``.../FeatureServer/3`` become two unrelated graph nodes and impact analysis
  breaks.
* record ``http`` vs ``https`` separately but normalize the stored URL to a
  single scheme, so the same service reached both ways is one node
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

__all__ = ["SERVICE_TYPES", "NormalizedUrl", "normalize_url", "same_service", "service_root"]

# ArcGIS REST service endpoint types. Matched case-insensitively; the canonical
# spelling here is what lands in ``resource.service_type``.
SERVICE_TYPES: tuple[str, ...] = (
    "FeatureServer",
    "MapServer",
    "ImageServer",
    "GeocodeServer",
    "GPServer",
    "GeometryServer",
    "VectorTileServer",
    "SceneServer",
    "StreamServer",
    "NAServer",
)

_SERVICE_TYPE_BY_LOWER = {s.lower(): s for s in SERVICE_TYPES}

_DEFAULT_PORTS = {"http": 80, "https": 443}

_DIGITS = re.compile(r"^\d+$")


@dataclass(frozen=True, slots=True)
class NormalizedUrl:
    """The canonical identity of a service endpoint, plus what normalizing
    threw away.

    ``url`` is what goes in ``resource.url_normalized``. ``layer_index`` goes in
    ``edge.detail_json`` --- never back into the URL.
    """

    url: str
    host: str
    is_https: bool
    layer_index: int | None = None
    service_type: str | None = None

    def __str__(self) -> str:  # pragma: no cover - convenience only
        return self.url


def normalize_url(raw: str) -> NormalizedUrl:
    """Normalize ``raw`` to a canonical endpoint identity.

    Idempotent: ``normalize_url(normalize_url(u).url).url == normalize_url(u).url``.

    Raises ``ValueError`` on input with no host --- a relative or malformed URL
    reaching this function means an extractor bug, and returning something
    plausible would hide it.
    """
    candidate = raw.strip()
    if not candidate:
        raise ValueError("cannot normalize an empty URL")

    # Protocol-relative URLs appear in older web map JSON.
    if candidate.startswith("//"):
        candidate = "https:" + candidate

    parts = urlsplit(candidate)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"unsupported URL scheme {parts.scheme!r} in {raw!r}")
    if not parts.hostname:
        raise ValueError(f"URL has no host: {raw!r}")

    host = parts.hostname.lower()
    port = parts.port
    authority = host
    if port is not None and port != _DEFAULT_PORTS[scheme]:
        authority = f"{host}:{port}"

    path, layer_index, service_type = _split_path(parts.path)

    # Query and fragment are never part of an endpoint's identity --- `?f=json`
    # and a token parameter must not create a second node.
    canonical = f"https://{authority}{path}"

    return NormalizedUrl(
        url=canonical,
        host=host,
        is_https=(scheme == "https"),
        layer_index=layer_index,
        service_type=service_type,
    )


def _split_path(raw_path: str) -> tuple[str, int | None, str | None]:
    """Return ``(path, layer_index, service_type)`` for a REST path."""
    segments = [s for s in raw_path.split("/") if s]
    if not segments:
        return "", None, None

    # Case handling pivots on `rest/services`. Before it: server plumbing,
    # case-insensitive, safe to lowercase. After it: folder and service names
    # that Enterprise treats as case-sensitive.
    pivot = _find_pivot(segments)
    normalized = [s.lower() for s in segments[:pivot]] + list(segments[pivot:])

    # A trailing all-digits segment is a layer/table index, not part of the
    # service identity --- but only when a service type precedes it.
    layer_index: int | None = None
    service_at = _last_service_index(normalized)
    if (
        service_at is not None
        and len(normalized) == service_at + 2
        and _DIGITS.match(normalized[-1])
    ):
        layer_index = int(normalized.pop())

    service_at = _last_service_index(normalized)
    service_type = (
        _SERVICE_TYPE_BY_LOWER[normalized[service_at].lower()] if service_at is not None else None
    )
    if service_at is not None:
        # Canonicalize the service-type segment's spelling so `featureserver`
        # and `FeatureServer` are one node.
        normalized[service_at] = service_type or normalized[service_at]

    return "/" + "/".join(normalized) if normalized else "", layer_index, service_type


def _find_pivot(segments: list[str]) -> int:
    """Index of the first segment whose case must be preserved."""
    lowered = [s.lower() for s in segments]
    for i in range(len(lowered) - 1):
        if lowered[i] == "rest" and lowered[i + 1] == "services":
            return i + 2
    return len(segments)  # no REST path: lowercase the lot


def _last_service_index(segments: list[str]) -> int | None:
    for i in range(len(segments) - 1, -1, -1):
        if segments[i].lower() in _SERVICE_TYPE_BY_LOWER:
            return i
    return None


def same_service(a: str, b: str) -> bool:
    """True when two URLs denote the same endpoint, ignoring scheme and layer."""
    return normalize_url(a).url == normalize_url(b).url


def service_root(raw: str) -> NormalizedUrl:
    """Normalize, then drop anything hanging below the service itself.

    A print or geoprocessing configuration points at a *task*
    (``.../GPServer/Export%20Web%20Map%20Task``), but the thing an organization
    depends on --- and the thing that breaks --- is the service. Without this,
    every GP task becomes its own graph node and impact analysis fragments.

    A URL with no recognizable service type is returned unchanged; guessing
    where to truncate would be worse than not truncating.
    """
    normalized = normalize_url(raw)
    if normalized.service_type is None:
        return normalized

    segments = normalized.url.split("//", 1)[1].split("/")
    for i, segment in enumerate(segments):
        if segment.lower() in _SERVICE_TYPE_BY_LOWER:
            trimmed = "https://" + "/".join(segments[: i + 1])
            return NormalizedUrl(
                url=trimmed,
                host=normalized.host,
                is_https=normalized.is_https,
                layer_index=normalized.layer_index,
                service_type=normalized.service_type,
            )
    return normalized  # pragma: no cover - service_type implies a match

"""The HTTP boundary.

Everything above this module --- pagination, classification, edge extraction,
rules --- is the same code in tests and in production. Tests that mock at a
higher level than this stop testing the thing that actually breaks.

Two implementations of one protocol:

``HttpTransport``
    Raw ArcGIS REST over httpx. Deliberately not the ``arcgis`` Python package:
    this has to pip-install on a locked-down workstation with no conda.

``FixtureTransport``
    Resolves a URL to a file in the synthetic fixture org. No network, no
    credentials, so CI passes for anyone and no real organization's structure
    can leak into the repo through tests or sample output.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

import httpx

from .errors import FixtureMissingError, PortalError, RateLimitedError

__all__ = [
    "FixtureTransport",
    "HttpTransport",
    "Response",
    "Transport",
    "fixture_service_filename",
]

# Service paths become one flat filename rather than a directory tree. A real
# REST path can be deep enough that `<repo>/tests/fixtures/.../<path>.json`
# exceeds the Windows 260-character limit, at which point git cannot even read
# the directory --- and Windows is exactly where this tool's users are.
_MAX_SLUG = 80


def fixture_service_filename(service_path: str) -> str:
    """Flatten a REST service path into a single, bounded filename component."""
    slug = service_path.strip("/").replace("/", "__")
    if len(slug) > _MAX_SLUG:
        digest = hashlib.sha256(slug.encode("utf-8")).hexdigest()[:12]
        slug = f"{slug[:_MAX_SLUG]}~{digest}"
    return f"{slug}.json"


@dataclass(frozen=True, slots=True)
class Response:
    """A parsed portal response plus the bits the crawler records on failure."""

    url: str
    status: int
    data: Any

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


@runtime_checkable
class Transport(Protocol):
    """Fetch a JSON document from a URL."""

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> Response:
        """Return the parsed response.

        Transport-level failures (connection refused, TLS, timeout, HTTP error)
        raise :class:`~arcgis_inventory.errors.PortalError`. An ArcGIS
        application-level error --- HTTP 200 with an ``error`` object in the
        body, which the REST API does constantly --- is returned as-is; the
        caller decides whether it is a crawl error or a finding.
        """
        ...

    def close(self) -> None: ...


class HttpTransport:
    """Live portal access."""

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        max_retries: int = 3,
        max_rps: float = 8.0,
        verify: bool | str = True,
        referer: str = "arcgis-inventory",
        token: str | None = None,
    ) -> None:
        self._max_retries = max_retries
        self._min_interval = 1.0 / max_rps if max_rps > 0 else 0.0
        self._last_request = 0.0
        self._token = token
        self._client = httpx.Client(
            timeout=timeout,
            verify=verify,
            follow_redirects=True,
            headers={"Referer": referer, "User-Agent": "arcgis-inventory"},
        )

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> Response:
        query: dict[str, Any] = {"f": "json", **(params or {})}
        if self._token and "token" not in query:
            query["token"] = self._token

        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            self._throttle()
            try:
                reply = self._client.get(url, params=query)
            except httpx.HTTPError as exc:  # timeout, TLS, connection
                last_exc = exc
                if attempt == self._max_retries:
                    raise PortalError(str(exc), url=url) from exc
                self._backoff(attempt)
                continue

            if reply.status_code == 429 or reply.status_code >= 500:
                if attempt == self._max_retries:
                    exc_type = RateLimitedError if reply.status_code == 429 else PortalError
                    raise exc_type(
                        f"HTTP {reply.status_code} from {url}",
                        url=url,
                        status=reply.status_code,
                    )
                self._backoff(attempt, retry_after=reply.headers.get("Retry-After"))
                continue

            return Response(url=url, status=reply.status_code, data=_parse(reply))

        raise PortalError(str(last_exc), url=url)  # pragma: no cover - unreachable

    def _throttle(self) -> None:
        if self._min_interval <= 0:
            return
        wait = self._min_interval - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    @staticmethod
    def _backoff(attempt: int, retry_after: str | None = None) -> None:
        if retry_after:
            try:
                time.sleep(min(float(retry_after), 60.0))
                return
            except ValueError:
                pass
        time.sleep(min(2.0**attempt, 30.0))

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HttpTransport:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _parse(reply: httpx.Response) -> Any:
    try:
        return reply.json()
    except ValueError as exc:
        # A portal returning HTML where JSON was asked for almost always means
        # an auth redirect or a WAF. Say so rather than dying in a parser.
        snippet = reply.text[:200].replace("\n", " ")
        raise PortalError(
            f"expected JSON from {reply.request.url}, got {reply.headers.get('content-type')}: "
            f"{snippet}",
            url=str(reply.request.url),
            status=reply.status_code,
        ) from exc


class FixtureTransport:
    """Serve the synthetic fixture org from disk.

    Resolution mirrors the fixture's on-disk layout, which is organized by what
    a file *is* rather than by URL shape, so a human reviewing a fixture diff
    can find things::

        portal/self.json      <- /sharing/rest/portals/self
        portal/users.json     <- /sharing/rest/community/users
        search/page-N.json    <- /sharing/rest/search?start=...
        items/<id>.json       <- /sharing/rest/content/items/<id>
        items/<id>.data.json  <- /sharing/rest/content/items/<id>/data
        services/<host>/<slug>.json   <- any /rest/services/... URL, path
                                         flattened (see fixture_service_filename)

    An unmapped URL is a hard failure, never an empty result: a silently-empty
    response makes a broken crawler look like a clean org.

    ``overlay`` selects a subdirectory (``run2``) that is consulted first, so
    the second crawl can redefine only what changed.
    """

    def __init__(
        self,
        root: Path | str,
        *,
        strict: bool = True,
        overlay: str | None = None,
        anonymous: bool = False,
    ) -> None:
        self.root = Path(root)
        self.strict = strict
        self.overlay = overlay
        # When set, a `<name>.anon.json` file wins if one exists --- the fixture's
        # way of saying "this URL answers differently without credentials",
        # which is what an unauthenticated sharing probe is testing for.
        self.anonymous = anonymous
        self.requested: list[str] = []

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> Response:
        self.requested.append(url)
        path = self.resolve(url, params)
        if path is None or not path.is_file():
            if self.strict:
                raise FixtureMissingError(
                    f"no fixture for {url} (params={params}); expected {path}. "
                    "Add it to the fixture spec --- an empty response here would make a "
                    "broken crawler look like a clean org."
                )
            return Response(url=url, status=404, data=None)

        text = path.read_text(encoding="utf-8")
        try:
            data = json.loads(text)
        except ValueError as exc:
            # Deliberate in the fixture (case 14). Surfaced the same way
            # HttpTransport surfaces a portal that returns garbage, so the
            # crawler's error path is exercised by the same code.
            raise PortalError(
                f"invalid JSON in fixture {path}: {exc}", url=url, status=200
            ) from exc
        return Response(url=url, status=200, data=data)

    def resolve(self, url: str, params: dict[str, Any] | None = None) -> Path | None:
        """Map a URL (plus paging params) onto a fixture file."""
        relative = self._relative_path(url, params)
        if relative is None:
            return None

        bases = [self.root / self.overlay, self.root] if self.overlay else [self.root]
        for base in bases:
            if self.anonymous:
                anon = (base / relative).with_name(f"{relative.stem}.anon.json")
                if anon.is_file():
                    return anon
            candidate = base / relative
            if candidate.is_file():
                return candidate
        return self.root / relative  # reported as missing by the caller

    def _relative_path(self, url: str, params: dict[str, Any] | None) -> Path | None:
        parts = urlsplit(url)
        segments = [s for s in parts.path.split("/") if s]
        if not segments:
            return None

        rest = self._after_rest(segments)

        # A service URL also contains `/rest/`, so disambiguate on what follows.
        if rest is not None and rest[:1] != ["services"]:
            return self._portal_path(rest, params)

        host = parts.hostname or "unknown-host"
        return Path("services") / host / fixture_service_filename("/".join(segments))

    @staticmethod
    def _after_rest(segments: list[str]) -> list[str] | None:
        for i, segment in enumerate(segments):
            if segment.lower() == "rest":
                return segments[i + 1 :]
        return None

    def _portal_path(self, rest: list[str], params: dict[str, Any] | None) -> Path | None:
        if rest[:1] == ["search"]:
            return Path("search") / f"page-{self._page_number(params)}.json"
        if rest[:2] == ["portals", "self"]:
            return Path("portal") / "self.json"
        if rest[:1] == ["community"] and rest[1:2] in (["users"], ["groups"]):
            return Path("portal") / f"{rest[1]}.json"
        if rest[:2] == ["content", "items"] and len(rest) >= 3:
            suffix = ".data.json" if rest[3:4] == ["data"] else ".json"
            return Path("items") / f"{rest[2]}{suffix}"
        return None

    def _page_number(self, params: dict[str, Any] | None) -> int:
        start = int((params or {}).get("start", 1))
        num = int((params or {}).get("num", 100)) or 100
        return (start - 1) // num + 1

    def close(self) -> None:
        return None

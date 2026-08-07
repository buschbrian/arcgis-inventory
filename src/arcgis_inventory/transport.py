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

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

import httpx

from .errors import FixtureMissingError, PortalError, RateLimitedError

__all__ = ["FixtureTransport", "HttpTransport", "Response", "Transport"]


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

    Resolution is deliberately dumb and inspectable: the URL's host and path
    become a file path under ``root``. An unmapped URL is a hard failure ---
    never an empty result.
    """

    def __init__(self, root: Path | str, *, strict: bool = True) -> None:
        self.root = Path(root)
        self.strict = strict
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
        return Response(url=url, status=200, data=json.loads(path.read_text(encoding="utf-8")))

    def resolve(self, url: str, params: dict[str, Any] | None = None) -> Path | None:
        """Map a URL (plus paging params) onto a fixture file."""
        parts = urlsplit(url)
        segments = [s for s in parts.path.split("/") if s]

        # Paginated search: /sharing/rest/search?start=1&num=100 -> search/page-N.json
        if segments[-1:] == ["search"]:
            page = self._page_number(params)
            return self.root / "search" / f"page-{page}.json"

        base = self.root / parts.hostname if parts.hostname else self.root
        if not segments:
            return None
        return (base / "/".join(segments)).with_suffix(".json")

    def _page_number(self, params: dict[str, Any] | None) -> int:
        start = int((params or {}).get("start", 1))
        num = int((params or {}).get("num", 100)) or 100
        return (start - 1) // num + 1

    def close(self) -> None:
        return None

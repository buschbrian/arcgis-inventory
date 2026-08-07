"""The crawl.

One paginated pass over the portal's items, classifying as it goes and keeping
every raw response. Dependency resolution, scanning, and recommendations are
separate subcommands over the same stored data --- splitting the *crawl* would
mean hitting someone's portal four times to answer four questions about the
same items.

Everything that can fail per-item is caught and recorded. A crawl that dies on
item 400 of 5,000 is worse than useless; a crawl that reports `partial` and
tells you which 12 items it could not read is a result.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from . import __version__
from .classify import classify, is_data_bearing
from .db import store
from .errors import PortalError
from .transport import Transport

__all__ = ["CrawlResult", "PortalClient", "crawl_inventory", "scoped_query"]


def scoped_query(query: str, org_id: str | None) -> str:
    """Confine a search to one organization.

    This matters most in the case that looks safest. An **anonymous** search
    against ArcGIS Online with no query returns every public item in ArcGIS
    Online --- the entire worldwide public catalogue, not your organization's.
    The result is a meaningless inventory built by hammering Esri's servers for
    hours, and nothing about the request looks wrong while it happens.

    Enterprise scopes anonymous search to the portal already, but pinning the
    org id there too costs nothing and makes the run reproducible.

    A caller who writes their own `orgid:` is left alone.
    """
    query = query.strip()
    if not org_id or "orgid:" in query.lower():
        return query
    scope = f"orgid:{org_id}"
    return f"{scope} AND ({query})" if query else scope


@dataclass(slots=True)
class CrawlResult:
    run_id: int
    portal_id: int
    item_count: int
    error_count: int
    status: str
    platforms: dict[str, int] = field(default_factory=dict)


class PortalClient:
    """URL construction and paging for the ArcGIS REST API."""

    def __init__(self, transport: Transport, portal_url: str, *, page_size: int = 100) -> None:
        self.transport = transport
        self.portal_url = portal_url.rstrip("/")
        self.page_size = page_size

    @property
    def rest(self) -> str:
        return f"{self.portal_url}/sharing/rest"

    def portal_self(self) -> dict[str, Any]:
        return dict(self.transport.get_json(f"{self.rest}/portals/self").data or {})

    def usernames(self) -> set[str] | None:
        """Every account that still exists, or None if the list is unreadable.

        None matters: it is the difference between 'this owner is gone' and 'we
        could not check', and `owner_exists` has three states for that reason.
        """
        try:
            data = self.transport.get_json(f"{self.rest}/community/users", {"num": 1000}).data
        except PortalError:
            return None
        if not isinstance(data, dict) or "users" not in data:
            return None
        return {u["username"] for u in data["users"] if "username" in u}

    def search(self, query: str = "") -> list[dict[str, Any]]:
        """Walk every page of results.

        `nextStart` is authoritative; a portal will happily disagree with
        arithmetic on `start + num` when items change mid-crawl.
        """
        items: list[dict[str, Any]] = []
        start = 1
        seen_starts: set[int] = set()

        while start > 0 and start not in seen_starts:
            seen_starts.add(start)
            page = self.transport.get_json(
                f"{self.rest}/search",
                {"q": query, "start": start, "num": self.page_size, "sortField": "created"},
            ).data
            if not isinstance(page, dict):
                break
            items.extend(page.get("results") or [])
            start = int(page.get("nextStart", -1))

        return items

    def item_data(self, item_id: str) -> Any:
        return self.transport.get_json(f"{self.rest}/content/items/{item_id}/data").data


def crawl_inventory(
    conn: sqlite3.Connection,
    client: PortalClient,
    *,
    query: str = "",
    fetch_data: bool = True,
) -> CrawlResult:
    """Crawl every item into the database and return what happened."""
    started_at = datetime.now(UTC).isoformat(timespec="seconds")

    portal_info = _portal_info(client)
    portal_id = store.upsert_portal(conn, **portal_info)
    effective_query = scoped_query(query, portal_info.get("org_id"))

    run_id = store.start_run(
        conn,
        portal_id=portal_id,
        mode="crawl",
        tool_version=__version__,
        scope={
            "query": query,
            "effective_query": effective_query,
            "page_size": client.page_size,
            "fetch_data": fetch_data,
        },
    )

    known_users = client.usernames()
    if known_users is None:
        store.record_error(
            conn,
            run_id=run_id,
            phase="user",
            message="user list unreadable; owner_exists left unknown for every item",
        )

    errors = 0
    platforms: dict[str, int] = {}

    try:
        items = client.search(effective_query)
    except PortalError as exc:
        store.record_error(
            conn,
            run_id=run_id,
            phase="search",
            message=str(exc),
            target_url=exc.url,
            http_status=exc.status,
        )
        store.finish_run(conn, run_id, status="failed", item_count=0, error_count=1)
        conn.commit()
        return CrawlResult(run_id, portal_id, 0, 1, "failed")

    for item in items:
        data, status, error = _fetch_data(client, item) if fetch_data else (None, "absent", None)
        if error is not None:
            errors += 1

        write = store.ResourceWrite(
            item=item,
            classification=classify(item, data, status),
            raw_data=data if status == "ok" else None,
            raw_data_fetched=status == "ok",
            owner_exists=None if known_users is None else item.get("owner") in known_users,
        )
        resource_id = store.upsert_item(conn, portal_id=portal_id, run_id=run_id, write=write)
        platforms[write.classification.platform] = (
            platforms.get(write.classification.platform, 0) + 1
        )

        store.record_usage(
            conn,
            resource_id=resource_id,
            run_id=run_id,
            num_views=item.get("numViews"),
            captured_at=started_at,
        )

        if error is not None:
            store.record_error(
                conn,
                run_id=run_id,
                resource_id=resource_id,
                phase="item_data",
                message=error["message"],
                target_url=error.get("url"),
                http_status=error.get("status"),
            )

    status = "partial" if errors else "complete"
    store.finish_run(conn, run_id, status=status, item_count=len(items), error_count=errors)
    conn.commit()
    return CrawlResult(run_id, portal_id, len(items), errors, status, platforms)


def _portal_info(client: PortalClient) -> dict[str, Any]:
    try:
        info = client.portal_self()
    except PortalError:
        info = {}
    return {
        "url": client.portal_url,
        # `isPortal` is the portal's own answer to "am I Enterprise".
        "kind": "enterprise" if info.get("isPortal") else "online",
        "org_id": info.get("id"),
        "name": info.get("name"),
        "version": info.get("currentVersion"),
    }


def _fetch_data(
    client: PortalClient, item: dict[str, Any]
) -> tuple[Any, str, dict[str, Any] | None]:
    """Return ``(data, status, error)`` for one item's data document.

    Three outcomes, and they are genuinely different:

    ``absent``  the item type has no JSON data document. Not a problem.
    ``ok``      fetched and parsed.
    ``error``   unreadable --- malformed, forbidden, or gone. Recorded, and it
                lowers the confidence of anything classified without it.
    """
    if not is_data_bearing(item.get("type")):
        return None, "absent", None

    item_id = item.get("id", "")
    try:
        data = client.item_data(item_id)
    except PortalError as exc:
        return None, "error", {"message": str(exc), "url": exc.url, "status": exc.status}

    # The REST API answers HTTP 200 with an error object for a permission
    # failure. Treating that as data would classify the item off a document
    # that says "you may not read this".
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        err = data["error"]
        return (
            None,
            "error",
            {
                "message": err.get("message", "portal returned an error object"),
                "url": f"{client.rest}/content/items/{item_id}/data",
                "status": err.get("code"),
            },
        )

    return data, "ok", None

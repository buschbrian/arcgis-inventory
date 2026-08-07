"""Writes against the schema.

The one rule this module exists to enforce: **a crawl only ever writes derived
columns.** ``finding.status``, ``recommendation.override_*``, and every column
of ``migration`` are authored by a human and must survive a wipe-and-recrawl.
Nothing here touches them.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..classify import Classification

__all__ = [
    "ResourceWrite",
    "finish_run",
    "record_error",
    "record_usage",
    "start_run",
    "upsert_item",
    "upsert_portal",
]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _dumps(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def upsert_portal(
    conn: sqlite3.Connection,
    *,
    url: str,
    kind: str,
    org_id: str | None = None,
    name: str | None = None,
    version: str | None = None,
) -> int:
    normalized = url.rstrip("/")
    row = conn.execute("SELECT portal_id FROM portal WHERE url = ?", (normalized,)).fetchone()
    if row is not None:
        conn.execute(
            "UPDATE portal SET kind = ?, org_id = ?, name = ?, version = ? WHERE portal_id = ?",
            (kind, org_id, name, version, row["portal_id"]),
        )
        return int(row["portal_id"])

    cursor = conn.execute(
        "INSERT INTO portal (url, kind, org_id, name, version, added_at) VALUES (?,?,?,?,?,?)",
        (normalized, kind, org_id, name, version, _now()),
    )
    return int(cursor.lastrowid or 0)


def start_run(
    conn: sqlite3.Connection,
    *,
    portal_id: int,
    mode: str,
    tool_version: str,
    rules_version: str | None = None,
    scope: dict[str, Any] | None = None,
) -> int:
    cursor = conn.execute(
        "INSERT INTO run (portal_id, started_at, status, mode, tool_version, rules_version, "
        "scope_json) VALUES (?,?,?,?,?,?,?)",
        (portal_id, _now(), "running", mode, tool_version, rules_version, _dumps(scope)),
    )
    return int(cursor.lastrowid or 0)


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    status: str,
    item_count: int,
    error_count: int,
    notes: str | None = None,
) -> None:
    conn.execute(
        "UPDATE run SET finished_at = ?, status = ?, item_count = ?, error_count = ?, notes = ? "
        "WHERE run_id = ?",
        (_now(), status, item_count, error_count, notes, run_id),
    )


@dataclass(slots=True)
class ResourceWrite:
    """Everything a crawl knows about one item."""

    item: dict[str, Any]
    classification: Classification
    raw_data: Any = None
    raw_data_fetched: bool = False
    owner_exists: bool | None = None


def upsert_item(
    conn: sqlite3.Connection,
    *,
    portal_id: int,
    run_id: int,
    write: ResourceWrite,
) -> int:
    """Insert or refresh one item resource.

    On re-crawl this updates derived columns and advances ``last_seen_run``.
    ``first_seen_run`` is never rewritten --- 'when did this appear' is a fact
    about the portal's history, not about the latest run.
    """
    item = write.item
    item_id = item["id"]
    cls = write.classification

    columns: dict[str, Any] = {
        "title": item.get("title"),
        "item_type": item.get("type"),
        "type_keywords": _dumps(item.get("typeKeywords")),
        "owner": item.get("owner"),
        "owner_exists": None if write.owner_exists is None else int(write.owner_exists),
        "folder_id": item.get("ownerFolder"),
        "created_at": _iso(item.get("created")),
        "modified_at": _iso(item.get("modified")),
        "access": item.get("access"),
        "shared_groups": _dumps(item.get("groupIds")),
        "num_views": item.get("numViews"),
        "size_bytes": item.get("size"),
        "tags": _dumps(item.get("tags")),
        "snippet": item.get("snippet"),
        "url": item.get("url"),
        "platform": cls.platform,
        "platform_confidence": cls.confidence,
        "platform_evidence": _dumps(cls.evidence),
        "raw_json": _dumps(item),
        "last_seen_run": run_id,
    }
    # Retaining the raw document is what makes `reprocess` possible: crawling a
    # 5,000-item org takes a long time and hammers someone's portal, while
    # re-running the rules over stored JSON takes seconds. Only overwrite it
    # when this run actually fetched one.
    if write.raw_data_fetched:
        columns["raw_data_json"] = _dumps(write.raw_data)
        columns["raw_fetched_run"] = run_id

    existing = conn.execute(
        "SELECT resource_id FROM resource WHERE portal_id = ? AND item_id = ?",
        (portal_id, item_id),
    ).fetchone()

    if existing is not None:
        assignments = ", ".join(f"{name} = ?" for name in columns)
        conn.execute(
            f"UPDATE resource SET {assignments} WHERE resource_id = ?",
            (*columns.values(), existing["resource_id"]),
        )
        return int(existing["resource_id"])

    columns["portal_id"] = portal_id
    columns["kind"] = "item"
    columns["item_id"] = item_id
    columns["first_seen_run"] = run_id
    names = ", ".join(columns)
    placeholders = ", ".join("?" for _ in columns)
    cursor = conn.execute(
        f"INSERT INTO resource ({names}) VALUES ({placeholders})", tuple(columns.values())
    )
    return int(cursor.lastrowid or 0)


def record_usage(
    conn: sqlite3.Connection,
    *,
    resource_id: int,
    run_id: int,
    num_views: int | None,
    captured_at: str,
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO usage_snapshot (resource_id, run_id, num_views, captured_at) "
        "VALUES (?,?,?,?)",
        (resource_id, run_id, num_views, captured_at),
    )


def record_error(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    phase: str,
    message: str,
    resource_id: int | None = None,
    target_url: str | None = None,
    http_status: int | None = None,
) -> None:
    """Failures are results, not noise.

    A service that returns 403 during a crawl usually means an app depends on
    something the crawling account cannot see --- very often the same thing the
    public cannot see.
    """
    conn.execute(
        "INSERT INTO crawl_error (run_id, resource_id, target_url, phase, http_status, message, "
        "occurred_at) VALUES (?,?,?,?,?,?,?)",
        (run_id, resource_id, target_url, phase, http_status, message, _now()),
    )


def _iso(epoch_millis: Any) -> str | None:
    """Portal timestamps are epoch milliseconds; the schema stores ISO-8601."""
    if not isinstance(epoch_millis, (int, float)):
        return None
    return datetime.fromtimestamp(epoch_millis / 1000, tz=UTC).isoformat(timespec="seconds")

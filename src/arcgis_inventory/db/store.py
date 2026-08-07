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
    "FindingWrite",
    "ResourceWrite",
    "finish_run",
    "record_error",
    "record_usage",
    "resolve_absent_findings",
    "set_endpoint_sharing",
    "start_run",
    "upsert_edge",
    "upsert_endpoint",
    "upsert_finding",
    "upsert_item",
    "upsert_portal",
    "upsert_recommendation",
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


def upsert_endpoint(
    conn: sqlite3.Connection,
    *,
    portal_id: int,
    run_id: int,
    url_normalized: str,
    host: str | None = None,
    is_https: bool | None = None,
    service_type: str | None = None,
) -> int:
    """Insert or refresh a bare service endpoint --- a node that is not a portal item.

    Many dependencies are not items: a web map can reference a map service by
    URL, a geocoder hosted elsewhere, a print service on another server. One
    node table keeps every graph query from becoming a union.
    """
    existing = conn.execute(
        "SELECT resource_id, is_https FROM resource WHERE portal_id = ? AND url_normalized = ?",
        (portal_id, url_normalized),
    ).fetchone()

    if existing is not None:
        # If the same service is ever referenced over http, the endpoint stays
        # flagged as insecure. One plaintext reference is the finding, and a
        # later https reference does not undo it.
        merged = existing["is_https"]
        if is_https is not None:
            merged = 0 if merged == 0 or not is_https else 1
        conn.execute(
            "UPDATE resource SET host = COALESCE(?, host), is_https = ?, "
            "service_type = COALESCE(?, service_type), last_seen_run = ? WHERE resource_id = ?",
            (host, merged, service_type, run_id, existing["resource_id"]),
        )
        return int(existing["resource_id"])

    cursor = conn.execute(
        "INSERT INTO resource (portal_id, kind, url_normalized, title, host, is_https, "
        "service_type, platform, first_seen_run, last_seen_run) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            portal_id,
            "endpoint",
            url_normalized,
            _endpoint_title(url_normalized),
            host,
            None if is_https is None else int(is_https),
            service_type,
            _PLATFORM_BY_SERVICE.get(service_type or ""),
            run_id,
            run_id,
        ),
    )
    return int(cursor.lastrowid or 0)


def _endpoint_title(url: str) -> str:
    """A readable label for a bare service: 'Parcels (FeatureServer)'.

    Endpoints have no portal title, and a raw URL in a finding is unreadable at
    a glance --- which matters, because these findings get shown to people who
    do not administer the portal.
    """
    parts = [p for p in url.split("/") if p]
    if len(parts) >= 2:
        return f"{parts[-2]} ({parts[-1]})"
    return url


_PLATFORM_BY_SERVICE = {
    "FeatureServer": "feature_service",
    "MapServer": "map_service",
    "ImageServer": "image_service",
    "GeocodeServer": "geocode_service",
    "GPServer": "gp_service",
    "SceneServer": "other",
    "VectorTileServer": "other",
}


def upsert_edge(
    conn: sqlite3.Connection,
    *,
    from_resource: int,
    to_resource: int,
    relation: str,
    source_path: str | None,
    run_id: int,
    detail: dict[str, Any] | None = None,
) -> int:
    """Insert or refresh one dependency.

    Identity is (from, to, relation, source_path). The same layer referenced
    from two different widgets is genuinely two dependencies with two pieces of
    remediation work, so `source_path` belongs in the key.
    """
    existing = conn.execute(
        "SELECT edge_id FROM edge WHERE from_resource = ? AND to_resource = ? AND relation = ? "
        "AND source_path IS ?",
        (from_resource, to_resource, relation, source_path),
    ).fetchone()

    if existing is not None:
        conn.execute(
            "UPDATE edge SET detail_json = ?, last_seen_run = ? WHERE edge_id = ?",
            (_dumps(detail), run_id, existing["edge_id"]),
        )
        return int(existing["edge_id"])

    cursor = conn.execute(
        "INSERT INTO edge (from_resource, to_resource, relation, source_path, detail_json, "
        "first_seen_run, last_seen_run) VALUES (?,?,?,?,?,?,?)",
        (from_resource, to_resource, relation, source_path, _dumps(detail), run_id, run_id),
    )
    return int(cursor.lastrowid or 0)


@dataclass(slots=True)
class FindingWrite:
    """One thing a rule has to say about a resource."""

    fingerprint: str
    rule_id: str
    category: str
    severity: str
    title: str
    resource_id: int | None = None
    detail: str | None = None
    evidence: dict[str, Any] | None = None
    suggested_action: str | None = None


def upsert_finding(
    conn: sqlite3.Connection, *, portal_id: int, run_id: int, write: FindingWrite
) -> int:
    """Insert a finding, or refresh one that is still firing.

    The whole point of the stable fingerprint arrives here: when a finding
    already exists, its **authored triage state is not touched**. Someone marked
    it `wontfix` with a note; re-running the audit must not undo that. Only the
    generated description and the run bookkeeping are refreshed.
    """
    existing = conn.execute(
        "SELECT finding_id FROM finding WHERE fingerprint = ?", (write.fingerprint,)
    ).fetchone()

    if existing is not None:
        conn.execute(
            "UPDATE finding SET severity = ?, title = ?, detail = ?, evidence_json = ?, "
            "suggested_action = ?, last_seen_run = ?, resolved_run = NULL WHERE finding_id = ?",
            (
                write.severity,
                write.title,
                write.detail,
                _dumps(write.evidence),
                write.suggested_action,
                run_id,
                existing["finding_id"],
            ),
        )
        return int(existing["finding_id"])

    cursor = conn.execute(
        "INSERT INTO finding (fingerprint, portal_id, resource_id, rule_id, category, severity, "
        "title, detail, evidence_json, suggested_action, first_seen_run, last_seen_run) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            write.fingerprint,
            portal_id,
            write.resource_id,
            write.rule_id,
            write.category,
            write.severity,
            write.title,
            write.detail,
            _dumps(write.evidence),
            write.suggested_action,
            run_id,
            run_id,
        ),
    )
    return int(cursor.lastrowid or 0)


def upsert_recommendation(
    conn: sqlite3.Connection,
    *,
    resource_id: int,
    run_id: int,
    target: str,
    confidence: str,
    complexity: int | None,
    rules_fired: list[str],
    reasoning: str,
) -> None:
    """Write the generated recommendation, leaving any human override alone.

    Someone deciding "this one is going to an Instant App regardless of what
    the tool thinks" is exactly the judgment the tool cannot make, and rerunning
    the engine must not quietly discard it.
    """
    existing = conn.execute(
        "SELECT resource_id FROM recommendation WHERE resource_id = ?", (resource_id,)
    ).fetchone()

    if existing is not None:
        conn.execute(
            "UPDATE recommendation SET run_id = ?, target = ?, confidence = ?, complexity = ?, "
            "rules_fired = ?, reasoning = ? WHERE resource_id = ?",
            (run_id, target, confidence, complexity, _dumps(rules_fired), reasoning, resource_id),
        )
        return

    conn.execute(
        "INSERT INTO recommendation (resource_id, run_id, target, confidence, complexity, "
        "rules_fired, reasoning) VALUES (?,?,?,?,?,?,?)",
        (resource_id, run_id, target, confidence, complexity, _dumps(rules_fired), reasoning),
    )


def resolve_absent_findings(
    conn: sqlite3.Connection, *, portal_id: int, run_id: int, rule_ids: list[str]
) -> int:
    """Mark findings that stopped firing this run.

    `resolved_run` is *observed* --- the rule no longer matches --- which is a
    different claim from `status = 'fixed'`, which someone asserted. Both are
    worth having, and disagreement between them is interesting.
    """
    if not rule_ids:
        return 0
    placeholders = ", ".join("?" for _ in rule_ids)
    cursor = conn.execute(
        f"UPDATE finding SET resolved_run = ? WHERE portal_id = ? AND resolved_run IS NULL "
        f"AND last_seen_run < ? AND rule_id IN ({placeholders})",
        (run_id, portal_id, run_id, *rule_ids),
    )
    return cursor.rowcount


def set_endpoint_sharing(
    conn: sqlite3.Connection,
    *,
    resource_id: int,
    access: str | None,
    reachable: bool | None,
    http_status: int | None,
) -> None:
    """Record what an unauthenticated request to a service endpoint found."""
    conn.execute(
        "UPDATE resource SET access = ?, reachable = ?, http_status = ? WHERE resource_id = ?",
        (
            access,
            None if reachable is None else int(reachable),
            http_status,
            resource_id,
        ),
    )


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

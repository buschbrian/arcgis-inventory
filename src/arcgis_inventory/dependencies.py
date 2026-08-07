"""Build the dependency graph from stored item data.

No network: this reads `raw_data_json` from the crawl. Extraction lives in
`extract.py` as pure functions; this module resolves what those functions found
onto database rows and keeps the bookkeeping honest.

What it produces is the thing everything downstream needs. `audit-sharing` is
this graph read backwards; impact analysis ("what breaks if this layer moves")
is the index on `edge.to_resource`; and the migration burn-down only means
anything once you know which web maps are shared between apps.

**Endpoint sharing is deliberately left NULL here.** Whether a bare service is
reachable by the public cannot be known from a crawl authenticated as someone
who can see everything --- it takes an unauthenticated probe, which is
`audit-sharing`'s job. Guessing would produce exactly the false clean bill of
health this tool exists to prevent.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

from . import __version__
from .db import store
from .extract import ExtractedEdge, extract_edges

__all__ = ["DependencyResult", "build_dependencies"]


@dataclass(slots=True)
class DependencyResult:
    run_id: int
    portal_id: int
    edge_count: int
    endpoint_count: int
    unresolved: list[tuple[str, str]] = field(default_factory=list)
    relations: dict[str, int] = field(default_factory=dict)


def build_dependencies(
    conn: sqlite3.Connection, *, portal_id: int | None = None
) -> DependencyResult:
    """Resolve every stored item's dependencies into `resource` and `edge` rows."""
    if portal_id is None:
        row = conn.execute("SELECT MIN(portal_id) AS p FROM portal").fetchone()
        portal_id = None if row is None else row["p"]
    if portal_id is None:
        raise ValueError("no portal in this database; run `inventory` first")

    run_id = store.start_run(
        conn,
        portal_id=portal_id,
        mode="reprocess",
        tool_version=__version__,
        scope={"portal_id": portal_id, "stage": "dependencies"},
    )

    items = conn.execute(
        "SELECT resource_id, item_id, title, platform, raw_data_json FROM resource "
        "WHERE portal_id = ? AND kind = 'item' AND raw_data_json IS NOT NULL "
        "ORDER BY resource_id",
        (portal_id,),
    ).fetchall()

    by_item_id = {
        row["item_id"]: row["resource_id"]
        for row in conn.execute(
            "SELECT item_id, resource_id FROM resource WHERE portal_id = ? AND item_id IS NOT NULL",
            (portal_id,),
        )
    }

    endpoints: dict[str, int] = {}
    relations: dict[str, int] = {}
    unresolved: list[tuple[str, str]] = []
    edge_count = 0

    for row in items:
        data = json.loads(row["raw_data_json"])
        for edge in extract_edges(row["platform"], data):
            target = _resolve(
                conn,
                portal_id=portal_id,
                run_id=run_id,
                edge=edge,
                by_item_id=by_item_id,
                endpoints=endpoints,
            )
            if target is None:
                # An app pointing at an item this crawl never saw. Recorded
                # rather than dropped: it is usually either a deleted item or
                # something the crawling account cannot read, and both matter.
                unresolved.append((row["item_id"], edge.item_id or edge.url or "?"))
                store.record_error(
                    conn,
                    run_id=run_id,
                    resource_id=row["resource_id"],
                    phase="item",
                    message=(
                        f"{row['title'] or row['item_id']} references item {edge.item_id}, "
                        f"which is not in this crawl (deleted, or not visible to this account)"
                    ),
                )
                continue

            store.upsert_edge(
                conn,
                from_resource=row["resource_id"],
                to_resource=target,
                relation=edge.relation,
                source_path=edge.source_path,
                run_id=run_id,
                detail=(
                    {"layer_index": edge.layer_index} if edge.layer_index is not None else None
                ),
            )
            edge_count += 1
            relations[edge.relation] = relations.get(edge.relation, 0) + 1

    store.finish_run(
        conn,
        run_id,
        status="complete",
        item_count=len(items),
        error_count=len(unresolved),
        notes=f"{edge_count} edges, {len(endpoints)} endpoints, {len(unresolved)} unresolved",
    )
    conn.commit()
    return DependencyResult(
        run_id=run_id,
        portal_id=portal_id,
        edge_count=edge_count,
        endpoint_count=len(endpoints),
        unresolved=unresolved,
        relations=relations,
    )


def _resolve(
    conn: sqlite3.Connection,
    *,
    portal_id: int,
    run_id: int,
    edge: ExtractedEdge,
    by_item_id: dict[str, int],
    endpoints: dict[str, int],
) -> int | None:
    """Map an extracted edge's target onto a resource id, creating endpoints."""
    if edge.item_id is not None:
        return by_item_id.get(edge.item_id)

    assert edge.url is not None
    if edge.url not in endpoints:
        endpoints[edge.url] = store.upsert_endpoint(
            conn,
            portal_id=portal_id,
            run_id=run_id,
            url_normalized=edge.url,
            host=edge.host,
            is_https=edge.is_https,
            service_type=edge.service_type,
        )
    return endpoints[edge.url]

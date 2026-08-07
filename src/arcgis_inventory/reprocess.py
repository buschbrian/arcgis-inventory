"""Re-derive everything from stored raw documents, with no network at all.

This is the development loop. Classification, scanner, and recommendation rules
change constantly in early development; crawling a 5,000-item org takes a long
time and hammers somebody's production portal, while re-running the rules over
stored JSON takes seconds and can run in CI.

It is also the honest way to answer "did that rule change actually do
anything?" --- reprocess reports what moved, item by item.

Two things it deliberately does **not** do:

* **It does not advance ``last_seen_run``.** That column means "the last crawl
  that observed this in the portal". A reprocess observes nothing. Advancing it
  would make an item that disappeared last month look present again, which is
  exactly the question the column exists to answer.
* **It does not touch authored data.** Same rule as a crawl.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from . import __version__
from .classify import Classification, classify, is_data_bearing
from .db import store

__all__ = ["Reclassification", "ReprocessResult", "reprocess_inventory"]


@dataclass(frozen=True, slots=True)
class Reclassification:
    item_id: str
    title: str | None
    before: str
    after: str

    def __str__(self) -> str:
        return f"{self.title or self.item_id}: {self.before} -> {self.after}"


@dataclass(slots=True)
class ReprocessResult:
    run_id: int
    portal_id: int
    resource_count: int
    changed: list[Reclassification] = field(default_factory=list)
    skipped: int = 0
    platforms: dict[str, int] = field(default_factory=dict)

    @property
    def change_count(self) -> int:
        return len(self.changed)


def reprocess_inventory(
    conn: sqlite3.Connection, *, portal_id: int | None = None
) -> ReprocessResult:
    """Re-classify every stored item and report what moved."""
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
        scope={"portal_id": portal_id},
    )

    rows = conn.execute(
        "SELECT resource_id, item_id, title, item_type, platform, platform_confidence, "
        "raw_json, raw_data_json FROM resource "
        "WHERE portal_id = ? AND kind = 'item' ORDER BY resource_id",
        (portal_id,),
    ).fetchall()

    changed: list[Reclassification] = []
    platforms: dict[str, int] = {}
    skipped = 0

    for row in rows:
        if not row["raw_json"]:
            # Nothing to re-derive from. Never invent a classification here ---
            # a reprocess that quietly downgrades items it has no data for is
            # worse than one that says it skipped them.
            skipped += 1
            continue

        item = json.loads(row["raw_json"])
        data, data_status = _stored_data(row)
        result = classify(item, data, data_status)

        platforms[result.platform] = platforms.get(result.platform, 0) + 1

        before = f"{row['platform']}/{row['platform_confidence']}"
        after = f"{result.platform}/{result.confidence}"
        if before != after:
            changed.append(
                Reclassification(
                    item_id=row["item_id"], title=row["title"], before=before, after=after
                )
            )

        _reclassify(conn, row["resource_id"], result)

    store.finish_run(
        conn,
        run_id,
        status="complete",
        item_count=len(rows) - skipped,
        error_count=0,
        notes=f"{len(changed)} classifications changed, {skipped} skipped (no stored raw item)",
    )
    conn.commit()
    return ReprocessResult(
        run_id=run_id,
        portal_id=portal_id,
        resource_count=len(rows),
        changed=changed,
        skipped=skipped,
        platforms=platforms,
    )


def _stored_data(row: sqlite3.Row) -> tuple[Any, str]:
    """Recover the item's data document and how the crawl left it.

    ``raw_data_json`` is only ever written on a successful fetch, so its absence
    means one of two things and the item type tells them apart: a type with no
    JSON data document was never going to have one, while a data-bearing type
    with nothing stored is one the crawl could not read.

    The one case this reads pessimistically is a crawl run with ``--skip-data``:
    those items reprocess as though their data were unreadable, and drop to
    `guess`. That is the right answer for the wrong reason --- a classification
    made without the data document genuinely is a guess.
    """
    if row["raw_data_json"]:
        return json.loads(row["raw_data_json"]), "ok"
    return None, "error" if is_data_bearing(row["item_type"]) else "absent"


def _reclassify(conn: sqlite3.Connection, resource_id: int, result: Classification) -> None:
    """Update only the derived classification columns.

    Note what is absent: `last_seen_run`, every authored column, and the raw
    documents themselves.
    """
    conn.execute(
        "UPDATE resource SET platform = ?, platform_confidence = ?, platform_evidence = ? "
        "WHERE resource_id = ?",
        (
            result.platform,
            result.confidence,
            json.dumps(result.evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
            resource_id,
        ),
    )

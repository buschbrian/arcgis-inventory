"""Re-deriving classification from stored raw documents.

The property that makes `reprocess` worth having: run it over a crawled
database and nothing changes. If that ever fails, either the crawl is not
storing what it classified from, or classification depends on something the
database does not hold --- and both are bugs that would otherwise surface much
later as unexplainable golden-test drift.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from arcgis_inventory import classify as classify_module
from arcgis_inventory.crawl import PortalClient, crawl_inventory
from arcgis_inventory.db import open_database
from arcgis_inventory.reprocess import reprocess_inventory
from arcgis_inventory.transport import FixtureTransport

FIXTURE = Path(__file__).parent / "fixtures" / "northgate"
PORTAL_URL = "https://northgate.example.gov/portal"


def client(overlay: str | None = None) -> PortalClient:
    return PortalClient(FixtureTransport(FIXTURE, overlay=overlay), PORTAL_URL, page_size=10)


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    connection = open_database(tmp_path / "inv.sqlite")
    yield connection
    connection.close()


def snapshot(conn: sqlite3.Connection) -> dict[str, tuple[Any, ...]]:
    return {
        row["item_id"]: (row["platform"], row["platform_confidence"], row["platform_evidence"])
        for row in conn.execute(
            "SELECT item_id, platform, platform_confidence, platform_evidence FROM resource"
        )
    }


# ---------------------------------------------------------------------------
# The property
# ---------------------------------------------------------------------------


def test_reprocess_reproduces_the_crawl_exactly(conn: sqlite3.Connection) -> None:
    crawl_inventory(conn, client())
    before = snapshot(conn)

    result = reprocess_inventory(conn)

    assert result.resource_count == 30
    assert result.skipped == 0
    assert result.changed == [], [str(c) for c in result.changed]
    assert snapshot(conn) == before


def test_reprocess_cannot_touch_the_network() -> None:
    """Structural, not behavioral: it has no transport to reach for.

    "No network" is a guarantee about what the code *can* do, so assert it
    against the module rather than against one code path that happens not to
    make a request today.
    """
    import inspect

    from arcgis_inventory import reprocess as module

    parameters = inspect.signature(reprocess_inventory).parameters
    assert set(parameters) == {"conn", "portal_id"}

    source = Path(module.__file__).read_text(encoding="utf-8")
    for forbidden in ("httpx", "transport", "Transport", "PortalClient", "requests"):
        assert forbidden not in source, f"reprocess must not reference {forbidden}"


def test_reprocess_is_idempotent(conn: sqlite3.Connection) -> None:
    crawl_inventory(conn, client())
    reprocess_inventory(conn)
    first = snapshot(conn)
    second_result = reprocess_inventory(conn)
    assert second_result.changed == []
    assert snapshot(conn) == first


# ---------------------------------------------------------------------------
# What it is actually for: seeing what a rule change moved
# ---------------------------------------------------------------------------


def test_a_rule_change_is_reported_item_by_item(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    crawl_inventory(conn, client())

    real_classify = classify_module.classify

    def reclassify_wab_as_custom(item, data=None, data_status="absent"):
        result = real_classify(item, data, data_status)
        if result.platform == "web_appbuilder":
            return classify_module.Classification("custom", "likely", {"rule": "test-override"})
        return result

    monkeypatch.setattr("arcgis_inventory.reprocess.classify", reclassify_wab_as_custom)
    result = reprocess_inventory(conn)

    assert result.change_count == 9
    assert all(c.after.startswith("custom/") for c in result.changed)
    assert all(c.before.startswith("web_appbuilder/") for c in result.changed)
    # The report is human-readable, since its whole job is answering "what did
    # my rule change do?".
    assert "->" in str(result.changed[0])
    assert result.changed[0].title


def test_reprocess_records_a_run_of_its_own_kind(conn: sqlite3.Connection) -> None:
    crawl_inventory(conn, client())
    result = reprocess_inventory(conn)

    run = conn.execute("SELECT * FROM run WHERE run_id = ?", (result.run_id,)).fetchone()
    assert run["mode"] == "reprocess"
    assert run["status"] == "complete"
    assert run["error_count"] == 0
    assert "0 classifications changed" in run["notes"]


# ---------------------------------------------------------------------------
# What it must not do
# ---------------------------------------------------------------------------


def test_reprocess_does_not_advance_last_seen_run(conn: sqlite3.Connection) -> None:
    """`last_seen_run` means 'a crawl observed this'. A reprocess observes nothing.

    Advancing it would make an item that disappeared last month look present
    again --- which is the exact question the column exists to answer.
    """
    crawl = crawl_inventory(conn, client())
    before = {
        r["item_id"]: (r["first_seen_run"], r["last_seen_run"])
        for r in conn.execute("SELECT item_id, first_seen_run, last_seen_run FROM resource")
    }

    result = reprocess_inventory(conn)
    assert result.run_id > crawl.run_id  # a new run row exists...

    after = {
        r["item_id"]: (r["first_seen_run"], r["last_seen_run"])
        for r in conn.execute("SELECT item_id, first_seen_run, last_seen_run FROM resource")
    }
    assert after == before  # ...but no resource claims to have been seen in it


def test_a_disappeared_item_stays_disappeared_across_a_reprocess(
    conn: sqlite3.Connection,
) -> None:
    first = crawl_inventory(conn, client())
    crawl_inventory(conn, client(overlay="run2"))
    reprocess_inventory(conn)

    gone = conn.execute(
        "SELECT last_seen_run FROM resource WHERE item_id = 'a0000000000000000000000000000004'"
    ).fetchone()
    assert gone["last_seen_run"] == first.run_id


def test_reprocess_does_not_touch_authored_data(conn: sqlite3.Connection) -> None:
    crawl_inventory(conn, client())
    resource_id = conn.execute(
        "SELECT resource_id FROM resource WHERE item_id = 'a0000000000000000000000000000005'"
    ).fetchone()["resource_id"]
    conn.execute(
        "INSERT INTO migration (resource_id, status, owner_ref, updated_at) "
        "VALUES (?, 'built', 'TICKET-99', '2026-08-06T00:00:00Z')",
        (resource_id,),
    )
    conn.execute(
        "INSERT INTO recommendation (resource_id, run_id, target, confidence, reasoning, "
        "override_target, override_note) VALUES (?, 1, 'experience_builder', 'likely', "
        "'generated', 'instant_app', 'simpler than it looks')",
        (resource_id,),
    )
    conn.commit()

    reprocess_inventory(conn)

    migration = conn.execute(
        "SELECT status, owner_ref FROM migration WHERE resource_id = ?", (resource_id,)
    ).fetchone()
    assert (migration["status"], migration["owner_ref"]) == ("built", "TICKET-99")

    rec = conn.execute(
        "SELECT override_target, override_note FROM recommendation WHERE resource_id = ?",
        (resource_id,),
    ).fetchone()
    assert rec["override_target"] == "instant_app"
    assert rec["override_note"] == "simpler than it looks"


def test_reprocess_does_not_rewrite_the_raw_documents(conn: sqlite3.Connection) -> None:
    crawl_inventory(conn, client())
    before = {
        r["item_id"]: (r["raw_json"], r["raw_data_json"], r["raw_fetched_run"])
        for r in conn.execute(
            "SELECT item_id, raw_json, raw_data_json, raw_fetched_run FROM resource"
        )
    }
    reprocess_inventory(conn)
    after = {
        r["item_id"]: (r["raw_json"], r["raw_data_json"], r["raw_fetched_run"])
        for r in conn.execute(
            "SELECT item_id, raw_json, raw_data_json, raw_fetched_run FROM resource"
        )
    }
    assert after == before


# ---------------------------------------------------------------------------
# Edges
# ---------------------------------------------------------------------------


def test_items_with_no_stored_raw_document_are_skipped_not_invented(
    conn: sqlite3.Connection,
) -> None:
    """A reprocess that downgrades items it has no data for is worse than one
    that says it skipped them."""
    crawl_inventory(conn, client())
    conn.execute(
        "UPDATE resource SET raw_json = NULL WHERE item_id = 'a0000000000000000000000000000001'"
    )
    conn.commit()

    result = reprocess_inventory(conn)
    assert result.skipped == 1

    untouched = conn.execute(
        "SELECT platform, platform_confidence FROM resource "
        "WHERE item_id = 'a0000000000000000000000000000001'"
    ).fetchone()
    assert untouched["platform"] == "web_appbuilder"
    assert untouched["platform_confidence"] == "certain"


def test_unreadable_item_data_still_reprocesses_as_a_guess(conn: sqlite3.Connection) -> None:
    """The two fixture items whose data could not be read keep their honesty."""
    crawl_inventory(conn, client())
    reprocess_inventory(conn)

    for item_id in ("a0000000000000000000000000000014", "a0000000000000000000000000000015"):
        row = conn.execute(
            "SELECT platform_confidence, platform_evidence FROM resource WHERE item_id = ?",
            (item_id,),
        ).fetchone()
        assert row["platform_confidence"] == "guess"
        assert json.loads(row["platform_evidence"])["data_status"] == "error"


def test_reprocess_on_an_empty_database_says_so(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="no portal"):
        reprocess_inventory(conn)

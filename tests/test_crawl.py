"""The crawl, against the fixture org.

`expected/inventory.json` is the assertion: the crawler has to reproduce the
classification the fixture declares, item for item, including how confident it
claims to be.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from arcgis_inventory.crawl import PortalClient, crawl_inventory
from arcgis_inventory.db import open_database, store
from arcgis_inventory.transport import FixtureTransport

FIXTURE = Path(__file__).parent / "fixtures" / "northgate"
PORTAL_URL = "https://northgate.example.gov/portal"
PAGE_SIZE = 10


def expected_inventory() -> list[dict[str, Any]]:
    return json.loads((FIXTURE / "expected" / "inventory.json").read_text(encoding="utf-8"))


def client(overlay: str | None = None) -> PortalClient:
    return PortalClient(FixtureTransport(FIXTURE, overlay=overlay), PORTAL_URL, page_size=PAGE_SIZE)


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    connection = open_database(tmp_path / "inv.sqlite")
    yield connection
    connection.close()


@pytest.fixture
def crawled(conn: sqlite3.Connection):
    result = crawl_inventory(conn, client())
    return conn, result


# ---------------------------------------------------------------------------
# The golden test
# ---------------------------------------------------------------------------


def test_every_item_is_classified_exactly_as_the_fixture_declares(crawled) -> None:
    conn, result = crawled
    assert result.item_count == len(expected_inventory())

    rows = {
        r["item_id"]: r
        for r in conn.execute(
            "SELECT item_id, platform, platform_confidence, item_type, access, owner, num_views "
            "FROM resource WHERE kind = 'item'"
        )
    }

    mismatches = []
    for want in expected_inventory():
        got = rows.get(want["item_id"])
        if got is None:
            mismatches.append((want["key"], "missing from crawl", None))
            continue
        if (got["platform"], got["platform_confidence"]) != (
            want["platform"],
            want["platform_confidence"],
        ):
            mismatches.append(
                (
                    want["key"],
                    f"{want['platform']}/{want['platform_confidence']}",
                    f"{got['platform']}/{got['platform_confidence']}",
                )
            )

    assert not mismatches, "classification drift:\n" + "\n".join(
        f"  {key}: expected {exp}, got {act}" for key, exp, act in mismatches
    )


def test_the_portals_own_type_string_is_kept_verbatim(crawled) -> None:
    conn, _ = crawled
    for want in expected_inventory():
        row = conn.execute(
            "SELECT item_type FROM resource WHERE item_id = ?", (want["item_id"],)
        ).fetchone()
        assert row["item_type"] == want["item_type"]


def test_classification_records_which_signal_fired(crawled) -> None:
    """'How does it know?' has to be answerable per item."""
    conn, _ = crawled
    rows = conn.execute(
        "SELECT item_id, platform, platform_evidence FROM resource WHERE kind = 'item'"
    ).fetchall()
    assert rows
    for row in rows:
        evidence = json.loads(row["platform_evidence"])
        assert evidence, row["item_id"]
        assert "item_type" in evidence

    wab = conn.execute(
        "SELECT platform_evidence FROM resource WHERE item_id = 'a0000000000000000000000000000001'"
    ).fetchone()
    assert json.loads(wab["platform_evidence"])["data_marker"] == "wabVersion"

    legacy = conn.execute(
        "SELECT platform_evidence FROM resource WHERE item_id = 'a0000000000000000000000000000008'"
    ).fetchone()
    markers = json.loads(legacy["platform_evidence"])["data_markers"]
    assert "dojoConfig" in markers
    assert any("js.arcgis.com/3.x" in m for m in markers)


# ---------------------------------------------------------------------------
# Failures are results
# ---------------------------------------------------------------------------


def test_unreadable_items_do_not_stop_the_crawl(crawled) -> None:
    conn, result = crawled
    assert result.status == "partial"
    assert result.error_count == 2
    # The other 28 still landed.
    assert result.item_count == 30

    errors = conn.execute(
        "SELECT resource_id, phase, http_status, message FROM crawl_error ORDER BY error_id"
    ).fetchall()
    assert [e["phase"] for e in errors] == ["item_data", "item_data"]

    by_item = {
        conn.execute(
            "SELECT item_id FROM resource WHERE resource_id = ?", (e["resource_id"],)
        ).fetchone()["item_id"]: e
        for e in errors
    }
    assert set(by_item) == {
        "a0000000000000000000000000000014",  # malformed JSON
        "a0000000000000000000000000000015",  # 403 as an error object
    }
    assert by_item["a0000000000000000000000000000015"]["http_status"] == 403
    assert "invalid JSON" in by_item["a0000000000000000000000000000014"]["message"]


def test_an_item_whose_data_failed_is_classified_with_lower_confidence(crawled) -> None:
    """The strongest signal was missing; say so rather than claiming certainty."""
    conn, _ = crawled
    for item_id in ("a0000000000000000000000000000014", "a0000000000000000000000000000015"):
        row = conn.execute(
            "SELECT platform, platform_confidence, platform_evidence, raw_data_json "
            "FROM resource WHERE item_id = ?",
            (item_id,),
        ).fetchone()
        assert row["platform"] == "web_appbuilder"
        assert row["platform_confidence"] == "guess"
        assert json.loads(row["platform_evidence"])["data_status"] == "error"
        # Nothing unreadable gets stored as if it were data.
        assert row["raw_data_json"] is None


def test_a_departed_owner_is_recorded_as_absent(crawled) -> None:
    conn, _ = crawled
    orphans = conn.execute("SELECT item_id, owner FROM v_orphaned").fetchall()
    assert [o["item_id"] for o in orphans] == ["a0000000000000000000000000000004"]

    present = conn.execute(
        "SELECT COUNT(*) AS n FROM resource WHERE kind = 'item' AND owner_exists = 1"
    ).fetchone()["n"]
    assert present == 29


def test_owner_exists_stays_unknown_when_the_user_list_is_unreadable(
    conn: sqlite3.Connection,
) -> None:
    """Three states, not two: 'gone' and 'could not check' are different facts."""

    class NoUsers(FixtureTransport):
        def get_json(self, url: str, params: dict[str, Any] | None = None):
            if url.endswith("/community/users"):
                from arcgis_inventory.errors import PortalError

                raise PortalError("403", url=url, status=403)
            return super().get_json(url, params)

    result = crawl_inventory(conn, PortalClient(NoUsers(FIXTURE), PORTAL_URL, page_size=PAGE_SIZE))
    assert result.item_count == 30

    unknown = conn.execute(
        "SELECT COUNT(*) AS n FROM resource WHERE kind = 'item' AND owner_exists IS NULL"
    ).fetchone()["n"]
    assert unknown == 30
    assert (
        conn.execute("SELECT COUNT(*) AS n FROM crawl_error WHERE phase = 'user'").fetchone()["n"]
        == 1
    )


# ---------------------------------------------------------------------------
# Raw retention, runs, and re-crawl
# ---------------------------------------------------------------------------


def test_raw_documents_are_retained_so_reprocess_needs_no_network(crawled) -> None:
    conn, _ = crawled
    missing_raw = conn.execute(
        "SELECT COUNT(*) AS n FROM resource WHERE kind = 'item' AND raw_json IS NULL"
    ).fetchone()["n"]
    assert missing_raw == 0

    with_data = conn.execute(
        "SELECT COUNT(*) AS n FROM resource WHERE raw_data_json IS NOT NULL"
    ).fetchone()["n"]
    # Everything data-bearing that could actually be read.
    assert with_data == 27

    wab = conn.execute(
        "SELECT raw_data_json FROM resource WHERE item_id = 'a0000000000000000000000000000001'"
    ).fetchone()
    assert json.loads(wab["raw_data_json"])["wabVersion"] == "2.29"


def test_the_run_is_recorded_with_its_scope_and_tool_version(crawled) -> None:
    conn, result = crawled
    run = conn.execute("SELECT * FROM run WHERE run_id = ?", (result.run_id,)).fetchone()
    assert run["mode"] == "crawl"
    assert run["status"] == "partial"
    assert run["finished_at"] is not None
    assert run["item_count"] == 30
    assert run["tool_version"]
    assert json.loads(run["scope_json"])["page_size"] == PAGE_SIZE


def test_the_portal_row_reflects_what_the_portal_says_about_itself(crawled) -> None:
    conn, _ = crawled
    portal = conn.execute("SELECT * FROM portal").fetchone()
    assert portal["url"] == PORTAL_URL
    assert portal["kind"] == "enterprise"
    assert portal["version"] == "11.4"


def test_usage_snapshots_are_captured_per_run(crawled) -> None:
    conn, result = crawled
    rows = conn.execute(
        "SELECT COUNT(*) AS n FROM usage_snapshot WHERE run_id = ?", (result.run_id,)
    ).fetchone()["n"]
    assert rows == 30

    # Null views stay null: 'unknown usage' is not 'unused'.
    unknown = conn.execute(
        "SELECT u.num_views FROM usage_snapshot u JOIN resource r USING (resource_id) "
        "WHERE r.item_id = 'a0000000000000000000000000000024'"
    ).fetchone()
    assert unknown["num_views"] is None


def test_recrawling_updates_in_place_rather_than_duplicating(conn: sqlite3.Connection) -> None:
    first = crawl_inventory(conn, client())
    second = crawl_inventory(conn, client())

    count = conn.execute("SELECT COUNT(*) AS n FROM resource").fetchone()["n"]
    assert count == 30

    row = conn.execute(
        "SELECT first_seen_run, last_seen_run FROM resource "
        "WHERE item_id = 'a0000000000000000000000000000001'"
    ).fetchone()
    assert row["first_seen_run"] == first.run_id
    assert row["last_seen_run"] == second.run_id


def test_the_second_crawl_shows_what_appeared_and_what_disappeared(
    conn: sqlite3.Connection,
) -> None:
    first = crawl_inventory(conn, client())
    second = crawl_inventory(conn, client(overlay="run2"))

    # Nothing is ever deleted; the vanished app's last_seen_run just stops.
    gone = conn.execute(
        "SELECT first_seen_run, last_seen_run FROM resource "
        "WHERE item_id = 'a0000000000000000000000000000004'"
    ).fetchone()
    assert gone["last_seen_run"] == first.run_id

    added = conn.execute(
        "SELECT first_seen_run, platform FROM resource "
        "WHERE item_id = 'a0000000000000000000000000000031'"
    ).fetchone()
    assert added["first_seen_run"] == second.run_id
    assert added["platform"] == "experience_builder"

    # And the re-shared item is the same resource with new access.
    storm = conn.execute(
        "SELECT first_seen_run, access FROM resource "
        "WHERE item_id = 'a0000000000000000000000000000017'"
    ).fetchone()
    assert storm["first_seen_run"] == first.run_id
    assert storm["access"] == "public"


def test_usage_slope_is_visible_across_two_runs(conn: sqlite3.Connection) -> None:
    """Two crawls a month apart beat any single snapshot."""
    crawl_inventory(conn, client())
    crawl_inventory(conn, client(overlay="run2"))

    views = [
        r["num_views"]
        for r in conn.execute(
            "SELECT u.num_views FROM usage_snapshot u JOIN resource r USING (resource_id) "
            "WHERE r.item_id = 'a0000000000000000000000000000003' ORDER BY u.run_id"
        )
    ]
    assert len(views) == 2
    assert views[1] > views[0]


# ---------------------------------------------------------------------------
# The property the whole schema exists to protect
# ---------------------------------------------------------------------------


def test_a_recrawl_does_not_touch_authored_data(conn: sqlite3.Connection) -> None:
    """Losing triage state means losing weeks of work. It must survive re-crawl."""
    crawl_inventory(conn, client())
    resource_id = conn.execute(
        "SELECT resource_id FROM resource WHERE item_id = 'a0000000000000000000000000000005'"
    ).fetchone()["resource_id"]

    conn.execute(
        "INSERT INTO migration (resource_id, status, owner_ref, notes, updated_at) "
        "VALUES (?, 'in_progress', 'TICKET-4821', 'rebuilding as an Instant App', "
        "'2026-08-06T00:00:00Z')",
        (resource_id,),
    )
    conn.execute(
        "INSERT INTO finding (fingerprint, portal_id, resource_id, rule_id, category, severity, "
        "title, status, status_note, first_seen_run, last_seen_run) "
        "VALUES ('fp-test-0001', 1, ?, 'public-app-private-dep', 'sharing', 'critical', "
        "'Public app over a private layer', 'wontfix', 'accepted by the director', 1, 1)",
        (resource_id,),
    )
    conn.commit()

    crawl_inventory(conn, client(overlay="run2"))

    migration = conn.execute(
        "SELECT * FROM migration WHERE resource_id = ?", (resource_id,)
    ).fetchone()
    assert migration["status"] == "in_progress"
    assert migration["owner_ref"] == "TICKET-4821"

    finding = conn.execute(
        "SELECT status, status_note FROM finding WHERE fingerprint = 'fp-test-0001'"
    ).fetchone()
    assert finding["status"] == "wontfix"
    assert finding["status_note"] == "accepted by the director"


def test_skip_data_crawls_without_fetching_documents(conn: sqlite3.Connection) -> None:
    result = crawl_inventory(conn, client(), fetch_data=False)
    assert result.item_count == 30
    assert result.error_count == 0
    assert result.status == "complete"

    stored = conn.execute(
        "SELECT COUNT(*) AS n FROM resource WHERE raw_data_json IS NOT NULL"
    ).fetchone()["n"]
    assert stored == 0
    # Classification falls back to keywords, so the WAB apps are still found.
    wab = conn.execute(
        "SELECT COUNT(*) AS n FROM resource WHERE platform = 'web_appbuilder'"
    ).fetchone()["n"]
    assert wab == 9


def test_a_failed_search_ends_the_run_as_failed(conn: sqlite3.Connection) -> None:
    class NoSearch(FixtureTransport):
        def get_json(self, url: str, params: dict[str, Any] | None = None):
            if url.endswith("/search"):
                from arcgis_inventory.errors import PortalError

                raise PortalError("500 from portal", url=url, status=500)
            return super().get_json(url, params)

    result = crawl_inventory(conn, PortalClient(NoSearch(FIXTURE), PORTAL_URL))
    assert result.status == "failed"
    assert result.item_count == 0
    assert (
        conn.execute("SELECT COUNT(*) AS n FROM crawl_error WHERE phase = 'search'").fetchone()["n"]
        == 1
    )


def test_store_never_writes_authored_columns() -> None:
    """A guard on the module, not just on behavior: authored columns are not
    mentioned anywhere in the writer."""
    source = (Path(store.__file__)).read_text(encoding="utf-8")
    for forbidden in ("override_target", "override_note", "status_note", "blocked_reason"):
        assert forbidden not in source, f"{forbidden} must never be written by a crawl"


# ---------------------------------------------------------------------------
# Search scoping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "org_id", "expected"),
    [
        ("", "ORG123", "orgid:ORG123"),
        ("type:Web Map", "ORG123", "orgid:ORG123 AND (type:Web Map)"),
        ("", None, ""),
        ("type:Web Map", None, "type:Web Map"),
        # A caller who scoped it themselves is left alone.
        ("orgid:OTHER", "ORG123", "orgid:OTHER"),
    ],
)
def test_search_is_scoped_to_the_organization(
    query: str, org_id: str | None, expected: str
) -> None:
    """An anonymous unscoped search against ArcGIS Online returns the entire
    worldwide public catalogue, not this org. Nothing about the request looks
    wrong while it happens."""
    from arcgis_inventory.crawl import scoped_query

    assert scoped_query(query, org_id) == expected


def test_the_crawl_records_the_query_it_actually_sent(conn: sqlite3.Connection) -> None:
    result = crawl_inventory(conn, client())
    scope = json.loads(
        conn.execute("SELECT scope_json FROM run WHERE run_id = ?", (result.run_id,)).fetchone()[
            "scope_json"
        ]
    )
    # The fixture portal reports an org id, so the crawl must have scoped to it.
    assert scope["effective_query"] == "orgid:NgFiXtUrEoRg0001"
    assert scope["query"] == ""


def test_the_scoped_query_reaches_the_portal(conn: sqlite3.Connection) -> None:
    sent: list[Any] = []

    class Recording(FixtureTransport):
        def get_json(self, url: str, params: dict[str, Any] | None = None):
            if url.endswith("/search"):
                sent.append((params or {}).get("q"))
            return super().get_json(url, params)

    crawl_inventory(conn, PortalClient(Recording(FIXTURE), PORTAL_URL, page_size=10))
    assert sent
    assert all(q == "orgid:NgFiXtUrEoRg0001" for q in sent)


def test_a_rejected_search_fails_the_run_instead_of_reporting_an_empty_org(
    conn: sqlite3.Connection,
) -> None:
    """ArcGIS Online answers a rejected search with HTTP 200 and an error
    object. Treating that as 'no results' is how a tool tells somebody their
    organization is clean when it never looked."""

    class RejectingSearch(FixtureTransport):
        def get_json(self, url: str, params: dict[str, Any] | None = None):
            if url.endswith("/search"):
                from arcgis_inventory.transport import Response

                return Response(
                    url=url,
                    status=200,
                    data={"error": {"code": 400, "message": "Unable to perform search."}},
                )
            return super().get_json(url, params)

    result = crawl_inventory(conn, PortalClient(RejectingSearch(FIXTURE), PORTAL_URL))

    assert result.status == "failed"
    assert result.item_count == 0
    error = conn.execute(
        "SELECT message, http_status FROM crawl_error WHERE phase = 'search'"
    ).fetchone()
    assert "Unable to perform search" in error["message"]
    assert error["http_status"] == 400

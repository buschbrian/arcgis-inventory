"""The dependency graph, against the fixture's declared edge list.

`expected/edges.json` names every dependency the fixture contains, including
the JSON pointer it comes from. This is the assertion: extraction has to
reproduce it exactly --- not a superset, not a subset.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from arcgis_inventory.crawl import PortalClient, crawl_inventory
from arcgis_inventory.db import open_database
from arcgis_inventory.dependencies import build_dependencies
from arcgis_inventory.extract import extract_edges
from arcgis_inventory.transport import FixtureTransport
from arcgis_inventory.urls import normalize_url

FIXTURE = Path(__file__).parent / "fixtures" / "northgate"
PORTAL_URL = "https://northgate.example.gov/portal"

Edge = tuple[str, str, str, str, Any]  # from, to-identity, relation, source_path, layer_index


def expected_edges() -> set[Edge]:
    raw = json.loads((FIXTURE / "expected" / "edges.json").read_text(encoding="utf-8"))
    return {
        (
            row["from_item_id"],
            row.get("to_item_id") or normalize_url(row["to_url"]).url,
            row["relation"],
            row["source_path"],
            row["layer_index"],
        )
        for row in raw
    }


def actual_edges(conn: sqlite3.Connection) -> set[Edge]:
    rows = conn.execute(
        "SELECT src.item_id AS from_item, dst.item_id AS to_item, "
        "dst.url_normalized AS to_url, e.relation, e.source_path, e.detail_json "
        "FROM edge e "
        "JOIN resource src ON src.resource_id = e.from_resource "
        "JOIN resource dst ON dst.resource_id = e.to_resource"
    ).fetchall()
    result: set[Edge] = set()
    for row in rows:
        detail = json.loads(row["detail_json"]) if row["detail_json"] else {}
        result.add(
            (
                row["from_item"],
                row["to_item"] or row["to_url"],
                row["relation"],
                row["source_path"],
                detail.get("layer_index"),
            )
        )
    return result


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    connection = open_database(tmp_path / "inv.sqlite")
    yield connection
    connection.close()


@pytest.fixture
def graphed(conn: sqlite3.Connection):
    crawl_inventory(conn, PortalClient(FixtureTransport(FIXTURE), PORTAL_URL, page_size=10))
    result = build_dependencies(conn)
    return conn, result


# ---------------------------------------------------------------------------
# The golden test
# ---------------------------------------------------------------------------


def test_the_graph_matches_the_fixtures_declared_edge_list(graphed) -> None:
    conn, result = graphed
    want, got = expected_edges(), actual_edges(conn)

    missing = sorted(want - got)
    extra = sorted(got - want)
    assert not missing and not extra, (
        f"\nmissing {len(missing)}:\n"
        + "\n".join(f"  {e}" for e in missing[:10])
        + f"\nextra {len(extra)}:\n"
        + "\n".join(f"  {e}" for e in extra[:10])
    )
    assert result.edge_count == len(want) == 64


def test_every_relation_the_fixture_exercises_is_produced(graphed) -> None:
    _, result = graphed
    assert result.relations == {
        "operational_layer": 44,
        "data_source": 12,
        "basemap": 2,
        "geocoder": 2,
        "arcade_source": 1,
        "gp_service": 1,
        "print_service": 1,
        "widget_config": 1,
    }


# ---------------------------------------------------------------------------
# The traps the fixture exists to catch
# ---------------------------------------------------------------------------


def test_one_service_at_two_layer_indexes_is_one_node_and_three_edges(graphed) -> None:
    """Case 22 --- the URL-normalization trap, now through the whole pipeline."""
    conn, _ = graphed
    rows = conn.execute(
        "SELECT e.to_resource, e.source_path, e.detail_json FROM edge e "
        "JOIN resource src ON src.resource_id = e.from_resource "
        "WHERE src.item_id = 'a0000000000000000000000000000022'"
    ).fetchall()

    assert len(rows) == 3
    assert len({r["to_resource"] for r in rows}) == 1  # one endpoint node
    assert len({r["source_path"] for r in rows}) == 3  # three distinct dependencies
    indexes = sorted(json.loads(r["detail_json"])["layer_index"] for r in rows)
    assert indexes == [0, 0, 1]


def test_group_layers_are_walked_to_the_bottom(graphed) -> None:
    """Case 19 --- four levels deep, and every leaf is an edge."""
    conn, _ = graphed
    rows = conn.execute(
        "SELECT e.source_path FROM edge e JOIN resource src ON src.resource_id = e.from_resource "
        "WHERE src.item_id = 'a0000000000000000000000000000019'"
    ).fetchall()
    assert len(rows) == 8
    assert max(r["source_path"].count("/layers/") for r in rows) == 3


def test_an_arcade_expression_becomes_a_dependency(graphed) -> None:
    """Invisible to anything that only reads operationalLayers, breaks just as hard."""
    conn, _ = graphed
    row = conn.execute(
        "SELECT dst.url_normalized, e.source_path FROM edge e "
        "JOIN resource src ON src.resource_id = e.from_resource "
        "JOIN resource dst ON dst.resource_id = e.to_resource "
        "WHERE e.relation = 'arcade_source'"
    ).fetchone()
    assert row["url_normalized"].endswith("StreetCenterlines/FeatureServer")
    assert "expressionInfos" in row["source_path"]


def test_a_gp_task_url_resolves_to_the_service_not_the_task(graphed) -> None:
    """Otherwise every task becomes its own node and impact analysis fragments."""
    conn, _ = graphed
    rows = conn.execute(
        "SELECT dst.url_normalized, dst.service_type FROM edge e "
        "JOIN resource dst ON dst.resource_id = e.to_resource "
        "WHERE e.relation IN ('gp_service', 'print_service')"
    ).fetchall()
    assert len(rows) == 2
    for row in rows:
        assert row["url_normalized"].endswith("GPServer")
        assert row["service_type"] == "GPServer"


def test_a_service_referenced_by_url_only_becomes_an_endpoint_node(graphed) -> None:
    """Case 18 --- the node with no item_id that a two-table design gets wrong."""
    conn, _ = graphed
    row = conn.execute(
        "SELECT dst.kind, dst.item_id, dst.url_normalized FROM edge e "
        "JOIN resource src ON src.resource_id = e.from_resource "
        "JOIN resource dst ON dst.resource_id = e.to_resource "
        "WHERE src.item_id = 'a0000000000000000000000000000018'"
    ).fetchone()
    assert row["kind"] == "endpoint"
    assert row["item_id"] is None
    assert row["url_normalized"].endswith("TrafficSignals/FeatureServer")


def test_a_widget_config_dependency_is_traceable_to_its_widget(graphed) -> None:
    """Case 6 --- the dev-host service nobody knew an app still pointed at."""
    conn, _ = graphed
    row = conn.execute(
        "SELECT dst.host, e.source_path FROM edge e "
        "JOIN resource dst ON dst.resource_id = e.to_resource "
        "WHERE e.relation = 'widget_config'"
    ).fetchone()
    assert row["host"] == "gis-dev.northgate.example.gov"
    assert row["source_path"] == "/widgetPool/widgets/0/config/sources/0/url"


def test_an_http_service_is_flagged_insecure(graphed) -> None:
    """Case 7 --- one plaintext reference is the finding."""
    conn, _ = graphed
    rows = conn.execute("SELECT * FROM v_http_services").fetchall()
    assert rows
    assert all("FloodZones" in r["dep_url"] for r in rows)


def test_the_endpoint_url_is_canonical_even_when_written_as_http(graphed) -> None:
    conn, _ = graphed
    row = conn.execute(
        "SELECT url_normalized, is_https, host FROM resource "
        "WHERE url_normalized LIKE '%FloodZones%'"
    ).fetchone()
    assert row["url_normalized"].startswith("https://")  # one node, canonical form
    assert row["is_https"] == 0  # but the fact it was written http:// survives
    assert row["host"] == "maps.northgate.example.gov"


# ---------------------------------------------------------------------------
# What the graph makes answerable
# ---------------------------------------------------------------------------


def test_a_shared_web_map_is_visible_as_shared(graphed) -> None:
    """Migrate once, fix many --- the whole reason to build the graph first."""
    conn, _ = graphed
    rows = conn.execute("SELECT * FROM v_shared_maps ORDER BY app_count DESC").fetchall()
    assert [(r["title"], r["app_count"]) for r in rows] == [
        ("Utilities Master", 3),  # apps 2 and 6, plus the dashboard
        ("Parcels & Zoning", 2),
        ("Street Centerlines", 2),
    ]


def test_impact_analysis_reads_the_graph_backwards(graphed) -> None:
    """'What breaks if this layer changes' is the query that outlives the migration."""
    conn, _ = graphed
    rows = conn.execute(
        "SELECT src.title FROM edge e "
        "JOIN resource dst ON dst.resource_id = e.to_resource "
        "JOIN resource src ON src.resource_id = e.from_resource "
        "WHERE dst.url_normalized LIKE '%/Public/Parcels/FeatureServer'"
    ).fetchall()
    titles = sorted(r["title"] for r in rows)
    assert "Parcels & Zoning" in titles
    assert "Development Projects" in titles


def test_retirement_exposure_ranks_wab_apps_by_reach(graphed) -> None:
    conn, _ = graphed
    rows = conn.execute(
        "SELECT title, dep_count, exposure_score FROM v_retirement_exposure "
        "ORDER BY exposure_score DESC"
    ).fetchall()
    assert len(rows) == 9
    assert rows[0]["title"] == "Snow Plow Route Status"  # most-viewed WAB app

    # The only WAB apps with no dependencies are the two whose data could not be
    # read. That is a gap in knowledge, not an app with nothing underneath it,
    # and `scan` should treat it that way rather than reporting them as simple.
    no_deps = sorted(r["title"] for r in rows if r["dep_count"] == 0)
    assert no_deps == ["Bike Path Network", "Internal Facilities Viewer"]


# ---------------------------------------------------------------------------
# Honesty about what it does not know
# ---------------------------------------------------------------------------


def test_endpoint_sharing_is_left_unknown_not_guessed(graphed) -> None:
    """A crawl authenticated as someone who sees everything cannot tell whether
    a bare service is public. `audit-sharing` probes; this must not pretend."""
    conn, _ = graphed
    rows = conn.execute(
        "SELECT COUNT(*) AS n FROM resource WHERE kind = 'endpoint' AND access IS NOT NULL"
    ).fetchone()
    assert rows["n"] == 0

    # And so the headline view stays empty rather than reporting a false clean bill.
    assert conn.execute("SELECT COUNT(*) AS n FROM v_public_app_private_dep").fetchone()["n"] == 0


def test_a_reference_to_an_item_the_crawl_never_saw_is_recorded(
    conn: sqlite3.Connection,
) -> None:
    crawl_inventory(conn, PortalClient(FixtureTransport(FIXTURE), PORTAL_URL, page_size=10))

    # Simulate an app pointing at a web map this account cannot see: rewrite the
    # reference rather than deleting the row, which is both closer to what a
    # real portal looks like and does not fight the foreign keys.
    missing = "a" + "9" * 31
    conn.execute(
        "UPDATE resource SET raw_data_json = replace("
        "raw_data_json, 'a0000000000000000000000000000016', ?) "
        "WHERE item_id = 'a0000000000000000000000000000001'",
        (missing,),
    )
    conn.commit()

    result = build_dependencies(conn)
    assert [ref for _, ref in result.unresolved] == [missing]

    error = conn.execute("SELECT message FROM crawl_error WHERE phase = 'item'").fetchone()
    assert missing in error["message"]
    assert "not visible to this account" in error["message"]


# ---------------------------------------------------------------------------
# Bookkeeping
# ---------------------------------------------------------------------------


def test_rebuilding_is_idempotent(conn: sqlite3.Connection) -> None:
    crawl_inventory(conn, PortalClient(FixtureTransport(FIXTURE), PORTAL_URL, page_size=10))
    first = build_dependencies(conn)
    before = actual_edges(conn)
    second = build_dependencies(conn)

    assert second.edge_count == first.edge_count
    assert actual_edges(conn) == before
    assert conn.execute("SELECT COUNT(*) AS n FROM edge").fetchone()["n"] == 64
    assert (
        conn.execute("SELECT COUNT(*) AS n FROM resource WHERE kind = 'endpoint'").fetchone()["n"]
        == 22
    )


def test_dependencies_needs_a_crawl_first(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="no portal"):
        build_dependencies(conn)


# ---------------------------------------------------------------------------
# Extraction in isolation
# ---------------------------------------------------------------------------


def test_extraction_ignores_platforms_it_has_no_reader_for() -> None:
    assert extract_edges("widget_package", {"anything": 1}) == []
    assert extract_edges(None, {"operationalLayers": []}) == []
    assert extract_edges("web_map", None) == []


def test_extraction_survives_malformed_config() -> None:
    """Somebody's portal will contain every one of these shapes."""
    junk = {
        "operationalLayers": [None, {}, {"url": ""}, {"url": "not a url"}, {"layers": "nope"}],
        "baseMap": {"baseMapLayers": "not a list"},
    }
    assert extract_edges("web_map", junk) == []


def test_a_relative_url_is_not_a_dependency() -> None:
    edges = extract_edges("web_map", {"operationalLayers": [{"url": "/rest/services/X/MapServer"}]})
    assert edges == []


def test_an_edge_must_target_exactly_one_thing() -> None:
    from arcgis_inventory.extract import ExtractedEdge

    with pytest.raises(ValueError, match="exactly one"):
        ExtractedEdge("data_source", "/x", item_id="a", url="https://h.example.gov/x")
    with pytest.raises(ValueError, match="exactly one"):
        ExtractedEdge("data_source", "/x")

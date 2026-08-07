"""Recommendations, and the argument behind each one.

A bare verdict gets ignored, so the reasoning is tested as carefully as the
label --- if it does not carry the numbers the rule matched on, a reader cannot
check the verdict and will not trust it.
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
from arcgis_inventory.recommend import RecommendRule, load_recommend_rules, recommend_targets
from arcgis_inventory.transport import FixtureTransport

FIXTURE = Path(__file__).parent / "fixtures" / "northgate"
PORTAL_URL = "https://northgate.example.gov/portal"


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    connection = open_database(tmp_path / "inv.sqlite")
    yield connection
    connection.close()


@pytest.fixture
def graphed(conn: sqlite3.Connection) -> sqlite3.Connection:
    crawl_inventory(conn, PortalClient(FixtureTransport(FIXTURE), PORTAL_URL, page_size=10))
    build_dependencies(conn)
    return conn


def verdicts(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        row["title"]: row["target"]
        for row in conn.execute(
            "SELECT r.title, c.target FROM recommendation c JOIN resource r USING (resource_id)"
        )
    }


def row_for(conn: sqlite3.Connection, title: str) -> sqlite3.Row:
    return conn.execute(
        "SELECT c.* FROM recommendation c JOIN resource r USING (resource_id) WHERE r.title = ?",
        (title,),
    ).fetchone()


# ---------------------------------------------------------------------------
# The verdicts
# ---------------------------------------------------------------------------


def test_every_application_gets_a_verdict(graphed: sqlite3.Connection) -> None:
    result = recommend_targets(graphed)
    assert result.considered == 15
    assert result.targets == {
        "keep": 5,
        "instant_app": 4,
        "custom": 2,
        "unknown": 2,
        "experience_builder": 1,
        "retire": 1,
    }


def test_a_simple_single_map_app_goes_to_an_instant_app(graphed: sqlite3.Connection) -> None:
    """The bias that matters: most WAB apps are one map and a search box, and
    rebuilding those in Experience Builder is more work than they deserve."""
    recommend_targets(graphed)
    row = row_for(graphed, "Parcel & Zoning Lookup")
    assert row["target"] == "instant_app"
    assert json.loads(row["rules_fired"]) == ["instant-app-simple"]


def test_a_multi_page_multi_widget_app_goes_to_experience_builder(
    graphed: sqlite3.Connection,
) -> None:
    recommend_targets(graphed)
    row = row_for(graphed, "Public Works Asset Viewer")
    assert row["target"] == "experience_builder"
    assert "3 pages" in row["reasoning"]


def test_a_custom_widget_makes_it_a_rewrite(graphed: sqlite3.Connection) -> None:
    recommend_targets(graphed)
    row = row_for(graphed, "Snow Plow Route Status")
    assert row["target"] == "custom"
    assert row["confidence"] == "certain"
    assert "1 custom" in row["reasoning"]


def test_a_hand_built_js_app_is_a_rewrite_too(graphed: sqlite3.Connection) -> None:
    recommend_targets(graphed)
    assert row_for(graphed, "Election Precinct Finder")["target"] == "custom"


def test_an_orphaned_unused_app_is_retired_not_rebuilt(graphed: sqlite3.Connection) -> None:
    """The cheapest migration is the one you do not do."""
    recommend_targets(graphed)
    row = row_for(graphed, "Historic District Survey (2019)")
    assert row["target"] == "retire"
    assert "no current owner" in row["reasoning"]


def test_apps_already_on_a_current_platform_are_left_alone(
    graphed: sqlite3.Connection,
) -> None:
    recommend_targets(graphed)
    results = verdicts(graphed)
    for title in (
        "Street Resurfacing Program",
        "Park & Trail Finder",
        "Water Main Break Dashboard",
        "Address Point Lookup",
        "Downtown Streetscape Plan",
    ):
        assert results[title] == "keep", title


def test_an_unreadable_app_gets_no_verdict_rather_than_a_confident_guess(
    graphed: sqlite3.Connection,
) -> None:
    """A confident 'Instant App' for an app nobody could inspect is worse than
    saying so."""
    recommend_targets(graphed)
    for title in ("Bike Path Network", "Internal Facilities Viewer"):
        row = row_for(graphed, title)
        assert row["target"] == "unknown"
        assert row["confidence"] == "guess"
        assert "could not be read" in row["reasoning"]


def test_web_maps_and_services_do_not_get_recommendations(
    graphed: sqlite3.Connection,
) -> None:
    """A web map is not migrated in its own right; it is a dependency of
    something that is."""
    recommend_targets(graphed)
    assert "Parcels & Zoning" not in verdicts(graphed)
    endpoints = graphed.execute(
        "SELECT COUNT(*) AS n FROM recommendation c JOIN resource r USING (resource_id) "
        "WHERE r.kind = 'endpoint'"
    ).fetchone()["n"]
    assert endpoints == 0


# ---------------------------------------------------------------------------
# The reasoning is the product
# ---------------------------------------------------------------------------


def test_reasoning_carries_the_numbers_the_rule_matched_on(
    graphed: sqlite3.Connection,
) -> None:
    recommend_targets(graphed)
    row = row_for(graphed, "Parcel & Zoning Lookup")
    reasoning = row["reasoning"]
    assert "1 web map" in reasoning
    assert "2 widgets" in reasoning
    assert "48,210 views" in reasoning  # thousands separator: humans read this
    assert reasoning.startswith("Rebuild as an Instant App")


def test_unknown_usage_is_stated_not_rendered_as_zero(graphed: sqlite3.Connection) -> None:
    rules = load_recommend_rules()
    rules["recommend_platforms"] = ["web_map"]  # item 24 has numViews = null
    recommend_targets(graphed, rules=rules)
    row = row_for(graphed, "Address Points")
    assert "usage unknown" in row["reasoning"]
    assert "0 views" not in row["reasoning"]


def test_complexity_sorts_the_hard_ones_to_the_top(graphed: sqlite3.Connection) -> None:
    recommend_targets(graphed)
    ranked = [
        (r["title"], r["complexity"])
        for r in graphed.execute(
            "SELECT r.title, c.complexity FROM recommendation c JOIN resource r "
            "USING (resource_id) ORDER BY c.complexity DESC"
        )
    ]
    assert ranked[0][0] == "Public Works Asset Viewer"
    assert ranked[0][1] > ranked[-1][1]
    assert all(0 <= score <= 100 for _, score in ranked)


def test_a_custom_widget_dominates_the_complexity_score(graphed: sqlite3.Connection) -> None:
    recommend_targets(graphed)
    custom = row_for(graphed, "Snow Plow Route Status")["complexity"]
    simple = row_for(graphed, "Parcel & Zoning Lookup")["complexity"]
    assert custom > simple * 2


# ---------------------------------------------------------------------------
# Rules are data
# ---------------------------------------------------------------------------


def test_rules_can_be_replaced_wholesale(graphed: sqlite3.Connection) -> None:
    rules = load_recommend_rules()
    rules["rules"] = [
        RecommendRule(
            id="everything-to-exb",
            target="experience_builder",
            confidence="guess",
            when={"signal": "platform", "not_equals": "nothing"},
            because="the local policy is to standardize on Experience Builder.",
        )
    ]
    result = recommend_targets(graphed, rules=rules)
    assert set(result.targets) == {"experience_builder"}


def test_first_matching_rule_wins(graphed: sqlite3.Connection) -> None:
    """A recommendation is one verdict, not an accumulation."""
    rules = load_recommend_rules()
    rules["rules"] = [
        RecommendRule(
            id="first",
            target="retire",
            confidence="guess",
            when={"signal": "platform", "not_equals": "x"},
            because="first.",
        ),
        RecommendRule(
            id="second",
            target="keep",
            confidence="guess",
            when={"signal": "platform", "not_equals": "y"},
            because="second.",
        ),
    ]
    result = recommend_targets(graphed, rules=rules)
    assert set(result.targets) == {"retire"}
    assert json.loads(row_for(graphed, "Parcel & Zoning Lookup")["rules_fired"]) == ["first"]


@pytest.mark.parametrize(
    "when",
    [
        {"signal": "widget_kount", "gte": 1},
        {"signal": "widget_count", "roughly": 1},
        {"gte": 1},
    ],
)
def test_a_broken_rule_raises_rather_than_matching_nothing(
    graphed: sqlite3.Connection, when: dict[str, Any]
) -> None:
    rules = load_recommend_rules()
    rules["rules"] = [
        RecommendRule(id="bad", target="keep", confidence="guess", when=when, because="x.")
    ]
    with pytest.raises(ValueError):
        recommend_targets(graphed, rules=rules)


def test_a_null_signal_never_satisfies_a_comparison(graphed: sqlite3.Connection) -> None:
    """`num_views` is NULL when usage is unknown, and that must not read as zero
    --- the difference between retiring a dead app and deleting a live one."""
    rules = load_recommend_rules()
    rules["recommend_platforms"] = ["web_map"]
    rules["rules"] = [
        RecommendRule(
            id="unused",
            target="retire",
            confidence="guess",
            when={"signal": "num_views", "lte": 0},
            because="no views.",
        )
    ]
    recommend_targets(graphed, rules=rules)
    assert row_for(graphed, "Address Points")["target"] == "unknown"


# ---------------------------------------------------------------------------
# Authored data
# ---------------------------------------------------------------------------


def test_a_human_override_survives_a_rerun(graphed: sqlite3.Connection) -> None:
    """Deciding 'this one goes to an Instant App regardless' is exactly the
    judgment the tool cannot make."""
    recommend_targets(graphed)
    resource_id = row_for(graphed, "Public Works Asset Viewer")["resource_id"]
    graphed.execute(
        "UPDATE recommendation SET override_target = 'instant_app', "
        "override_note = 'splitting it into two simple apps', "
        "override_at = '2026-08-06T00:00:00Z' WHERE resource_id = ?",
        (resource_id,),
    )
    graphed.commit()

    result = recommend_targets(graphed)
    assert result.overridden == 1

    row = row_for(graphed, "Public Works Asset Viewer")
    assert row["override_target"] == "instant_app"
    assert row["override_note"] == "splitting it into two simple apps"
    assert row["target"] == "experience_builder"  # the generated verdict still updates


def test_rerunning_is_stable(graphed: sqlite3.Connection) -> None:
    first = recommend_targets(graphed)
    before = verdicts(graphed)
    second = recommend_targets(graphed)
    assert second.targets == first.targets
    assert verdicts(graphed) == before
    assert graphed.execute("SELECT COUNT(*) AS n FROM recommendation").fetchone()["n"] == 15


# ---------------------------------------------------------------------------
# Without the graph
# ---------------------------------------------------------------------------


def test_without_the_dependency_graph_apps_look_simpler_than_they_are(
    conn: sqlite3.Connection,
) -> None:
    """Documented, not fixed here: the CLI warns, because every app would
    otherwise read as a single-map app and every verdict would skew the same
    way."""
    crawl_inventory(conn, PortalClient(FixtureTransport(FIXTURE), PORTAL_URL, page_size=10))
    recommend_targets(conn)

    row = row_for(conn, "Public Works Asset Viewer")
    assert "web map" not in row["reasoning"]  # no graph, no maps counted
    assert row["target"] == "experience_builder"  # still right, via widgets and pages


def test_recommend_needs_a_crawl_first(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="no portal"):
        recommend_targets(conn)

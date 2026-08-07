"""Deprecated-technology scanning.

Two things are being tested: that the shipped rules fire on the cases the
fixture was built to contain, and that the matcher engine behaves --- because
the rules are data, and a rule file that silently matches nothing is worse than
no scanner at all.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from arcgis_inventory.crawl import PortalClient, crawl_inventory
from arcgis_inventory.db import open_database
from arcgis_inventory.scan import Rule, load_scan_rules, scan_inventory
from arcgis_inventory.transport import FixtureTransport

FIXTURE = Path(__file__).parent / "fixtures" / "northgate"
PORTAL_URL = "https://northgate.example.gov/portal"

WAB_SIMPLE = "a0000000000000000000000000000001"
WAB_CUSTOM_WIDGET = "a0000000000000000000000000000003"
RETIRED_APP = "a0000000000000000000000000000004"
JS_3X = "a0000000000000000000000000000008"


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    connection = open_database(tmp_path / "inv.sqlite")
    yield connection
    connection.close()


@pytest.fixture
def crawled(conn: sqlite3.Connection) -> sqlite3.Connection:
    crawl_inventory(conn, PortalClient(FixtureTransport(FIXTURE), PORTAL_URL, page_size=10))
    return conn


def items_for(conn: sqlite3.Connection, rule_id: str) -> set[str]:
    return {
        row["item_id"]
        for row in conn.execute(
            "SELECT r.item_id FROM finding f JOIN resource r USING (resource_id) "
            "WHERE f.rule_id = ?",
            (rule_id,),
        )
    }


def one_rule(rule: Rule) -> dict[str, Any]:
    config = load_scan_rules()
    config["rules"] = [rule]
    return config


# ---------------------------------------------------------------------------
# The shipped rules, against the fixture
# ---------------------------------------------------------------------------


def test_the_shipped_rules_find_what_the_fixture_contains(crawled: sqlite3.Connection) -> None:
    result = scan_inventory(crawled)
    assert result.scanned == 30
    assert result.findings == {
        "web-appbuilder-retiring": 9,
        "arcgis-js-3": 1,
        "dojo-dijit": 1,
        "wab-custom-widget": 1,
        "unused-and-stale": 1,
    }


def test_every_web_appbuilder_app_is_flagged_against_the_deadline(
    crawled: sqlite3.Connection,
) -> None:
    """The rule the whole project exists around."""
    scan_inventory(crawled)
    flagged = items_for(crawled, "web-appbuilder-retiring")
    platforms = {
        row["item_id"]
        for row in crawled.execute("SELECT item_id FROM resource WHERE platform = 'web_appbuilder'")
    }
    assert flagged == platforms
    row = crawled.execute(
        "SELECT severity, detail, suggested_action FROM finding "
        "WHERE rule_id = 'web-appbuilder-retiring' LIMIT 1"
    ).fetchone()
    assert row["severity"] == "critical"
    # The date that actually matters is when apps stop being *editable*.
    assert "Q4 2026" in row["detail"]
    assert "no converter" in row["suggested_action"].lower()


def test_the_js_3x_app_is_found_by_its_config(crawled: sqlite3.Connection) -> None:
    scan_inventory(crawled)
    assert items_for(crawled, "arcgis-js-3") == {JS_3X}

    evidence = json.loads(
        crawled.execute(
            "SELECT evidence_json FROM finding WHERE rule_id = 'arcgis-js-3'"
        ).fetchone()["evidence_json"]
    )
    # Whichever branch of the `any` fired is recorded, so the finding is arguable.
    assert "matched" in evidence or "data_key" in evidence


def test_dojo_is_found_separately_from_the_api_version(crawled: sqlite3.Connection) -> None:
    scan_inventory(crawled)
    assert items_for(crawled, "dojo-dijit") == {JS_3X}


def test_the_custom_widget_app_is_flagged_as_a_rewrite(crawled: sqlite3.Connection) -> None:
    scan_inventory(crawled)
    assert items_for(crawled, "wab-custom-widget") == {WAB_CUSTOM_WIDGET}

    evidence = json.loads(
        crawled.execute(
            "SELECT evidence_json FROM finding WHERE rule_id = 'wab-custom-widget'"
        ).fetchone()["evidence_json"]
    )
    assert evidence["custom_widgets"] == ["NorthgatePlowStatus"]


def test_stock_widgets_alone_do_not_make_an_app_custom(crawled: sqlite3.Connection) -> None:
    """Otherwise every WAB app is a rewrite and the signal is worthless."""
    scan_inventory(crawled)
    assert WAB_SIMPLE not in items_for(crawled, "wab-custom-widget")


def test_the_dead_app_is_flagged_as_unused_and_stale(crawled: sqlite3.Connection) -> None:
    scan_inventory(crawled)
    assert items_for(crawled, "unused-and-stale") == {RETIRED_APP}


def test_unknown_view_counts_never_match_the_unused_rule(crawled: sqlite3.Connection) -> None:
    """NULL views is 'we do not know', and retiring something on the strength of
    a missing number is how you delete an app somebody depends on."""
    scan_inventory(crawled)
    # Item 24 has numViews = null and an old-ish modified date.
    assert "a0000000000000000000000000000024" not in items_for(crawled, "unused-and-stale")


def test_rules_that_match_nothing_are_simply_absent(crawled: sqlite3.Connection) -> None:
    result = scan_inventory(crawled)
    for quiet in ("flex-or-silverlight-viewer", "wab-3d", "map-viewer-classic"):
        assert quiet not in result.findings


# ---------------------------------------------------------------------------
# The matcher engine
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("when", "expected"),
    [
        ({"platform": ["web_map"]}, 13),
        ({"item_type": ["Web Scene"]}, 1),
        ({"type_keyword": ["WAB2D"]}, 9),
        ({"data_has_key": "wabVersion"}, 7),
        ({"data_matches": "FoldableTheme"}, 7),
        ({"url_matches": "webappviewer"}, 9),
        ({"all": [{"platform": ["web_appbuilder"]}, {"data_has_key": "wabVersion"}]}, 7),
        ({"any": [{"platform": ["dashboard"]}, {"platform": ["storymap"]}]}, 2),
        ({"none": [{"platform": ["web_map"]}]}, 17),
    ],
)
def test_matchers(crawled: sqlite3.Connection, when: dict[str, Any], expected: int) -> None:
    rule = Rule(id="test-rule", category="hygiene", severity="info", title="test", when=when)
    result = scan_inventory(crawled, rules=one_rule(rule))
    assert result.findings.get("test-rule", 0) == expected


def test_an_unknown_matcher_is_an_error_not_a_silent_pass(
    crawled: sqlite3.Connection,
) -> None:
    """A typo in a rule file must not quietly match everything, or nothing."""
    rule = Rule(
        id="typo", category="hygiene", severity="info", title="t", when={"platfrom": ["web_map"]}
    )
    with pytest.raises(ValueError, match="unknown matcher"):
        scan_inventory(crawled, rules=one_rule(rule))


def test_an_empty_when_clause_matches_nothing(crawled: sqlite3.Connection) -> None:
    rule = Rule(id="empty", category="hygiene", severity="info", title="t", when={})
    result = scan_inventory(crawled, rules=one_rule(rule))
    assert result.findings.get("empty", 0) == 0


def test_missing_type_keyword_requires_absence(crawled: sqlite3.Connection) -> None:
    rule = Rule(
        id="not-wab",
        category="hygiene",
        severity="info",
        title="t",
        when={"missing_type_keyword": ["WAB2D", "Web AppBuilder"]},
    )
    result = scan_inventory(crawled, rules=one_rule(rule))
    assert result.findings["not-wab"] == 21  # 30 items minus the 9 WAB apps


def test_rules_are_data_and_can_be_replaced_wholesale(crawled: sqlite3.Connection) -> None:
    """No organization is stuck with the shipped opinions."""
    config = load_scan_rules()
    config["rules"] = []
    result = scan_inventory(crawled, rules=config)
    assert result.total == 0


# ---------------------------------------------------------------------------
# Findings bookkeeping
# ---------------------------------------------------------------------------


def test_rescanning_is_stable(crawled: sqlite3.Connection) -> None:
    scan_inventory(crawled)
    first = {r["fingerprint"] for r in crawled.execute("SELECT fingerprint FROM finding")}
    second = scan_inventory(crawled)
    after = {r["fingerprint"] for r in crawled.execute("SELECT fingerprint FROM finding")}
    assert after == first
    assert second.new == 0


def test_a_dismissed_scan_finding_survives_a_rescan(crawled: sqlite3.Connection) -> None:
    scan_inventory(crawled)
    crawled.execute(
        "UPDATE finding SET status = 'wontfix', status_note = 'retiring this app anyway' "
        "WHERE rule_id = 'web-appbuilder-retiring'"
    )
    crawled.commit()

    scan_inventory(crawled)
    statuses = {
        row["status"]
        for row in crawled.execute(
            "SELECT status FROM finding WHERE rule_id = 'web-appbuilder-retiring'"
        )
    }
    assert statuses == {"wontfix"}


def test_a_rule_that_stops_matching_resolves_its_findings(crawled: sqlite3.Connection) -> None:
    scan_inventory(crawled)
    assert items_for(crawled, "unused-and-stale")

    # Somebody looked at it.
    crawled.execute("UPDATE resource SET num_views = 42 WHERE item_id = ?", (RETIRED_APP,))
    crawled.commit()

    result = scan_inventory(crawled)
    assert result.resolved >= 1
    row = crawled.execute(
        "SELECT resolved_run, status FROM finding WHERE rule_id = 'unused-and-stale'"
    ).fetchone()
    assert row["resolved_run"] is not None
    assert row["status"] == "open"  # observed, never claimed fixed


def test_scan_records_the_rules_version_so_changes_are_attributable(
    crawled: sqlite3.Connection,
) -> None:
    """When a finding changes you need to know whether the portal changed or the
    rules did."""
    result = scan_inventory(crawled)
    run = crawled.execute(
        "SELECT rules_version, mode FROM run WHERE run_id = ?", (result.run_id,)
    ).fetchone()
    assert run["rules_version"]

    config = load_scan_rules()
    config["rules"] = [r for r in config["rules"] if r.id != "dojo-dijit"]
    second = scan_inventory(crawled, rules=config)
    other = crawled.execute(
        "SELECT rules_version FROM run WHERE run_id = ?", (second.run_id,)
    ).fetchone()
    assert other["rules_version"] != run["rules_version"]


def test_scan_needs_a_crawl_first(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="no portal"):
        scan_inventory(conn)

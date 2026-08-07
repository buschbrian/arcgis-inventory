"""Sharing findings, and the triage state they must not destroy.

The headline rule here is the reason the whole tool exists, so it gets tested
from both ends: that it fires on the case it should, and --- more important ---
that it stays quiet when it does not actually know.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from arcgis_inventory.audit import audit_sharing, load_rules, probe_endpoints
from arcgis_inventory.crawl import PortalClient, crawl_inventory
from arcgis_inventory.db import open_database
from arcgis_inventory.dependencies import build_dependencies
from arcgis_inventory.transport import FixtureTransport

FIXTURE = Path(__file__).parent / "fixtures" / "northgate"
PORTAL_URL = "https://northgate.example.gov/portal"

APP_5 = "a0000000000000000000000000000005"  # Development Projects Map, public
APP_3 = "a0000000000000000000000000000003"  # Snow Plow Route Status, public


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


def probe(conn: sqlite3.Connection) -> Any:
    return probe_endpoints(
        conn, FixtureTransport(FIXTURE, anonymous=True, strict=False), portal_id=1
    )


def findings(conn: sqlite3.Connection, rule_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT f.*, r.item_id FROM finding f LEFT JOIN resource r USING (resource_id) "
        "WHERE f.rule_id = ? ORDER BY f.finding_id",
        (rule_id,),
    ).fetchall()


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------


def test_probing_distinguishes_public_from_restricted(graphed: sqlite3.Connection) -> None:
    result = probe(graphed)
    assert result.probed == 22
    assert result.restricted == 6
    assert result.unreachable == 1  # the service that is simply gone
    assert result.public == 15


def test_the_dead_service_is_recorded_unreachable_not_private(
    graphed: sqlite3.Connection,
) -> None:
    """'Gone' and 'not for you' are different answers and lead to different work."""
    probe(graphed)
    row = graphed.execute(
        "SELECT access, reachable, http_status FROM resource "
        "WHERE url_normalized LIKE '%StormSewer_OLD%'"
    ).fetchone()
    assert row["reachable"] == 0
    assert row["http_status"] == 404
    assert row["access"] is None


def test_a_probe_must_not_carry_credentials() -> None:
    """Structural: probing with the crawling account would mark every restricted
    service public, which is the single worst wrong answer this tool could give."""
    source = (Path(__file__).parent.parent / "src/arcgis_inventory/cli.py").read_text(
        encoding="utf-8"
    )
    probe_block = source[source.index("def _probe_transport") : source.index("def _report_audit")]
    assert "token=None" in probe_block
    assert "cfg.portal.token" not in probe_block


# ---------------------------------------------------------------------------
# The headline rule
# ---------------------------------------------------------------------------


def test_without_probing_the_exposure_rule_says_nothing(graphed: sqlite3.Connection) -> None:
    """Unknown is not private. Reporting it as private is the false positive
    that trains people to ignore the rule."""
    result = audit_sharing(graphed)
    assert "public-app-private-dep" not in result.findings
    assert result.unprobed_endpoints == 22


def test_a_public_app_reaching_a_private_layer_through_a_web_map_is_found(
    graphed: sqlite3.Connection,
) -> None:
    """Case 5, and the reason the graph is walked transitively rather than the
    view being read: an app almost never touches a layer directly."""
    probe(graphed)
    audit_sharing(graphed)

    rows = findings(graphed, "public-app-private-dep")
    by_app = {r["item_id"]: r for r in rows}
    assert APP_5 in by_app

    evidence = json.loads(by_app[APP_5]["evidence_json"])
    assert evidence["dependency_access"] == "org"
    assert evidence["dependency"].endswith("DevelopmentProjects/FeatureServer")
    # Three hops: app -> web map -> layer. A direct-edge rule would miss this.
    assert len(evidence["path"]) == 3
    assert by_app[APP_5]["severity"] == "critical"
    assert (
        "broken for the public" in by_app[APP_5]["detail"]
        or "Anyone outside" in by_app[APP_5]["detail"]
    )


def test_a_public_app_on_a_restricted_gp_service_is_found(graphed: sqlite3.Connection) -> None:
    probe(graphed)
    audit_sharing(graphed)
    apps = {r["item_id"] for r in findings(graphed, "public-app-private-dep")}
    assert APP_3 in apps  # reaches the org-only plow route optimizer


def test_the_exposure_rule_does_not_fire_on_public_dependencies(
    graphed: sqlite3.Connection,
) -> None:
    probe(graphed)
    audit_sharing(graphed)
    for row in findings(graphed, "public-app-private-dep"):
        assert json.loads(row["evidence_json"])["dependency_access"] != "public"


# ---------------------------------------------------------------------------
# The other rules
# ---------------------------------------------------------------------------


def test_the_departed_owner_is_a_finding(graphed: sqlite3.Connection) -> None:
    audit_sharing(graphed)
    rows = findings(graphed, "orphaned-owner")
    assert [r["item_id"] for r in rows] == ["a0000000000000000000000000000004"]
    assert rows[0]["category"] == "ownership"


def test_a_dev_host_reference_is_a_finding(graphed: sqlite3.Connection) -> None:
    audit_sharing(graphed)
    rows = findings(graphed, "dev-host-reference")
    assert len(rows) == 1
    assert json.loads(rows[0]["evidence_json"])["host"] == "gis-dev.northgate.example.gov"


def test_dev_host_patterns_are_configurable_not_baked_in(graphed: sqlite3.Connection) -> None:
    """No organization's naming convention belongs in this repo."""
    rules = load_rules()
    rules["dev_host_patterns"] = []
    audit_sharing(graphed, rules=rules)
    assert findings(graphed, "dev-host-reference") == []


def test_an_http_dependency_is_a_finding(graphed: sqlite3.Connection) -> None:
    audit_sharing(graphed)
    rows = findings(graphed, "http-service-dependency")
    assert rows
    assert all(r["severity"] == "high" for r in rows)


def test_an_unreachable_dependency_is_a_finding_only_after_probing(
    graphed: sqlite3.Connection,
) -> None:
    audit_sharing(graphed)
    assert findings(graphed, "unreachable-dependency") == []

    probe(graphed)
    audit_sharing(graphed)
    rows = findings(graphed, "unreachable-dependency")
    assert len(rows) == 1
    assert rows[0]["item_id"] == "a0000000000000000000000000000017"


def test_every_finding_carries_an_action(graphed: sqlite3.Connection) -> None:
    """A verdict with no suggested action gets ignored."""
    probe(graphed)
    audit_sharing(graphed)
    rows = graphed.execute("SELECT rule_id, suggested_action, detail FROM finding").fetchall()
    assert rows
    for row in rows:
        assert row["suggested_action"], row["rule_id"]
        assert row["detail"], row["rule_id"]


# ---------------------------------------------------------------------------
# Fingerprint stability --- the property the schema exists to protect
# ---------------------------------------------------------------------------


def test_re_auditing_produces_identical_fingerprints(graphed: sqlite3.Connection) -> None:
    probe(graphed)
    audit_sharing(graphed)
    first = {r["fingerprint"] for r in graphed.execute("SELECT fingerprint FROM finding")}

    second = audit_sharing(graphed)
    after = {r["fingerprint"] for r in graphed.execute("SELECT fingerprint FROM finding")}

    assert after == first
    assert second.new == 0


def test_a_dismissed_finding_is_not_resurrected(graphed: sqlite3.Connection) -> None:
    """The failure mode that makes people stop running scanners."""
    probe(graphed)
    audit_sharing(graphed)

    graphed.execute(
        "UPDATE finding SET status = 'wontfix', status_note = 'accepted by the director', "
        "status_at = '2026-08-06T00:00:00Z' WHERE rule_id = 'orphaned-owner'"
    )
    graphed.commit()

    audit_sharing(graphed)

    row = graphed.execute(
        "SELECT status, status_note FROM finding WHERE rule_id = 'orphaned-owner'"
    ).fetchone()
    assert row["status"] == "wontfix"
    assert row["status_note"] == "accepted by the director"


def test_a_finding_that_stops_firing_is_marked_resolved_not_deleted(
    graphed: sqlite3.Connection,
) -> None:
    """`resolved_run` is observed; `status = 'fixed'` is claimed. Both matter."""
    probe(graphed)
    audit_sharing(graphed)

    fingerprint = graphed.execute(
        "SELECT fingerprint FROM finding WHERE rule_id = 'orphaned-owner'"
    ).fetchone()["fingerprint"]

    # Somebody reassigned the item.
    graphed.execute(
        "UPDATE resource SET owner_exists = 1 WHERE item_id = 'a0000000000000000000000000000004'"
    )
    graphed.commit()

    result = audit_sharing(graphed)
    assert result.resolved >= 1

    row = graphed.execute(
        "SELECT resolved_run, status FROM finding WHERE fingerprint = ?", (fingerprint,)
    ).fetchone()
    assert row is not None  # not deleted
    assert row["resolved_run"] is not None
    assert row["status"] == "open"  # observed resolved, never claimed fixed


def test_a_resolved_finding_that_returns_stops_being_resolved(
    graphed: sqlite3.Connection,
) -> None:
    probe(graphed)
    audit_sharing(graphed)
    graphed.execute(
        "UPDATE resource SET owner_exists = 1 WHERE item_id = 'a0000000000000000000000000000004'"
    )
    graphed.commit()
    audit_sharing(graphed)

    graphed.execute(
        "UPDATE resource SET owner_exists = 0 WHERE item_id = 'a0000000000000000000000000000004'"
    )
    graphed.commit()
    audit_sharing(graphed)

    row = graphed.execute(
        "SELECT resolved_run FROM finding WHERE rule_id = 'orphaned-owner'"
    ).fetchone()
    assert row["resolved_run"] is None


def test_a_reshared_item_produces_a_new_fingerprint_on_the_same_resource(
    conn: sqlite3.Connection,
) -> None:
    """The resource is unchanged; the finding is new. Both must be true at once."""
    crawl_inventory(conn, PortalClient(FixtureTransport(FIXTURE), PORTAL_URL, page_size=10))
    build_dependencies(conn)
    probe(conn)
    audit_sharing(conn)
    before = {r["fingerprint"] for r in conn.execute("SELECT fingerprint FROM finding")}

    # Run 2 re-shares the storm sewer map from org to public.
    crawl_inventory(
        conn,
        PortalClient(FixtureTransport(FIXTURE, overlay="run2"), PORTAL_URL, page_size=10),
    )
    build_dependencies(conn)
    result = audit_sharing(conn)

    after = {r["fingerprint"] for r in conn.execute("SELECT fingerprint FROM finding")}
    assert after > before
    assert result.new == len(after - before) > 0


def test_audit_needs_a_portal(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="no portal"):
        audit_sharing(conn)

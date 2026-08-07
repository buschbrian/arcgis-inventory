"""Exporting Web AppBuilder configuration as migration documentation.

The thing being protected here is a record that becomes unreachable after
Q4 2026, when these apps stop being editable. So the tests care about
completeness (nothing silently dropped), about the custom widgets specifically
(the part no configurable replacement reproduces), and about the file saying
plainly that it is not a converted app.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from arcgis_inventory.audit import audit_sharing, probe_endpoints
from arcgis_inventory.crawl import PortalClient, crawl_inventory
from arcgis_inventory.db import open_database
from arcgis_inventory.dependencies import build_dependencies
from arcgis_inventory.recommend import recommend_targets
from arcgis_inventory.scan import scan_inventory
from arcgis_inventory.transport import FixtureTransport
from arcgis_inventory.wab_export import export_wab_apps

FIXTURE = Path(__file__).parent / "fixtures" / "northgate"
PORTAL_URL = "https://northgate.example.gov/portal"

SIMPLE = "a0000000000000000000000000000001"
MULTI_PAGE = "a0000000000000000000000000000002"
CUSTOM_WIDGET = "a0000000000000000000000000000003"
DEV_HOST = "a0000000000000000000000000000006"
MALFORMED = "a0000000000000000000000000000014"
FORBIDDEN = "a0000000000000000000000000000015"


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    connection = open_database(tmp_path / "inv.sqlite")
    yield connection
    connection.close()


@pytest.fixture
def full(conn: sqlite3.Connection) -> sqlite3.Connection:
    crawl_inventory(conn, PortalClient(FixtureTransport(FIXTURE), PORTAL_URL, page_size=10))
    build_dependencies(conn)
    probe_endpoints(conn, FixtureTransport(FIXTURE, anonymous=True, strict=False), portal_id=1)
    audit_sharing(conn)
    scan_inventory(conn)
    recommend_targets(conn)
    return conn


def load(directory: Path, item_id: str) -> dict:
    return json.loads((directory / f"{item_id}.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# What gets written
# ---------------------------------------------------------------------------


def test_every_readable_wab_app_is_exported(full: sqlite3.Connection, tmp_path: Path) -> None:
    out = tmp_path / "wab"
    result = export_wab_apps(full, out)

    # Nine WAB apps, two of which nobody could read.
    assert result.exported == 7
    assert len(result.skipped) == 2
    assert {s["item_id"] for s in result.skipped} == {MALFORMED, FORBIDDEN}
    assert len(list(out.glob("a*.json"))) == 7


def test_unreadable_apps_are_named_in_the_manifest_not_dropped(
    full: sqlite3.Connection, tmp_path: Path
) -> None:
    """An app nobody could read is the one most at risk of being lost."""
    out = tmp_path / "wab"
    export_wab_apps(full, out)
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["app_count"] == 7
    missing = {entry["item_id"]: entry for entry in manifest["not_exported"]}
    assert set(missing) == {MALFORMED, FORBIDDEN}
    assert missing[MALFORMED]["title"] == "Bike Path Network"
    assert "could not be read" in missing[MALFORMED]["reason"]


def test_only_web_appbuilder_apps_are_exported(full: sqlite3.Connection, tmp_path: Path) -> None:
    out = tmp_path / "wab"
    export_wab_apps(full, out)
    exported = {p.stem for p in out.glob("a*.json")}
    platforms = {
        row["item_id"]
        for row in full.execute("SELECT item_id FROM resource WHERE platform = 'web_appbuilder'")
    }
    assert exported <= platforms


# ---------------------------------------------------------------------------
# What each document says
# ---------------------------------------------------------------------------


def test_the_document_states_it_is_not_a_conversion(
    full: sqlite3.Connection, tmp_path: Path
) -> None:
    """These land in ticket attachments detached from the README, and a JSON
    file next to a retirement deadline invites exactly the wrong assumption."""
    out = tmp_path / "wab"
    export_wab_apps(full, out)

    document = load(out, SIMPLE)
    assert "NOT a converted app" in document["_note"]
    assert "no such converter exists" in document["_note"].lower()

    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert "NOT a converted app" in manifest["_note"]


def test_custom_widgets_are_flagged(full: sqlite3.Connection, tmp_path: Path) -> None:
    """The part of the app no configurable replacement reproduces."""
    out = tmp_path / "wab"
    export_wab_apps(full, out)

    widgets = load(out, CUSTOM_WIDGET)["widgets"]
    custom = [w["name"] for w in widgets if w["custom"]]
    stock = [w["name"] for w in widgets if not w["custom"]]
    assert custom == ["NorthgatePlowStatus"]
    assert "Search" in stock and "Legend" in stock


def test_a_multi_page_app_reports_its_pages_and_all_widgets(
    full: sqlite3.Connection, tmp_path: Path
) -> None:
    """Widgets live in `groups` for a multi-page app; missing them would make a
    complex app read as a simple one in its own documentation."""
    out = tmp_path / "wab"
    export_wab_apps(full, out)

    document = load(out, MULTI_PAGE)
    assert document["app"]["page_count"] == 3
    assert len(document["widgets"]) == 7


def test_search_configuration_is_captured(full: sqlite3.Connection, tmp_path: Path) -> None:
    """The most-rebuilt part of any app, and the easiest to get subtly wrong
    when nobody wrote down which fields it searched."""
    out = tmp_path / "wab"
    export_wab_apps(full, out)

    sources = load(out, DEV_HOST)["search_sources"]
    assert len(sources) == 1
    assert sources[0]["name"] == "Locate Requests"
    assert sources[0]["search_fields"] == ["TICKET_NO"]
    assert "gis-dev" in sources[0]["url"]


def test_theme_and_version_are_recorded(full: sqlite3.Connection, tmp_path: Path) -> None:
    out = tmp_path / "wab"
    export_wab_apps(full, out)
    app = load(out, SIMPLE)["app"]
    assert app["wab_version"] == "2.29"
    assert app["theme"]["name"] == "FoldableTheme"
    assert app["web_map_item_id"]


def test_services_are_listed_separately(full: sqlite3.Connection, tmp_path: Path) -> None:
    out = tmp_path / "wab"
    export_wab_apps(full, out)

    services = load(out, MULTI_PAGE)["services"]
    assert services["geocoder"]
    assert services["print"]

    plow = load(out, CUSTOM_WIDGET)["services"]
    assert plow["geoprocessing"] and "PlowRouteOptimizer" in plow["geoprocessing"][0]


def test_the_analysis_travels_with_the_configuration(
    full: sqlite3.Connection, tmp_path: Path
) -> None:
    """Whoever rebuilds the app should not have to go back to the database to
    find out what was wrong with it or where it was supposed to land."""
    out = tmp_path / "wab"
    export_wab_apps(full, out)

    document = load(out, CUSTOM_WIDGET)
    assert document["recommendation"]["target"] == "custom"
    assert document["recommendation"]["reasoning"]
    rules = {f["rule_id"] for f in document["findings"]}
    assert "wab-custom-widget" in rules
    assert "web-appbuilder-retiring" in rules
    assert document["dependencies"]


def test_dependencies_carry_their_source_path(full: sqlite3.Connection, tmp_path: Path) -> None:
    out = tmp_path / "wab"
    export_wab_apps(full, out)
    deps = load(out, DEV_HOST)["dependencies"]
    assert all("source_path" in dep for dep in deps)
    assert any(dep["relation"] == "widget_config" for dep in deps)


def test_the_raw_configuration_is_retained_underneath(
    full: sqlite3.Connection, tmp_path: Path
) -> None:
    """Anything the exporter does not understand yet is still in the file."""
    out = tmp_path / "wab"
    export_wab_apps(full, out)
    document = load(out, SIMPLE)
    assert document["raw_config"]["wabVersion"] == "2.29"
    assert "widgetPool" in document["raw_config"]


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------


def test_export_works_without_any_analysis_having_run(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """A crawl alone is enough --- the configuration is the point, and the
    analysis is a bonus when present."""
    crawl_inventory(conn, PortalClient(FixtureTransport(FIXTURE), PORTAL_URL, page_size=10))
    out = tmp_path / "wab"
    result = export_wab_apps(conn, out)

    assert result.exported == 7
    document = load(out, SIMPLE)
    assert document["recommendation"] is None
    assert document["findings"] == []
    assert document["dependencies"] == []
    assert document["widgets"]  # the part that matters is still there


def test_export_is_deterministic(full: sqlite3.Connection, tmp_path: Path) -> None:
    """Two exports of an unchanged portal should diff cleanly, apart from the
    timestamp --- that is what makes this useful as a before/after record."""
    first = tmp_path / "one"
    second = tmp_path / "two"
    export_wab_apps(full, first)
    export_wab_apps(full, second)

    for path in first.glob("*.json"):
        a = json.loads(path.read_text(encoding="utf-8"))
        b = json.loads((second / path.name).read_text(encoding="utf-8"))
        a.pop("exported_at", None)
        b.pop("exported_at", None)
        assert a == b, path.name


def test_files_are_written_as_utf8_with_unix_newlines(
    full: sqlite3.Connection, tmp_path: Path
) -> None:
    out = tmp_path / "wab"
    export_wab_apps(full, out)
    raw = (out / "manifest.json").read_bytes()
    assert b"\r\n" not in raw
    raw.decode("utf-8")


def test_export_needs_a_crawl_first(conn: sqlite3.Connection, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no portal"):
        export_wab_apps(conn, tmp_path / "wab")

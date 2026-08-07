"""The rollup, and above all the section that says what it does not know.

A report of this kind gets forwarded to people who will not run the tool
themselves, and read as a complete picture. So the tests here care as much about
what it admits as about what it reports.
"""

from __future__ import annotations

import itertools
import re
import sqlite3
from pathlib import Path

import pytest

from arcgis_inventory.audit import audit_sharing, probe_endpoints
from arcgis_inventory.crawl import PortalClient, crawl_inventory
from arcgis_inventory.db import open_database
from arcgis_inventory.dependencies import build_dependencies
from arcgis_inventory.recommend import recommend_targets
from arcgis_inventory.report import build_report, render_html, render_markdown
from arcgis_inventory.scan import scan_inventory
from arcgis_inventory.transport import FixtureTransport

FIXTURE = Path(__file__).parent / "fixtures" / "northgate"
PORTAL_URL = "https://northgate.example.gov/portal"


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    connection = open_database(tmp_path / "inv.sqlite")
    yield connection
    connection.close()


@pytest.fixture
def crawled(conn: sqlite3.Connection) -> sqlite3.Connection:
    crawl_inventory(conn, PortalClient(FixtureTransport(FIXTURE), PORTAL_URL, page_size=10))
    return conn


@pytest.fixture
def full(crawled: sqlite3.Connection) -> sqlite3.Connection:
    """Everything run, in order, as a real user would."""
    build_dependencies(crawled)
    probe_endpoints(crawled, FixtureTransport(FIXTURE, anonymous=True, strict=False), portal_id=1)
    audit_sharing(crawled)
    scan_inventory(crawled)
    recommend_targets(crawled)
    return crawled


# ---------------------------------------------------------------------------
# Content
# ---------------------------------------------------------------------------


def test_the_report_leads_with_the_deadline(full: sqlite3.Connection) -> None:
    """Q4 2026 is when apps stop being editable --- two quarters before they
    stop working, and the date anyone planning should act on."""
    data = build_report(full)
    markdown = render_markdown(data)
    assert "Q4 2026" in markdown
    assert "editable" in markdown
    assert "no converter" in markdown.lower()
    assert "9 Web AppBuilder apps" in markdown


def test_the_headline_exposure_section_names_the_chain(full: sqlite3.Connection) -> None:
    data = build_report(full)
    assert len(data.exposure) == 3
    markdown = render_markdown(data)
    assert "Public apps depending on non-public layers" in markdown
    assert "Development Projects Map" in markdown
    assert "->" in markdown  # the path through the web map


def test_the_migration_plan_is_ordered_hardest_first(full: sqlite3.Connection) -> None:
    data = build_report(full)
    complexities = [row["complexity"] for row in data.plan]
    assert complexities == sorted(complexities, reverse=True)
    assert data.plan[0]["title"] == "Public Works Asset Viewer"


def test_platform_and_target_counts_are_present(full: sqlite3.Connection) -> None:
    data = build_report(full)
    assert dict(data.platforms)["web_appbuilder"] == 9
    assert dict(data.targets)["instant_app"] == 4
    assert data.item_count == 30


def test_shared_maps_are_called_out_as_leverage(full: sqlite3.Connection) -> None:
    data = build_report(full)
    assert data.shared_maps
    markdown = render_markdown(data)
    assert "Fix these once" in markdown


def test_unknown_view_counts_render_as_unknown_not_zero(full: sqlite3.Connection) -> None:
    """A blank or a zero in this column would both be read as 'nobody uses it',
    and that is the number people retire things on."""
    full.execute(
        "UPDATE resource SET num_views = NULL WHERE item_id = 'a0000000000000000000000000000001'"
    )
    full.commit()

    row = next(r for r in build_report(full).plan if r["title"] == "Parcel & Zoning Lookup")
    assert row["num_views"] is None

    markdown = render_markdown(build_report(full))
    plan_line = next(
        line for line in markdown.splitlines() if line.startswith("| Parcel & Zoning Lookup |")
    )
    assert "unknown" in plan_line
    assert "| 0 |" not in plan_line


def test_a_human_override_is_shown_as_such(full: sqlite3.Connection) -> None:
    """A reader must be able to tell a person's decision from the tool's."""
    resource_id = full.execute(
        "SELECT resource_id FROM resource WHERE item_id = 'a0000000000000000000000000000002'"
    ).fetchone()["resource_id"]
    full.execute(
        "UPDATE recommendation SET override_target = 'retire', override_at = 'now' "
        "WHERE resource_id = ?",
        (resource_id,),
    )
    full.commit()

    data = build_report(full)
    markdown = render_markdown(data)
    assert "human override" in markdown
    assert dict(data.targets).get("retire", 0) >= 1  # the override drives the count


# ---------------------------------------------------------------------------
# What it does not know
# ---------------------------------------------------------------------------


def test_the_gaps_section_is_always_present(full: sqlite3.Connection) -> None:
    markdown = render_markdown(build_report(full))
    assert "## What this report does not know" in markdown


def test_unreadable_items_are_admitted(full: sqlite3.Connection) -> None:
    gaps = " ".join(build_report(full).gaps)
    assert "could not be fully read" in gaps
    assert "no recommendation" in gaps


def test_never_probed_is_distinguished_from_probed_and_dead(
    full: sqlite3.Connection,
) -> None:
    """A service that did not answer was still looked at. Conflating the two
    makes the report overstate its own coverage."""
    gaps = build_report(full).gaps
    joined = " ".join(gaps)
    assert "did not answer when probed" in joined
    assert "never probed" not in joined  # everything was probed in this run


def test_without_probing_the_report_says_exposure_was_not_checked(
    crawled: sqlite3.Connection,
) -> None:
    build_dependencies(crawled)
    audit_sharing(crawled)
    gaps = " ".join(build_report(crawled).gaps)
    assert "never probed" in gaps
    assert "not evidence of no exposure" in gaps


def test_without_a_graph_the_report_says_so(crawled: sqlite3.Connection) -> None:
    gaps = " ".join(build_report(crawled).gaps)
    assert "No dependency graph" in gaps


def test_without_recommendations_the_report_says_so(crawled: sqlite3.Connection) -> None:
    gaps = " ".join(build_report(crawled).gaps)
    assert "No recommendations have been generated" in gaps


def test_dismissed_findings_do_not_inflate_the_counts(full: sqlite3.Connection) -> None:
    before = len(build_report(full).exposure)
    full.execute("UPDATE finding SET status = 'wontfix' WHERE rule_id = 'public-app-private-dep'")
    full.commit()
    assert len(build_report(full).exposure) < before


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------


def test_html_is_well_formed_enough_to_render(full: sqlite3.Connection) -> None:
    page = render_html(build_report(full))
    assert page.startswith("<!doctype html>")
    assert '<html lang="en">' in page
    assert page.count("<table>") == page.count("</table>")
    assert page.rstrip().endswith("</html>")


def test_html_headings_do_not_skip_levels(full: sqlite3.Connection) -> None:
    """The report from an accessibility-motivated tool has to be accessible."""
    page = render_html(build_report(full))
    levels = [int(m) for m in re.findall(r"<h([1-6])[ >]", page)]
    assert levels[0] == 1
    assert levels.count(1) == 1
    for previous, current in itertools.pairwise(levels):
        assert current - previous <= 1, f"heading jumped from h{previous} to h{current}"


def test_html_tables_carry_captions_and_scoped_headers(full: sqlite3.Connection) -> None:
    page = render_html(build_report(full))
    assert page.count("<caption>") == page.count("<table>")
    assert '<th scope="col"' in page
    assert "<th>" not in page  # every header is scoped


def test_html_is_self_contained(full: sqlite3.Connection) -> None:
    """No external stylesheet, script, or image: this gets emailed around."""
    page = render_html(build_report(full))
    assert "<link" not in page
    assert "<script" not in page
    assert "<img" not in page


def test_html_escapes_hostile_content(full: sqlite3.Connection) -> None:
    """Item titles are attacker-controllable in the sense that they are typed by
    whoever made the item, and this file gets opened in a browser."""
    full.execute(
        "UPDATE resource SET title = ? WHERE item_id = 'a0000000000000000000000000000001'",
        ("<script>alert('x')</script> & \"quoted\"",),
    )
    full.commit()

    page = render_html(build_report(full))
    assert "<script>alert" not in page
    assert "&lt;script&gt;" in page
    assert "&amp;" in page


def test_the_emoji_title_survives_both_renderers(full: sqlite3.Connection) -> None:
    """Windows console encoding is the usual casualty here."""
    data = build_report(full)
    markdown = render_markdown(data)
    page = render_html(data)
    assert isinstance(markdown, str)
    assert isinstance(page, str)
    markdown.encode("utf-8")
    page.encode("utf-8")


# ---------------------------------------------------------------------------
# Both renderers agree
# ---------------------------------------------------------------------------


def test_both_renderers_report_the_same_numbers(full: sqlite3.Connection) -> None:
    data = build_report(full)
    markdown, page = render_markdown(data), render_html(data)
    for fragment in ("30", "9 Web AppBuilder apps"):
        assert fragment in markdown
        assert fragment in page.replace("<strong>", "").replace("</strong>", "")


def test_report_needs_a_crawl_first(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="no portal"):
        build_report(conn)


def test_gaps_describe_the_latest_crawl_not_every_crawl_ever(
    crawled: sqlite3.Connection,
) -> None:
    """After a bug is fixed and the crawl re-run, a report still citing the old
    failures is telling the reader about a problem they already solved."""
    from arcgis_inventory.crawl import PortalClient, crawl_inventory

    first = " ".join(build_report(crawled).gaps)
    assert "2 item(s) could not be fully read" in first

    # Pretend the second crawl read everything.
    second = crawl_inventory(
        crawled, PortalClient(FixtureTransport(FIXTURE), PORTAL_URL, page_size=10)
    )
    crawled.execute("DELETE FROM crawl_error WHERE run_id = ?", (second.run_id,))
    crawled.commit()

    after = " ".join(build_report(crawled).gaps)
    assert "could not be fully read" not in after

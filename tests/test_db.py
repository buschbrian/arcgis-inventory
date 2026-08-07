from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from arcgis_inventory.db import SCHEMA_VERSION, open_database
from arcgis_inventory.errors import SchemaError

EXPECTED_TABLES = {
    "portal",
    "run",
    "resource",
    "edge",
    "finding",
    "recommendation",
    "migration",
    "usage_snapshot",
    "crawl_error",
    "schema_version",
}

EXPECTED_VIEWS = {
    "v_public_app_private_dep",
    "v_orphaned",
    "v_host_refs",
    "v_http_services",
    "v_shared_maps",
    "v_retirement_exposure",
    "v_dead",
    "v_migration_burndown",
}


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    connection = open_database(tmp_path / "inv.sqlite")
    yield connection
    connection.close()


def test_schema_creates_every_table_and_view(conn: sqlite3.Connection) -> None:
    names = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")
    }
    assert names >= EXPECTED_TABLES
    assert names >= EXPECTED_VIEWS


def test_every_view_is_queryable(conn: sqlite3.Connection) -> None:
    for view in EXPECTED_VIEWS:
        conn.execute(f"SELECT * FROM {view} LIMIT 1").fetchall()


def test_opening_twice_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "inv.sqlite"
    open_database(path).close()
    conn = open_database(path)
    rows = conn.execute("SELECT COUNT(*) AS n FROM schema_version").fetchone()["n"]
    conn.close()
    assert rows == 1


def test_a_newer_database_is_refused_rather_than_written_to(tmp_path: Path) -> None:
    path = tmp_path / "inv.sqlite"
    open_database(path).close()
    raw = sqlite3.connect(path)
    raw.execute(
        "INSERT INTO schema_version (version, applied_at) VALUES (?, '2026-01-01T00:00:00Z')",
        (SCHEMA_VERSION + 1,),
    )
    raw.commit()
    raw.close()

    with pytest.raises(SchemaError):
        open_database(path)


def test_foreign_keys_are_enforced(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO run (portal_id, started_at, status, mode, tool_version) "
            "VALUES (999, '2026-01-01T00:00:00Z', 'running', 'crawl', '0.1.0')"
        )


def test_the_headline_view_finds_a_public_app_on_a_private_layer(conn: sqlite3.Connection) -> None:
    """v_public_app_private_dep is the query the whole audit-sharing feature exists for."""
    conn.execute(
        "INSERT INTO portal (portal_id, url, kind, added_at) "
        "VALUES (1, 'https://northgate.example.gov/portal', 'enterprise', '2026-01-01T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO run (run_id, portal_id, started_at, status, mode, tool_version) "
        "VALUES (1, 1, '2026-01-01T00:00:00Z', 'complete', 'crawl', '0.1.0')"
    )
    conn.executemany(
        "INSERT INTO resource (resource_id, portal_id, kind, item_id, url_normalized, title, "
        "platform, access, first_seen_run, last_seen_run) VALUES (?,1,?,?,?,?,?,?,1,1)",
        [
            (10, "item", "a" * 32, None, "Public Parcel Viewer", "web_appbuilder", "public"),
            (11, "item", "b" * 32, None, "Parcel Map", "web_map", "public"),
            (
                12,
                "endpoint",
                None,
                "https://services.example.gov/server/rest/services/Parcels/FeatureServer",
                "Parcels",
                "feature_service",
                "org",
            ),
        ],
    )
    conn.executemany(
        "INSERT INTO edge (from_resource, to_resource, relation, source_path, "
        "first_seen_run, last_seen_run) VALUES (?,?,?,?,1,1)",
        [
            (10, 11, "data_source", "/map/itemId"),
            (11, 12, "operational_layer", "/operationalLayers/0"),
        ],
    )

    rows = conn.execute("SELECT * FROM v_public_app_private_dep").fetchall()
    # The direct app -> org-only layer edge is not present; the app reaches the
    # private layer through the web map. The view reports direct edges only, so
    # here it is the *web map* that trips it. Transitive reachability is the
    # `dependencies` subcommand's job, not the view's.
    assert [(r["app_title"], r["dep_title"]) for r in rows] == [("Parcel Map", "Parcels")]

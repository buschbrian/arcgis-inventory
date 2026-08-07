"""Dump each Web AppBuilder app's configuration as migration documentation.

After Q4 2026 a Web AppBuilder app can still run but cannot be opened for
editing --- which means the record of *what it was configured to do* becomes
unreachable at exactly the moment somebody needs it to rebuild the thing. This
writes that record out while it is still readable.

It is documentation, not a conversion. Nothing here produces an Experience
Builder app or an Instant App, and no such converter exists. What it produces
is the answer to "what did this app actually do?", in a form a person can read
during a rebuild and a diff can compare afterwards.

Reads stored documents only --- no network.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__
from .extract import wab_widget_names
from .scan import load_scan_rules

__all__ = ["ExportResult", "build_app_document", "export_wab_apps"]

# Documentation-only, stated inside every file. These land in ticket
# attachments and shared drives, detached from this README, and the first
# assumption anyone makes about a JSON file next to a retirement deadline is
# that it can be imported somewhere.
_DISCLAIMER = (
    "Documentation of an existing Web AppBuilder configuration, exported for "
    "rebuilding it by hand. This is NOT a converted app and cannot be imported "
    "into Experience Builder or anything else; no such converter exists."
)


@dataclass(slots=True)
class ExportResult:
    directory: Path
    exported: int = 0
    skipped: list[dict[str, str]] = field(default_factory=list)
    files: list[Path] = field(default_factory=list)


def export_wab_apps(
    conn: sqlite3.Connection,
    directory: Path,
    *,
    portal_id: int | None = None,
) -> ExportResult:
    """Write one JSON document per Web AppBuilder app, plus a manifest."""
    if portal_id is None:
        row = conn.execute("SELECT MIN(portal_id) AS p FROM portal").fetchone()
        portal_id = None if row is None else row["p"]
    if portal_id is None:
        raise ValueError("no portal in this database; run `inventory` first")

    stock_widgets = set(load_scan_rules().get("stock_wab_widgets", []))
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    result = ExportResult(directory=directory)

    for row in conn.execute(
        "SELECT resource_id, item_id, title, owner, access, num_views, created_at, modified_at, "
        "url, raw_data_json FROM resource "
        "WHERE portal_id = ? AND kind = 'item' AND platform = 'web_appbuilder' "
        "ORDER BY item_id",
        (portal_id,),
    ):
        if not row["raw_data_json"]:
            # An app nobody could read is exactly the one most at risk of being
            # lost, so it goes in the manifest by name rather than vanishing.
            result.skipped.append(
                {
                    "item_id": row["item_id"],
                    "title": row["title"] or "",
                    "reason": "configuration could not be read during the crawl",
                }
            )
            continue

        document = build_app_document(conn, row, stock_widgets)
        path = directory / f"{row['item_id']}.json"
        path.write_text(
            json.dumps(document, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
            newline="",
        )
        result.files.append(path)
        result.exported += 1

    manifest = {
        "_note": _DISCLAIMER,
        "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "tool_version": __version__,
        "app_count": result.exported,
        "apps": [
            {"item_id": p.stem, "file": p.name} for p in sorted(result.files, key=lambda f: f.name)
        ],
        "not_exported": result.skipped,
    }
    manifest_path = directory / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="",
    )
    result.files.append(manifest_path)
    return result


def build_app_document(
    conn: sqlite3.Connection, row: sqlite3.Row, stock_widgets: set[str]
) -> dict[str, Any]:
    """Everything worth knowing about one app, in one readable object."""
    data = json.loads(row["raw_data_json"])

    return {
        "_note": _DISCLAIMER,
        "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "tool_version": __version__,
        "item": {
            "item_id": row["item_id"],
            "title": row["title"],
            "owner": row["owner"],
            "access": row["access"],
            "num_views": row["num_views"],
            "created": row["created_at"],
            "modified": row["modified_at"],
            "url": row["url"],
        },
        "app": {
            "wab_version": data.get("wabVersion"),
            "theme": _theme(data),
            "page_count": _page_count(data),
            "web_map_item_id": (data.get("map") or {}).get("itemId"),
        },
        "widgets": _widgets(data, stock_widgets),
        "search_sources": _search_sources(data),
        "services": _services(data),
        "dependencies": _dependencies(conn, row["resource_id"]),
        "recommendation": _recommendation(conn, row["resource_id"]),
        "findings": _findings(conn, row["resource_id"]),
        # The raw configuration, retained verbatim underneath the readable
        # summary. Anything this exporter does not yet understand is still here.
        "raw_config": data,
    }


def _theme(data: dict[str, Any]) -> dict[str, Any]:
    theme = data.get("theme")
    if not isinstance(theme, dict):
        return {}
    return {
        "name": theme.get("name"),
        "styles": theme.get("styles"),
        "version": theme.get("version"),
    }


def _page_count(data: dict[str, Any]) -> int:
    pool = data.get("widgetPool")
    if isinstance(pool, dict) and isinstance(pool.get("groups"), list):
        return max(1, len(pool["groups"]))
    return 1


def _widgets(data: dict[str, Any], stock_widgets: set[str]) -> list[dict[str, Any]]:
    """Every widget, flagged custom or stock.

    The custom ones are the whole reason to read this file: they are the part of
    the app that no configurable replacement reproduces, and the part whose
    behaviour has to be described to whoever rebuilds it.
    """
    widgets: list[dict[str, Any]] = []
    for name in wab_widget_names(data):
        widgets.append(
            {
                "name": name,
                "custom": name not in stock_widgets,
                "config": _widget_config(data, name),
            }
        )
    return widgets


def _widget_config(data: dict[str, Any], name: str) -> Any:
    """Pull the stored config for a widget by name, wherever it lives."""
    collections: list[Any] = []
    pool = data.get("widgetPool")
    if isinstance(pool, dict):
        collections.append(pool.get("widgets"))
        groups = pool.get("groups")
        if isinstance(groups, list):
            collections += [g.get("widgets") for g in groups if isinstance(g, dict)]
    on_screen = data.get("widgetOnScreen")
    if isinstance(on_screen, dict):
        collections.append(on_screen.get("widgets"))

    for collection in collections:
        if not isinstance(collection, list):
            continue
        for widget in collection:
            if not isinstance(widget, dict):
                continue
            uri = widget.get("uri")
            if isinstance(uri, str) and f"/{name}/" in uri:
                return widget.get("config")
    return None


def _search_sources(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Search configuration is the single most-rebuilt part of any app, and the
    easiest to get subtly wrong when nobody wrote down which fields it searched.
    """
    sources: list[dict[str, Any]] = []
    for widget in _all_widgets(data):
        config = widget.get("config")
        if not isinstance(config, dict):
            continue
        for source in config.get("sources") or []:
            if isinstance(source, dict):
                sources.append(
                    {
                        "name": source.get("name"),
                        "url": source.get("url"),
                        "search_fields": source.get("searchFields"),
                        "display_field": source.get("displayField"),
                        "exact_match": source.get("exactMatch"),
                    }
                )
    return sources


def _all_widgets(data: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    pool = data.get("widgetPool")
    collections: list[Any] = []
    if isinstance(pool, dict):
        collections.append(pool.get("widgets"))
        groups = pool.get("groups")
        if isinstance(groups, list):
            collections += [g.get("widgets") for g in groups if isinstance(g, dict)]
    on_screen = data.get("widgetOnScreen")
    if isinstance(on_screen, dict):
        collections.append(on_screen.get("widgets"))
    for collection in collections:
        if isinstance(collection, list):
            found += [w for w in collection if isinstance(w, dict)]
    return found


def _services(data: dict[str, Any]) -> dict[str, Any]:
    geocoder = data.get("geocoder")
    print_task = data.get("printTask")
    return {
        "geocoder": geocoder.get("url") if isinstance(geocoder, dict) else None,
        "print": print_task.get("url") if isinstance(print_task, dict) else None,
        "geoprocessing": [
            service.get("url")
            for service in data.get("gpServices") or []
            if isinstance(service, dict)
        ],
        "geometry": data.get("geometryService"),
    }


def _dependencies(conn: sqlite3.Connection, resource_id: int) -> list[dict[str, Any]]:
    return [
        {
            "relation": row["relation"],
            "target": row["item_id"] or row["url_normalized"],
            "title": row["title"],
            "source_path": row["source_path"],
        }
        for row in conn.execute(
            "SELECT e.relation, e.source_path, dst.item_id, dst.url_normalized, dst.title "
            "FROM edge e JOIN resource dst ON dst.resource_id = e.to_resource "
            "WHERE e.from_resource = ? ORDER BY e.relation, e.source_path",
            (resource_id,),
        )
    ]


def _recommendation(conn: sqlite3.Connection, resource_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT target, confidence, complexity, reasoning, override_target, override_note "
        "FROM recommendation WHERE resource_id = ?",
        (resource_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "target": row["target"],
        "confidence": row["confidence"],
        "complexity": row["complexity"],
        "reasoning": row["reasoning"],
        "override_target": row["override_target"],
        "override_note": row["override_note"],
    }


def _findings(conn: sqlite3.Connection, resource_id: int) -> list[dict[str, Any]]:
    return [
        {"rule_id": row["rule_id"], "severity": row["severity"], "title": row["title"]}
        for row in conn.execute(
            "SELECT rule_id, severity, title FROM finding WHERE resource_id = ? "
            "AND resolved_run IS NULL AND status IN ('open', 'acknowledged') "
            "ORDER BY rule_id",
            (resource_id,),
        )
    ]

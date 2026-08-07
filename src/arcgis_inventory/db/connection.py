"""Opening, creating, and version-checking the database."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

from ..errors import SchemaError

__all__ = ["SCHEMA_VERSION", "connect", "open_database"]

SCHEMA_VERSION = 1


def open_database(path: Path | str) -> sqlite3.Connection:
    """Open (creating if needed) the inventory database at ``path``.

    ``:memory:`` is honoured for tests.
    """
    target = str(path)
    if target != ":memory:":
        Path(target).parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    # Crawls are long and write-heavy; the default sync level costs real time
    # and the failure mode (losing the tail of an interruptible crawl) is cheap.
    conn.execute("PRAGMA synchronous = NORMAL")

    _apply_schema(conn)
    return conn


@contextmanager
def connect(path: Path | str) -> Iterator[sqlite3.Connection]:
    conn = open_database(path)
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _apply_schema(conn: sqlite3.Connection) -> None:
    existing = _current_version(conn)
    if existing is None:
        conn.executescript(_schema_sql())
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, datetime.now(UTC).isoformat()),
        )
        conn.commit()
        return

    if existing > SCHEMA_VERSION:
        raise SchemaError(
            f"database is at schema version {existing}, this build understands {SCHEMA_VERSION}. "
            "Upgrade arcgis-inventory rather than letting an older build write to it."
        )
    if existing < SCHEMA_VERSION:  # pragma: no cover - no migrations exist yet
        raise SchemaError(
            f"database is at schema version {existing}, expected {SCHEMA_VERSION}. "
            "No migration path is implemented yet; re-crawl into a new file."
        )


def _current_version(conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    if row is None:
        return None
    version = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()["v"]
    return int(version) if version is not None else None


def _schema_sql() -> str:
    return resources.files("arcgis_inventory.db").joinpath("schema.sql").read_text(encoding="utf-8")

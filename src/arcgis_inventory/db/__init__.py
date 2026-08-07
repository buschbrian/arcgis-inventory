"""SQLite store.

SQLite because the crawl has to be re-runnable and diffable --- migration
progress is the whole point, so runs have to be comparable to each other. CSV,
JSON, and Markdown are exports, never the store.
"""

from __future__ import annotations

from .connection import SCHEMA_VERSION, connect, open_database

__all__ = ["SCHEMA_VERSION", "connect", "open_database"]

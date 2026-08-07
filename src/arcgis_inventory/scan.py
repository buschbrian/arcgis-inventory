"""Deprecated-technology scanning, driven by YAML rules.

The rules are data, not code, because the interesting ones are
organization-specific and because a scanner nobody can extend gets replaced by a
spreadsheet within a month. `rules/scan.yaml` ships sensible defaults; `--rules`
replaces the file wholesale.

Runs entirely against stored documents --- no network, same as `reprocess`.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from . import __version__
from .db import store
from .extract import wab_widget_names
from .fingerprint import finding_fingerprint

__all__ = ["Rule", "ScanResult", "load_scan_rules", "scan_inventory"]

DEFAULT_RULES = Path(__file__).parent / "rules" / "scan.yaml"

# How much of a regex match to keep as evidence. Enough to recognize, short
# enough that a finding stays readable and the fingerprint stays stable.
_EVIDENCE_LIMIT = 120


@dataclass(frozen=True, slots=True)
class Rule:
    id: str
    category: str
    severity: str
    title: str
    when: dict[str, Any]
    detail: str | None = None
    suggested_action: str | None = None


@dataclass(slots=True)
class ScanResult:
    run_id: int
    portal_id: int
    findings: dict[str, int] = field(default_factory=dict)
    new: int = 0
    resolved: int = 0
    scanned: int = 0

    @property
    def total(self) -> int:
        return sum(self.findings.values())


def load_scan_rules(rules_dir: Path | None = None) -> dict[str, Any]:
    path = DEFAULT_RULES
    if rules_dir is not None:
        candidate = Path(rules_dir) / "scan.yaml"
        if candidate.is_file():
            path = candidate
    config = dict(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    config["rules"] = [Rule(**entry) for entry in config.get("rules", [])]
    return config


# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Context:
    """Everything the matchers can look at, prepared once per item."""

    resource_id: int
    identity: str
    title: str | None
    platform: str | None
    item_type: str | None
    type_keywords: set[str]
    url: str | None
    num_views: int | None
    modified_at: str | None
    data: Any
    data_text: str
    stock_widgets: set[str]


def scan_inventory(
    conn: sqlite3.Connection,
    *,
    portal_id: int | None = None,
    rules: dict[str, Any] | None = None,
) -> ScanResult:
    """Apply every rule to every stored item, writing findings."""
    if portal_id is None:
        row = conn.execute("SELECT MIN(portal_id) AS p FROM portal").fetchone()
        portal_id = None if row is None else row["p"]
    if portal_id is None:
        raise ValueError("no portal in this database; run `inventory` first")

    config = rules or load_scan_rules()
    rule_list: list[Rule] = list(config.get("rules", []))
    stock_widgets = set(config.get("stock_wab_widgets", []))

    run_id = store.start_run(
        conn,
        portal_id=portal_id,
        mode="reprocess",
        tool_version=__version__,
        rules_version=_rules_version(rule_list),
        scope={"portal_id": portal_id, "stage": "scan", "rule_count": len(rule_list)},
    )

    before = _fingerprints(conn, portal_id)
    counts: dict[str, int] = {}
    scanned = 0

    for row in conn.execute(
        "SELECT resource_id, item_id, url_normalized, title, platform, item_type, "
        "type_keywords, url, num_views, modified_at, raw_data_json FROM resource "
        "WHERE portal_id = ? AND kind = 'item' ORDER BY resource_id",
        (portal_id,),
    ):
        context = _context(row, stock_widgets)
        scanned += 1
        for rule in rule_list:
            matched, evidence = _evaluate(rule.when, context)
            if not matched:
                continue
            store.upsert_finding(
                conn,
                portal_id=portal_id,
                run_id=run_id,
                write=store.FindingWrite(
                    fingerprint=finding_fingerprint(
                        rule.id, context.identity, evidence=evidence or None
                    ),
                    rule_id=rule.id,
                    category=rule.category,
                    severity=rule.severity,
                    title=rule.title,
                    resource_id=context.resource_id,
                    detail=_detail(rule, context),
                    evidence=evidence or None,
                    suggested_action=rule.suggested_action,
                ),
            )
            counts[rule.id] = counts.get(rule.id, 0) + 1

    after = _fingerprints(conn, portal_id)
    resolved = store.resolve_absent_findings(
        conn, portal_id=portal_id, run_id=run_id, rule_ids=[r.id for r in rule_list]
    )

    store.finish_run(
        conn,
        run_id,
        status="complete",
        item_count=scanned,
        error_count=0,
        notes=f"{sum(counts.values())} findings from {len(rule_list)} rules",
    )
    conn.commit()
    return ScanResult(
        run_id=run_id,
        portal_id=portal_id,
        findings=counts,
        new=len(after - before),
        resolved=resolved,
        scanned=scanned,
    )


def _rules_version(rules: list[Rule]) -> str:
    """So a changed finding can be attributed to the rules or to the portal."""
    return str(sorted((r.id, r.severity, str(r.when)) for r in rules))[:200]


def _detail(rule: Rule, context: _Context) -> str:
    prefix = f"{context.title or context.identity}: " if context.title else ""
    return f"{prefix}{(rule.detail or rule.title).strip()}"


def _context(row: sqlite3.Row, stock_widgets: set[str]) -> _Context:
    data_text = row["raw_data_json"] or ""
    try:
        data = json.loads(data_text) if data_text else None
    except ValueError:  # pragma: no cover - stored JSON is written by us
        data = None
    keywords = set(json.loads(row["type_keywords"]) if row["type_keywords"] else [])
    return _Context(
        resource_id=row["resource_id"],
        identity=row["item_id"] or row["url_normalized"] or str(row["resource_id"]),
        title=row["title"],
        platform=row["platform"],
        item_type=row["item_type"],
        type_keywords=keywords,
        url=row["url"],
        num_views=row["num_views"],
        modified_at=row["modified_at"],
        data=data,
        data_text=data_text,
        stock_widgets=stock_widgets,
    )


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def _evaluate(clause: Any, context: _Context) -> tuple[bool, dict[str, Any]]:
    """Return ``(matched, evidence)`` for one `when` clause.

    Evidence is what distinguishes two findings of the same rule on the same
    item, so it goes into the fingerprint. It must therefore hold only stable
    facts --- a matched pattern, not a count or a timestamp.
    """
    if not isinstance(clause, dict) or not clause:
        return False, {}

    evidence: dict[str, Any] = {}

    for key, value in clause.items():
        if key == "all":
            for sub in value:
                matched, sub_evidence = _evaluate(sub, context)
                if not matched:
                    return False, {}
                evidence.update(sub_evidence)
            continue

        if key == "any":
            hit = False
            for sub in value:
                matched, sub_evidence = _evaluate(sub, context)
                if matched:
                    hit = True
                    evidence.update(sub_evidence)
                    break
            if not hit:
                return False, {}
            continue

        if key == "none":
            for sub in value:
                matched, _ = _evaluate(sub, context)
                if matched:
                    return False, {}
            continue

        matcher = _MATCHERS.get(key)
        if matcher is None:
            raise ValueError(f"unknown matcher {key!r} in scan rules")
        matched, sub_evidence = matcher(value, context)
        if not matched:
            return False, {}
        evidence.update(sub_evidence)

    return True, evidence


def _match_platform(value: Any, context: _Context) -> tuple[bool, dict[str, Any]]:
    return (context.platform in set(value), {})


def _match_item_type(value: Any, context: _Context) -> tuple[bool, dict[str, Any]]:
    return (context.item_type in set(value), {})


def _match_type_keyword(value: Any, context: _Context) -> tuple[bool, dict[str, Any]]:
    hits = sorted(context.type_keywords & set(value))
    return (bool(hits), {"type_keyword": hits[0]} if hits else {})


def _match_missing_type_keyword(value: Any, context: _Context) -> tuple[bool, dict[str, Any]]:
    return (not (context.type_keywords & set(value)), {})


def _match_data_matches(value: Any, context: _Context) -> tuple[bool, dict[str, Any]]:
    if not context.data_text:
        return False, {}
    found = re.search(str(value), context.data_text, re.IGNORECASE)
    if found is None:
        return False, {}
    return True, {"matched": found.group(0)[:_EVIDENCE_LIMIT]}


def _match_data_has_key(value: Any, context: _Context) -> tuple[bool, dict[str, Any]]:
    if not isinstance(context.data, dict) or value not in context.data:
        return False, {}
    return True, {"data_key": str(value)}


def _match_url_matches(value: Any, context: _Context) -> tuple[bool, dict[str, Any]]:
    if not context.url:
        return False, {}
    found = re.search(str(value), context.url, re.IGNORECASE)
    return (found is not None, {"url_matched": found.group(0)} if found else {})


def _match_custom_wab_widget(value: Any, context: _Context) -> tuple[bool, dict[str, Any]]:
    """A widget URI outside the stock list.

    Reads the stored config rather than regexing the JSON, because the stock
    list is the whole point and it lives in the rules file.
    """
    if not value or not isinstance(context.data, dict):
        return False, {}

    custom = sorted(
        {name for name in wab_widget_names(context.data) if name not in context.stock_widgets}
    )
    if not custom:
        return False, {}
    return True, {"custom_widgets": custom}


def _match_views_below(value: Any, context: _Context) -> tuple[bool, dict[str, Any]]:
    # NULL views never match: 'unknown usage' is not 'unused', and retiring
    # something on the strength of a missing number is how you delete an app
    # somebody depends on.
    if context.num_views is None:
        return False, {}
    return (context.num_views < int(value), {})


def _match_modified_older_than_days(value: Any, context: _Context) -> tuple[bool, dict[str, Any]]:
    if not context.modified_at:
        return False, {}
    try:
        modified = datetime.fromisoformat(context.modified_at)
    except ValueError:  # pragma: no cover - written by us as ISO
        return False, {}
    if modified.tzinfo is None:
        modified = modified.replace(tzinfo=UTC)
    age = (datetime.now(UTC) - modified).days
    return (age >= int(value), {})


_MATCHERS = {
    "platform": _match_platform,
    "item_type": _match_item_type,
    "type_keyword": _match_type_keyword,
    "missing_type_keyword": _match_missing_type_keyword,
    "data_matches": _match_data_matches,
    "data_has_key": _match_data_has_key,
    "url_matches": _match_url_matches,
    "custom_wab_widget": _match_custom_wab_widget,
    "views_below": _match_views_below,
    "modified_older_than_days": _match_modified_older_than_days,
}


def _fingerprints(conn: sqlite3.Connection, portal_id: int) -> set[str]:
    return {
        row["fingerprint"]
        for row in conn.execute("SELECT fingerprint FROM finding WHERE portal_id = ?", (portal_id,))
    }

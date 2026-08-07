"""Where each application should land, and --- more importantly --- why.

`reasoning` is not decoration. A bare verdict of "Experience Builder" gets
ignored; "single web map, 3 standard widgets, no custom code, 48,210 views in
the last crawl" gets acted on. The real output of this module is the argument,
and the label is a summary of it.

Rules are ordered and the first match wins, because a recommendation is one
verdict rather than an accumulation. The order encodes the priorities: refuse to
guess about items nobody could read, then retire what is dead, then flag the
rewrites, then choose between Instant App and Experience Builder --- biased
toward Instant Apps, which is both Esri's guidance and what most Web AppBuilder
apps in the wild actually are.
"""

from __future__ import annotations

import json
import operator
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from . import __version__
from .classify import is_data_bearing
from .db import store
from .extract import wab_widget_names
from .scan import load_scan_rules

__all__ = ["RecommendResult", "Signals", "load_recommend_rules", "recommend_targets"]

DEFAULT_RULES = Path(__file__).parent / "rules" / "recommend.yaml"

# None-handling lives in `_evaluate`, not here: a missing signal never
# satisfies an ordering comparison, and stating that in one place keeps the
# comparators honest.
_COMPARATORS: dict[str, Callable[[Any, Any], bool]] = {
    "equals": operator.eq,
    "not_equals": operator.ne,
    "in": lambda got, want: bool(got in want),
    "gt": operator.gt,
    "gte": operator.ge,
    "lt": operator.lt,
    "lte": operator.le,
}


@dataclass(frozen=True, slots=True)
class RecommendRule:
    id: str
    target: str
    confidence: str
    when: dict[str, Any]
    because: str


@dataclass(slots=True)
class Signals:
    """The facts a recommendation is argued from."""

    resource_id: int
    item_id: str | None
    title: str | None
    platform: str | None
    data_known: bool = False
    # Whether a configuration document was ever going to exist. A Hub Site has
    # none by design, and treating that as "unreadable" produces a verdict of
    # `unknown` for an item there is nothing wrong with.
    data_expected: bool = False
    num_views: int | None = None
    owner_exists: bool | None = None
    days_since_modified: int | None = None
    widget_count: int = 0
    custom_widget_count: int = 0
    custom_widgets: list[str] = field(default_factory=list)
    page_count: int = 1
    map_count: int = 0
    layer_count: int = 0
    shared_map: bool = False
    uses_gp: bool = False
    uses_print: bool = False
    uses_geocoder: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "data_known": self.data_known,
            "data_expected": self.data_expected,
            "num_views": self.num_views,
            "owner_exists": self.owner_exists,
            "days_since_modified": self.days_since_modified,
            "widget_count": self.widget_count,
            "custom_widget_count": self.custom_widget_count,
            "page_count": self.page_count,
            "map_count": self.map_count,
            "layer_count": self.layer_count,
            "shared_map": self.shared_map,
            "uses_gp": self.uses_gp,
            "uses_print": self.uses_print,
            "uses_geocoder": self.uses_geocoder,
            "platform": self.platform,
        }


@dataclass(slots=True)
class RecommendResult:
    run_id: int
    portal_id: int
    targets: dict[str, int] = field(default_factory=dict)
    considered: int = 0
    overridden: int = 0

    @property
    def total(self) -> int:
        return sum(self.targets.values())


def load_recommend_rules(rules_dir: Path | None = None) -> dict[str, Any]:
    path = DEFAULT_RULES
    if rules_dir is not None:
        candidate = Path(rules_dir) / "recommend.yaml"
        if candidate.is_file():
            path = candidate
    config = dict(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    config["rules"] = [RecommendRule(**entry) for entry in config.get("rules", [])]
    return config


def recommend_targets(
    conn: sqlite3.Connection,
    *,
    portal_id: int | None = None,
    rules: dict[str, Any] | None = None,
) -> RecommendResult:
    """Write a recommendation for every application, with its reasoning."""
    if portal_id is None:
        row = conn.execute("SELECT MIN(portal_id) AS p FROM portal").fetchone()
        portal_id = None if row is None else row["p"]
    if portal_id is None:
        raise ValueError("no portal in this database; run `inventory` first")

    config = rules or load_recommend_rules()
    rule_list: list[RecommendRule] = list(config.get("rules", []))
    platforms = set(config.get("recommend_platforms", []))
    weights = dict(config.get("complexity", {}))
    # The stock widget list lives in scan.yaml, which is its canonical home.
    # Reading it from there rather than duplicating it means the two commands
    # cannot disagree about what counts as custom code.
    stock_widgets = set(
        config.get("stock_wab_widgets") or load_scan_rules().get("stock_wab_widgets", [])
    )

    run_id = store.start_run(
        conn,
        portal_id=portal_id,
        mode="reprocess",
        tool_version=__version__,
        rules_version=str([(r.id, r.target) for r in rule_list])[:200],
        scope={"portal_id": portal_id, "stage": "recommend"},
    )

    targets: dict[str, int] = {}
    considered = 0

    for row in conn.execute(
        "SELECT resource_id, item_id, title, platform, item_type, num_views, owner_exists, "
        "modified_at, raw_data_json FROM resource "
        "WHERE portal_id = ? AND kind = 'item' ORDER BY resource_id",
        (portal_id,),
    ):
        if row["platform"] not in platforms:
            continue
        considered += 1

        signals = _signals(conn, row, stock_widgets)
        rule = _first_match(rule_list, signals)
        target = rule.target if rule else "unknown"
        confidence = rule.confidence if rule else "guess"

        store.upsert_recommendation(
            conn,
            resource_id=signals.resource_id,
            run_id=run_id,
            target=target,
            confidence=confidence,
            complexity=_complexity(signals, weights),
            rules_fired=[rule.id] if rule else [],
            reasoning=_reasoning(rule, signals, target),
        )
        targets[target] = targets.get(target, 0) + 1

    overridden = conn.execute(
        "SELECT COUNT(*) AS n FROM recommendation WHERE override_target IS NOT NULL"
    ).fetchone()["n"]

    store.finish_run(
        conn,
        run_id,
        status="complete",
        item_count=considered,
        error_count=0,
        notes=f"{considered} applications; {overridden} carry a human override",
    )
    conn.commit()
    return RecommendResult(
        run_id=run_id,
        portal_id=portal_id,
        targets=targets,
        considered=considered,
        overridden=overridden,
    )


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------


def _signals(conn: sqlite3.Connection, row: sqlite3.Row, stock_widgets: set[str]) -> Signals:
    data = json.loads(row["raw_data_json"]) if row["raw_data_json"] else None
    signals = Signals(
        resource_id=row["resource_id"],
        item_id=row["item_id"],
        title=row["title"],
        platform=row["platform"],
        data_known=data is not None,
        data_expected=is_data_bearing(row["item_type"]),
        num_views=row["num_views"],
        owner_exists=None if row["owner_exists"] is None else bool(row["owner_exists"]),
        days_since_modified=_age_days(row["modified_at"]),
    )

    if isinstance(data, dict):
        widgets = wab_widget_names(data)
        signals.widget_count = len(widgets)
        signals.custom_widgets = sorted({w for w in widgets if w not in stock_widgets})
        signals.custom_widget_count = len(signals.custom_widgets)
        pool = data.get("widgetPool")
        if isinstance(pool, dict) and isinstance(pool.get("groups"), list):
            signals.page_count = max(1, len(pool["groups"]))

    _graph_signals(conn, signals)
    return signals


def _graph_signals(conn: sqlite3.Connection, signals: Signals) -> None:
    """Counts that only the dependency graph knows.

    All zero when `dependencies` has not been run, which is why the CLI says so
    rather than letting an app look simpler than it is.
    """
    for row in conn.execute(
        "SELECT e.relation, dst.kind, dst.platform, dst.resource_id FROM edge e "
        "JOIN resource dst ON dst.resource_id = e.to_resource WHERE e.from_resource = ?",
        (signals.resource_id,),
    ):
        if row["relation"] == "gp_service":
            signals.uses_gp = True
        elif row["relation"] == "print_service":
            signals.uses_print = True
        elif row["relation"] == "geocoder":
            signals.uses_geocoder = True

        if row["platform"] in ("web_map", "web_scene"):
            signals.map_count += 1
            shared = conn.execute(
                "SELECT COUNT(DISTINCT from_resource) AS n FROM edge WHERE to_resource = ?",
                (row["resource_id"],),
            ).fetchone()["n"]
            if shared > 1:
                signals.shared_map = True
        elif row["kind"] == "endpoint" and row["relation"] in (
            "operational_layer",
            "basemap",
            "widget_config",
        ):
            signals.layer_count += 1

    # Layers reached through the app's web maps count toward its complexity ---
    # they are what has to be re-added in the replacement.
    if signals.map_count:
        signals.layer_count += conn.execute(
            "SELECT COUNT(*) AS n FROM edge inner_e "
            "JOIN resource dst ON dst.resource_id = inner_e.to_resource "
            "WHERE dst.kind = 'endpoint' AND inner_e.from_resource IN ("
            "  SELECT to_resource FROM edge WHERE from_resource = ?)",
            (signals.resource_id,),
        ).fetchone()["n"]


def _age_days(modified_at: str | None) -> int | None:
    if not modified_at:
        return None
    try:
        modified = datetime.fromisoformat(modified_at)
    except ValueError:  # pragma: no cover - written by us as ISO
        return None
    if modified.tzinfo is None:
        modified = modified.replace(tzinfo=UTC)
    return (datetime.now(UTC) - modified).days


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


def _first_match(rules: list[RecommendRule], signals: Signals) -> RecommendRule | None:
    values = signals.as_dict()
    for rule in rules:
        if _evaluate(rule.when, values):
            return rule
    return None


def _evaluate(clause: Any, values: dict[str, Any]) -> bool:
    if not isinstance(clause, dict) or not clause:
        return False

    if "all" in clause:
        return all(_evaluate(sub, values) for sub in clause["all"])
    if "any" in clause:
        return any(_evaluate(sub, values) for sub in clause["any"])
    if "none" in clause:
        return not any(_evaluate(sub, values) for sub in clause["none"])

    name = clause.get("signal")
    if name is None:
        raise ValueError(f"recommendation clause has no signal: {clause!r}")
    if name not in values:
        raise ValueError(f"unknown signal {name!r} in recommendation rules")

    got = values[name]
    for key, want in clause.items():
        if key == "signal":
            continue
        comparator = _COMPARATORS.get(key)
        if comparator is None:
            raise ValueError(f"unknown comparator {key!r} in recommendation rules")
        # A missing value never satisfies a comparison. `num_views` is NULL when
        # usage is unknown, and "unknown" must not read as "zero" --- that is
        # the difference between retiring a dead app and deleting a live one.
        if got is None and key not in ("equals", "not_equals"):
            return False
        if not comparator(got, want):
            return False
    return True


def _complexity(signals: Signals, weights: dict[str, Any]) -> int:
    score = float(weights.get("base", 0))
    score += signals.widget_count * float(weights.get("per_widget", 0))
    score += signals.custom_widget_count * float(weights.get("per_custom_widget", 0))
    score += signals.layer_count * float(weights.get("per_layer", 0))
    score += max(0, signals.page_count - 1) * float(weights.get("per_page", 0))
    if signals.uses_gp:
        score += float(weights.get("uses_gp", 0))
    if signals.uses_print:
        score += float(weights.get("uses_print", 0))
    if signals.uses_geocoder:
        score += float(weights.get("uses_geocoder", 0))
    return max(0, min(100, round(score)))


def _reasoning(rule: RecommendRule | None, signals: Signals, target: str) -> str:
    """The argument, in a sentence someone can disagree with.

    Deliberately built from the same numbers the rule matched on, so a reader
    can check the verdict rather than take it on faith.
    """
    facts = _facts(signals)
    verb = {
        "retire": "Retire",
        "instant_app": "Rebuild as an Instant App",
        "experience_builder": "Rebuild in Experience Builder",
        "custom": "Rebuild as custom development",
        "keep": "Keep as-is",
        "unknown": "No recommendation",
    }.get(target, target)

    if rule is None:
        return (
            f"{verb}: no rule matched. Signals: {facts}. This usually means the rule "
            "set does not cover this platform yet."
        )
    return f"{verb} --- {rule.because.strip()} Signals: {facts}."


def _facts(signals: Signals) -> str:
    parts: list[str] = []

    if signals.map_count:
        parts.append(f"{signals.map_count} web map{'s' if signals.map_count != 1 else ''}")
    if signals.layer_count:
        parts.append(f"{signals.layer_count} layer{'s' if signals.layer_count != 1 else ''}")
    if signals.widget_count:
        parts.append(
            f"{signals.widget_count} widget{'s' if signals.widget_count != 1 else ''}"
            + (f" ({signals.custom_widget_count} custom)" if signals.custom_widget_count else "")
        )
    if signals.page_count > 1:
        parts.append(f"{signals.page_count} pages")
    for flag, label in (
        (signals.uses_gp, "geoprocessing"),
        (signals.uses_print, "printing"),
        (signals.uses_geocoder, "a geocoder"),
    ):
        if flag:
            parts.append(label)
    if signals.shared_map:
        parts.append("a web map shared with other apps")

    # Usage last, because it is the number people argue about.
    if signals.num_views is None:
        parts.append("usage unknown")
    else:
        parts.append(f"{signals.num_views:,} views")
    if signals.owner_exists is False:
        parts.append("no current owner")

    return ", ".join(parts) if parts else "no signals available"

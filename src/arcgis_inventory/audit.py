"""Sharing, ownership, and exposure findings over the dependency graph.

This is the highest-return thing the tool does, because it answers questions
nobody can currently answer and a director immediately understands:

* which **public** apps depend on layers that are *not* public --- i.e. which
  public-facing apps are quietly broken for the public right now
* which apps are owned by accounts that no longer exist
* which production apps reference dev or staging services
* which dependencies are reached over plaintext, or are simply gone

The first of those is the dependency graph read backwards, and it is what turns
this from a migration tool into something an organization keeps running after
the migration is over.

**Probing is not automatic.** Whether the *public* can reach a service cannot be
determined by a crawl authenticated as someone who can see everything --- it
takes an unauthenticated request per endpoint, to hosts that may not even belong
to the organization running the tool. That is somebody else's infrastructure, so
it happens only when explicitly asked for.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import networkx as nx
import yaml

from . import __version__
from .db import store
from .errors import PortalError
from .fingerprint import finding_fingerprint
from .transport import Transport

__all__ = ["AuditResult", "ProbeResult", "audit_sharing", "load_rules", "probe_endpoints"]

DEFAULT_RULES = Path(__file__).parent / "rules" / "sharing.yaml"

# ArcGIS error codes that mean "not for you", as opposed to "not there".
_FORBIDDEN_CODES = frozenset({403, 498, 499})


@dataclass(slots=True)
class ProbeResult:
    probed: int = 0
    public: int = 0
    restricted: int = 0
    unreachable: int = 0


@dataclass(slots=True)
class AuditResult:
    run_id: int
    portal_id: int
    findings: dict[str, int] = field(default_factory=dict)
    new: int = 0
    resolved: int = 0
    unprobed_endpoints: int = 0

    @property
    def total(self) -> int:
        return sum(self.findings.values())


def load_rules(rules_dir: Path | None = None) -> dict[str, Any]:
    """Load the shipped rules, replaced wholesale by a user's file if present."""
    path = DEFAULT_RULES
    if rules_dir is not None:
        candidate = Path(rules_dir) / "sharing.yaml"
        if candidate.is_file():
            path = candidate
    return dict(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------


def probe_endpoints(
    conn: sqlite3.Connection,
    transport: Transport,
    *,
    portal_id: int,
    run_id: int | None = None,
) -> ProbeResult:
    """Ask each service endpoint, *without credentials*, whether it answers.

    The transport passed here must carry no token. A probe made with the
    crawling account's credentials tells you nothing you did not already know
    and would mark every restricted service public --- the single worst wrong
    answer this tool could give.
    """
    endpoints = conn.execute(
        "SELECT resource_id, url_normalized FROM resource "
        "WHERE portal_id = ? AND kind = 'endpoint' ORDER BY resource_id",
        (portal_id,),
    ).fetchall()

    result = ProbeResult()
    for row in endpoints:
        access, reachable, status = _probe_one(transport, row["url_normalized"])
        store.set_endpoint_sharing(
            conn,
            resource_id=row["resource_id"],
            access=access,
            reachable=reachable,
            http_status=status,
        )
        result.probed += 1
        if not reachable:
            result.unreachable += 1
        elif access == "public":
            result.public += 1
        else:
            result.restricted += 1

    conn.commit()
    return result


def _probe_one(transport: Transport, url: str) -> tuple[str | None, bool, int | None]:
    """Return ``(access, reachable, http_status)`` for one endpoint."""
    try:
        reply = transport.get_json(url)
    except PortalError as exc:
        if exc.status in _FORBIDDEN_CODES:
            # It exists and it said no. That is a sharing answer, not an outage.
            return "org", True, exc.status
        return None, False, exc.status
    except Exception:
        return None, False, None

    data = reply.data
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        code = data["error"].get("code")
        if code in _FORBIDDEN_CODES:
            return "org", True, code
        return None, False, code

    return "public", True, reply.status


# ---------------------------------------------------------------------------
# The audit
# ---------------------------------------------------------------------------


def audit_sharing(
    conn: sqlite3.Connection,
    *,
    portal_id: int | None = None,
    rules: dict[str, Any] | None = None,
) -> AuditResult:
    """Apply the sharing and ownership rules, writing findings."""
    if portal_id is None:
        row = conn.execute("SELECT MIN(portal_id) AS p FROM portal").fetchone()
        portal_id = None if row is None else row["p"]
    if portal_id is None:
        raise ValueError("no portal in this database; run `inventory` first")

    config = rules or load_rules()
    run_id = store.start_run(
        conn,
        portal_id=portal_id,
        mode="reprocess",
        tool_version=__version__,
        rules_version=str(sorted(config.get("severities", {}).items())),
        scope={"portal_id": portal_id, "stage": "audit-sharing"},
    )

    graph, nodes = _load_graph(conn, portal_id)
    severities = config.get("severities", {})
    counts: dict[str, int] = {}
    before = _open_fingerprints(conn, portal_id)

    for write in _all_findings(graph, nodes, config, severities):
        store.upsert_finding(conn, portal_id=portal_id, run_id=run_id, write=write)
        counts[write.rule_id] = counts.get(write.rule_id, 0) + 1

    after = _open_fingerprints(conn, portal_id)
    resolved = store.resolve_absent_findings(
        conn, portal_id=portal_id, run_id=run_id, rule_ids=list(severities)
    )

    unprobed = conn.execute(
        "SELECT COUNT(*) AS n FROM resource WHERE portal_id = ? AND kind = 'endpoint' "
        "AND access IS NULL",
        (portal_id,),
    ).fetchone()["n"]

    store.finish_run(
        conn,
        run_id,
        status="complete",
        item_count=len(nodes),
        error_count=0,
        notes=f"{sum(counts.values())} findings across {len(counts)} rules",
    )
    conn.commit()

    return AuditResult(
        run_id=run_id,
        portal_id=portal_id,
        findings=counts,
        new=len(after - before),
        resolved=resolved,
        unprobed_endpoints=unprobed,
    )


def _load_graph(
    conn: sqlite3.Connection, portal_id: int
) -> tuple[nx.DiGraph, dict[int, sqlite3.Row]]:
    graph = nx.DiGraph()
    nodes = {
        row["resource_id"]: row
        for row in conn.execute(
            "SELECT resource_id, kind, item_id, url_normalized, title, platform, access, "
            "owner, owner_exists, is_https, host, reachable FROM resource WHERE portal_id = ?",
            (portal_id,),
        )
    }
    graph.add_nodes_from(nodes)
    for edge in conn.execute(
        "SELECT e.from_resource, e.to_resource, e.relation, e.source_path FROM edge e "
        "JOIN resource r ON r.resource_id = e.from_resource WHERE r.portal_id = ?",
        (portal_id,),
    ):
        graph.add_edge(
            edge["from_resource"],
            edge["to_resource"],
            relation=edge["relation"],
            source_path=edge["source_path"],
        )
    return graph, nodes


def _identity(row: sqlite3.Row) -> str:
    """Stable across databases: never the autoincrement resource_id."""
    return row["item_id"] or row["url_normalized"] or f"resource:{row['resource_id']}"


def _label(row: sqlite3.Row) -> str:
    return row["title"] or _identity(row)


def _all_findings(
    graph: nx.DiGraph,
    nodes: dict[int, sqlite3.Row],
    config: dict[str, Any],
    severities: dict[str, str],
) -> list[store.FindingWrite]:
    findings: list[store.FindingWrite] = []
    findings += _public_app_private_dep(graph, nodes, config, severities)
    findings += _orphaned_owners(nodes, severities)
    findings += _dev_host_references(graph, nodes, config, severities)
    findings += _insecure_and_broken(graph, nodes, severities)
    return findings


def _public_app_private_dep(
    graph: nx.DiGraph,
    nodes: dict[int, sqlite3.Row],
    config: dict[str, Any],
    severities: dict[str, str],
) -> list[store.FindingWrite]:
    """The headline rule, walked **transitively**.

    An app almost never touches a layer directly --- it goes app -> web map ->
    layer. Reporting only direct edges would miss essentially every real
    instance of this problem, which is why the graph gets walked rather than
    the view being read.
    """
    exposed = set(config.get("exposed_platforms", []))
    out: list[store.FindingWrite] = []

    for resource_id, row in nodes.items():
        if row["kind"] != "item" or row["platform"] not in exposed:
            continue
        if row["access"] != "public":
            continue

        for target in nx.descendants(graph, resource_id):
            dep = nodes[target]
            # NULL access means "not established" --- an unprobed endpoint. It
            # must never be reported as private; that is the false positive that
            # would train people to ignore this rule.
            if dep["access"] is None or dep["access"] == "public":
                continue

            path = nx.shortest_path(graph, resource_id, target)
            hops = [_label(nodes[n]) for n in path]
            out.append(
                store.FindingWrite(
                    fingerprint=finding_fingerprint(
                        "public-app-private-dep",
                        _identity(row),
                        evidence={"dep": _identity(dep)},
                    ),
                    rule_id="public-app-private-dep",
                    category="sharing",
                    severity=severities.get("public-app-private-dep", "critical"),
                    title=f"Publicly shared item depends on non-public {_label(dep)}",
                    resource_id=resource_id,
                    detail=(
                        f"{_label(row)} is shared publicly but reaches "
                        f"{_label(dep)} (access: {dep['access']}) via "
                        + " -> ".join(hops)
                        + ". Anyone outside the organization sees it broken."
                    ),
                    evidence={
                        "app": _identity(row),
                        "dependency": _identity(dep),
                        "dependency_access": dep["access"],
                        "path": [_identity(nodes[n]) for n in path],
                    },
                    suggested_action=(
                        "Either share the dependency publicly or stop sharing the app "
                        "publicly. Confirm which was intended before changing either."
                    ),
                )
            )
    return out


def _orphaned_owners(
    nodes: dict[int, sqlite3.Row], severities: dict[str, str]
) -> list[store.FindingWrite]:
    return [
        store.FindingWrite(
            fingerprint=finding_fingerprint("orphaned-owner", _identity(row)),
            rule_id="orphaned-owner",
            category="ownership",
            severity=severities.get("orphaned-owner", "medium"),
            title=f"Owner {row['owner']} no longer exists",
            resource_id=resource_id,
            detail=(
                f"{_label(row)} is owned by {row['owner']}, which is not in the "
                "portal's user list. Nobody can currently edit or retire it."
            ),
            evidence={"owner": row["owner"]},
            suggested_action="Reassign to a current owner, or retire the item.",
        )
        for resource_id, row in nodes.items()
        if row["kind"] == "item" and row["owner_exists"] == 0
    ]


def _dev_host_references(
    graph: nx.DiGraph,
    nodes: dict[int, sqlite3.Row],
    config: dict[str, Any],
    severities: dict[str, str],
) -> list[store.FindingWrite]:
    patterns = [p.lower() for p in config.get("dev_host_patterns", [])]
    out: list[store.FindingWrite] = []

    for target, dep in nodes.items():
        host = (dep["host"] or "").lower()
        matched = next((p for p in patterns if p in host), None)
        if matched is None:
            continue
        for source in graph.predecessors(target):
            row = nodes[source]
            out.append(
                store.FindingWrite(
                    fingerprint=finding_fingerprint(
                        "dev-host-reference",
                        _identity(row),
                        evidence={"dep": _identity(dep)},
                    ),
                    rule_id="dev-host-reference",
                    category="hygiene",
                    severity=severities.get("dev-host-reference", "high"),
                    title=f"References a non-production host ({dep['host']})",
                    resource_id=source,
                    detail=(
                        f"{_label(row)} depends on {dep['url_normalized']}, whose host "
                        f"matches the non-production pattern {matched!r}."
                    ),
                    evidence={"host": dep["host"], "pattern": matched},
                    suggested_action=(
                        "Repoint at the production service, or confirm this is intentional."
                    ),
                )
            )
    return out


def _insecure_and_broken(
    graph: nx.DiGraph, nodes: dict[int, sqlite3.Row], severities: dict[str, str]
) -> list[store.FindingWrite]:
    out: list[store.FindingWrite] = []

    for target, dep in nodes.items():
        if dep["kind"] != "endpoint":
            continue

        for source in graph.predecessors(target):
            row = nodes[source]
            if dep["is_https"] == 0:
                out.append(
                    store.FindingWrite(
                        fingerprint=finding_fingerprint(
                            "http-service-dependency",
                            _identity(row),
                            evidence={"dep": _identity(dep)},
                        ),
                        rule_id="http-service-dependency",
                        category="deprecated_tech",
                        severity=severities.get("http-service-dependency", "high"),
                        title="Depends on a service over plaintext HTTP",
                        resource_id=source,
                        detail=(
                            f"{_label(row)} reaches {dep['url_normalized']} over http://. "
                            "Browsers block this from an https page, so the layer silently "
                            "fails to load."
                        ),
                        evidence={"dependency": _identity(dep)},
                        suggested_action="Serve the service over HTTPS and update the reference.",
                    )
                )

            if dep["reachable"] == 0:
                out.append(
                    store.FindingWrite(
                        fingerprint=finding_fingerprint(
                            "unreachable-dependency",
                            _identity(row),
                            evidence={"dep": _identity(dep)},
                        ),
                        rule_id="unreachable-dependency",
                        category="reachability",
                        severity=severities.get("unreachable-dependency", "high"),
                        title="Depends on a service that did not respond",
                        resource_id=source,
                        detail=(
                            f"{_label(row)} references {dep['url_normalized']}, which returned "
                            "no usable response when probed."
                        ),
                        evidence={"dependency": _identity(dep)},
                        suggested_action="Repair or remove the reference.",
                    )
                )
    return out


def _open_fingerprints(conn: sqlite3.Connection, portal_id: int) -> set[str]:
    return {
        row["fingerprint"]
        for row in conn.execute("SELECT fingerprint FROM finding WHERE portal_id = ?", (portal_id,))
    }

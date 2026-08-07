"""Roll the database up into something a person will actually read.

Two renderers over one gathered structure, so Markdown and HTML cannot drift
apart and both are testable without touching a filesystem.

The section that matters most is the last one. Every report of this kind is
read as a complete picture, so it has to say plainly what it *did not* look at:
items whose configuration could not be read, services whose sharing was never
probed, apps with no recommendation. A rollup that omits its own gaps invites
the reader to conclude that everything not mentioned is fine.

The HTML is written to the accessibility standard the rest of this project
argues for --- real headings in order, table captions and scoped headers,
meaning never carried by colour alone --- because shipping an inaccessible
report from an accessibility-motivated tool would be embarrassing.
"""

from __future__ import annotations

import html
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

__all__ = ["ReportData", "build_report", "render_html", "render_markdown"]

SEVERITY_ORDER = ("critical", "high", "medium", "low", "info")

_TARGET_LABELS = {
    "retire": "Retire",
    "instant_app": "Instant App",
    "experience_builder": "Experience Builder",
    "custom": "Custom development",
    "keep": "Keep as-is",
    "unknown": "No recommendation",
}


@dataclass(slots=True)
class ReportData:
    portal_url: str
    portal_name: str | None
    portal_kind: str
    generated_at: str
    crawl_started_at: str | None
    crawl_status: str | None
    item_count: int
    platforms: list[tuple[str, int]] = field(default_factory=list)
    targets: list[tuple[str, int]] = field(default_factory=list)
    severities: list[tuple[str, int]] = field(default_factory=list)
    exposure: list[dict[str, Any]] = field(default_factory=list)
    shared_maps: list[dict[str, Any]] = field(default_factory=list)
    orphans: list[dict[str, Any]] = field(default_factory=list)
    plan: list[dict[str, Any]] = field(default_factory=list)
    findings_by_rule: list[dict[str, Any]] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)

    @property
    def wab_count(self) -> int:
        return dict(self.platforms).get("web_appbuilder", 0)


def build_report(conn: sqlite3.Connection, *, portal_id: int | None = None) -> ReportData:
    """Gather everything the renderers need. One pass, no rendering decisions."""
    if portal_id is None:
        row = conn.execute("SELECT MIN(portal_id) AS p FROM portal").fetchone()
        portal_id = None if row is None else row["p"]
    if portal_id is None:
        raise ValueError("no portal in this database; run `inventory` first")

    portal = conn.execute(
        "SELECT url, name, kind FROM portal WHERE portal_id = ?", (portal_id,)
    ).fetchone()
    crawl = conn.execute(
        "SELECT started_at, status FROM run WHERE portal_id = ? AND mode = 'crawl' "
        "ORDER BY run_id DESC LIMIT 1",
        (portal_id,),
    ).fetchone()

    data = ReportData(
        portal_url=portal["url"],
        portal_name=portal["name"],
        portal_kind=portal["kind"],
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        crawl_started_at=crawl["started_at"] if crawl else None,
        crawl_status=crawl["status"] if crawl else None,
        item_count=conn.execute(
            "SELECT COUNT(*) AS n FROM resource WHERE portal_id = ? AND kind = 'item'",
            (portal_id,),
        ).fetchone()["n"],
    )

    data.platforms = [
        (row["platform"] or "unclassified", row["n"])
        for row in conn.execute(
            "SELECT platform, COUNT(*) AS n FROM resource WHERE portal_id = ? AND kind = 'item' "
            "GROUP BY platform ORDER BY n DESC, platform",
            (portal_id,),
        )
    ]

    data.targets = [
        (row["target"], row["n"])
        for row in conn.execute(
            "SELECT COALESCE(c.override_target, c.target) AS target, COUNT(*) AS n "
            "FROM recommendation c JOIN resource r USING (resource_id) "
            "WHERE r.portal_id = ? GROUP BY target ORDER BY n DESC, target",
            (portal_id,),
        )
    ]

    data.severities = [
        (row["severity"], row["n"])
        for row in conn.execute(
            "SELECT severity, COUNT(*) AS n FROM finding WHERE portal_id = ? "
            "AND resolved_run IS NULL AND status IN ('open', 'acknowledged') "
            "GROUP BY severity",
            (portal_id,),
        )
    ]
    data.severities.sort(key=lambda pair: SEVERITY_ORDER.index(pair[0]))

    data.findings_by_rule = [
        dict(row)
        for row in conn.execute(
            "SELECT rule_id, severity, COUNT(*) AS n, MIN(title) AS title FROM finding "
            "WHERE portal_id = ? AND resolved_run IS NULL AND status IN ('open', 'acknowledged') "
            "GROUP BY rule_id, severity",
            (portal_id,),
        )
    ]
    data.findings_by_rule.sort(key=lambda r: (SEVERITY_ORDER.index(r["severity"]), -r["n"]))

    data.exposure = [
        dict(row)
        for row in conn.execute(
            "SELECT r.title, r.item_id, f.detail, f.suggested_action FROM finding f "
            "JOIN resource r USING (resource_id) WHERE f.portal_id = ? "
            "AND f.rule_id = 'public-app-private-dep' AND f.resolved_run IS NULL "
            "AND f.status IN ('open', 'acknowledged') ORDER BY r.title",
            (portal_id,),
        )
    ]

    data.shared_maps = [dict(row) for row in conn.execute("SELECT * FROM v_shared_maps")]
    data.shared_maps.sort(key=lambda r: (-r["app_count"], r["title"] or ""))

    data.orphans = [dict(row) for row in conn.execute("SELECT * FROM v_orphaned")]

    data.plan = [
        dict(row)
        for row in conn.execute(
            "SELECT r.title, r.item_id, r.num_views, r.access, "
            "COALESCE(c.override_target, c.target) AS target, c.confidence, c.complexity, "
            "c.reasoning, c.override_target FROM recommendation c "
            "JOIN resource r USING (resource_id) WHERE r.portal_id = ? "
            "ORDER BY c.complexity DESC, r.title",
            (portal_id,),
        )
    ]

    data.gaps = _gaps(conn, portal_id, data)
    return data


def _gaps(conn: sqlite3.Connection, portal_id: int, data: ReportData) -> list[str]:
    """What this report does not know. Never omit this section."""
    gaps: list[str] = []

    # Only the most recent crawl counts. Errors from a superseded run describe a
    # portal, and a tool, that no longer exist --- after a bug is fixed and the
    # crawl re-run, a report still citing the old failures is telling the reader
    # about a problem they already solved.
    errors = conn.execute(
        "SELECT COUNT(*) AS n FROM crawl_error WHERE phase IN ('item', 'item_data') AND run_id = ("
        "  SELECT MAX(run_id) FROM run WHERE portal_id = ? AND mode = 'crawl')",
        (portal_id,),
    ).fetchone()["n"]
    if errors:
        gaps.append(
            f"{errors} item(s) could not be fully read during the crawl. Their configuration "
            "was not analysed, so any conclusion about them is weaker than it looks. See the "
            "`crawl_error` table."
        )

    # Ownership is the gap most likely to be misread as good news. An anonymous
    # or under-permissioned crawl cannot list users, so `owner_exists` is
    # unknown for everything, `v_orphaned` returns nothing, and the report's
    # "no current owner" section is empty --- which reads exactly like "there
    # are no orphans" unless it says otherwise.
    unknown_owner = conn.execute(
        "SELECT COUNT(*) AS n FROM resource WHERE portal_id = ? AND kind = 'item' "
        "AND owner_exists IS NULL",
        (portal_id,),
    ).fetchone()["n"]
    if unknown_owner:
        gaps.append(
            f"Ownership could not be established for {unknown_owner} item(s) --- the portal's "
            "user list was not readable by the account that crawled. No orphaned-owner findings "
            "could be produced, and an empty 'no current owner' section above means the check "
            "did not run, not that everything is owned."
        )

    # Never probed at all, versus probed and did not answer. The first is a gap
    # in the audit; the second is a finding about the portal, and conflating
    # them makes the report overstate what it looked at.
    unprobed = conn.execute(
        "SELECT COUNT(*) AS n FROM resource WHERE portal_id = ? AND kind = 'endpoint' "
        "AND access IS NULL AND reachable IS NULL",
        (portal_id,),
    ).fetchone()["n"]
    if unprobed:
        gaps.append(
            f"Sharing is unknown for {unprobed} service endpoint(s) --- they were never probed, "
            "so the public-exposure check could not run against them. Re-run "
            "`audit-sharing --probe` to establish it. **Absence of exposure findings here is "
            "not evidence of no exposure.**"
        )

    unreachable = conn.execute(
        "SELECT COUNT(*) AS n FROM resource WHERE portal_id = ? AND kind = 'endpoint' "
        "AND reachable = 0",
        (portal_id,),
    ).fetchone()["n"]
    if unreachable:
        gaps.append(
            f"{unreachable} service endpoint(s) did not answer when probed, so their sharing "
            "could not be established either. Whatever depends on them is already broken."
        )

    if not conn.execute("SELECT COUNT(*) AS n FROM edge").fetchone()["n"]:
        gaps.append(
            "No dependency graph has been built. Run `dependencies` --- without it every app "
            "looks like a single-map app and the recommendations skew the same way for all of "
            "them."
        )

    unknown = dict(data.targets).get("unknown", 0)
    if unknown:
        gaps.append(
            f"{unknown} application(s) have no recommendation, because their configuration "
            "could not be read. They still need a decision; the tool declines to guess."
        )

    if not data.targets:
        gaps.append("No recommendations have been generated. Run `recommend`.")

    unknown_views = conn.execute(
        "SELECT COUNT(*) AS n FROM resource WHERE portal_id = ? AND kind = 'item' "
        "AND num_views IS NULL",
        (portal_id,),
    ).fetchone()["n"]
    if unknown_views:
        gaps.append(
            f"{unknown_views} item(s) report no view count. Unknown usage is not zero usage, "
            "and nothing here treats it as such."
        )

    return gaps


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------

DEADLINE_NOTE = (
    "Web AppBuilder apps in ArcGIS Online stop being **editable** in Q4 2026 and stop "
    "**working** in Q2 2027. Q4 2026 is the date to plan against: after it an app still runs, "
    "but it cannot be changed --- which means it cannot be fixed either. There is no converter "
    "to Experience Builder, from Esri or anyone else."
)


def render_markdown(data: ReportData) -> str:
    out: list[str] = []
    add = out.append

    add(f"# ArcGIS portal inventory --- {data.portal_name or data.portal_url}")
    add("")
    add(f"- **Portal:** {data.portal_url} ({data.portal_kind})")
    add(f"- **Items crawled:** {data.item_count:,}")
    if data.crawl_started_at:
        add(f"- **Last crawl:** {data.crawl_started_at} ({data.crawl_status})")
    add(f"- **Report generated:** {data.generated_at}")
    add("")

    add("## The deadline")
    add("")
    add(DEADLINE_NOTE)
    add("")
    if data.wab_count:
        add(
            f"This portal has **{data.wab_count} Web AppBuilder "
            f"app{'s' if data.wab_count != 1 else ''}**."
        )
    else:
        add("No Web AppBuilder apps were found in this portal.")
    add("")

    if data.exposure:
        add("## Public apps depending on non-public layers")
        add("")
        add(
            "These are shared publicly but reach something that is not. To anyone outside the "
            "organization they are broken right now."
        )
        add("")
        for row in data.exposure:
            add(f"- **{row['title']}** --- {row['detail']}")
        add("")

    add("## Where things should go")
    add("")
    if data.targets:
        add("| Target | Apps |")
        add("| --- | ---: |")
        for target, count in data.targets:
            add(f"| {_TARGET_LABELS.get(target, target)} | {count} |")
    else:
        add("_No recommendations generated. Run `recommend`._")
    add("")

    if data.plan:
        add("### Migration plan, hardest first")
        add("")
        add("| App | Target | Confidence | Complexity | Views |")
        add("| --- | --- | --- | ---: | ---: |")
        for row in data.plan:
            views = "unknown" if row["num_views"] is None else f"{row['num_views']:,}"
            label = _TARGET_LABELS.get(row["target"], row["target"])
            if row["override_target"]:
                label += " (human override)"
            add(
                f"| {row['title']} | {label} | {row['confidence']} | "
                f"{row['complexity']} | {views} |"
            )
        add("")

    add("## What is in the portal")
    add("")
    add("| Platform | Items |")
    add("| --- | ---: |")
    for platform, count in data.platforms:
        add(f"| {platform} | {count} |")
    add("")

    if data.findings_by_rule:
        add("## Findings")
        add("")
        add("| Severity | Rule | Items |")
        add("| --- | --- | ---: |")
        for row in data.findings_by_rule:
            add(f"| {row['severity']} | {row['rule_id']} | {row['n']} |")
        add("")

    if data.shared_maps:
        add("## Web maps shared between apps")
        add("")
        add("Fix these once and several apps improve at the same time.")
        add("")
        add("| Web map | Used by |")
        add("| --- | ---: |")
        for row in data.shared_maps:
            add(f"| {row['title']} | {row['app_count']} apps |")
        add("")

    if data.orphans:
        add("## Items with no current owner")
        add("")
        for row in data.orphans:
            add(f"- **{row['title']}** --- owner `{row['owner']}` no longer exists")
        add("")

    add("## What this report does not know")
    add("")
    if data.gaps:
        for gap in data.gaps:
            add(f"- {gap}")
    else:
        add("- Nothing outstanding: every item was read and every endpoint was probed.")
    add("")

    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

_CSS = """
:root { color-scheme: light dark; }
body {
  font: 16px/1.6 system-ui, -apple-system, "Segoe UI", sans-serif;
  max-width: 60rem; margin: 0 auto; padding: 2rem 1rem; }
h1, h2, h3 { line-height: 1.25; }
h1 { font-size: 1.9rem; }
h2 { font-size: 1.4rem; margin-top: 2.5rem;
     border-bottom: 1px solid currentColor; padding-bottom: .25rem; }
h3 { font-size: 1.1rem; margin-top: 1.75rem; }
table { border-collapse: collapse; width: 100%; margin: 1rem 0; }
caption { text-align: left; font-weight: 600; padding-bottom: .5rem; }
th, td { text-align: left; padding: .4rem .6rem; border-bottom: 1px solid #8884; }
th[scope="col"] { border-bottom-width: 2px; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
.sev { font-weight: 600; }
.gaps { border-left: 4px solid #8886; padding: .5rem 0 .5rem 1rem; }
.meta { list-style: none; padding: 0; }
.meta li { margin: .15rem 0; }
"""


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _table(caption: str, headers: list[tuple[str, bool]], rows: list[list[Any]]) -> str:
    """A table with a caption and scoped headers, because a screen reader user
    should be able to tell what they are in."""
    numeric = ' class="num"'
    head = "".join(
        '<th scope="col"' + (numeric if is_num else "") + ">" + _esc(label) + "</th>"
        for label, is_num in headers
    )
    body = []
    for row in rows:
        cells = "".join(
            "<td" + (numeric if headers[i][1] else "") + ">" + _esc(value) + "</td>"
            for i, value in enumerate(row)
        )
        body.append("<tr>" + cells + "</tr>")
    return (
        f"<table><caption>{_esc(caption)}</caption>"
        f"<thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"
    )


def render_html(data: ReportData) -> str:
    title = f"ArcGIS portal inventory — {data.portal_name or data.portal_url}"
    parts: list[str] = [
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{_esc(title)}</title>",
        f"<style>{_CSS}</style>",
        "</head>",
        "<body>",
        "<main>",
        f"<h1>{_esc(title)}</h1>",
        '<ul class="meta">',
        f"<li><strong>Portal:</strong> {_esc(data.portal_url)} ({_esc(data.portal_kind)})</li>",
        f"<li><strong>Items crawled:</strong> {data.item_count:,}</li>",
    ]
    if data.crawl_started_at:
        parts.append(
            f"<li><strong>Last crawl:</strong> {_esc(data.crawl_started_at)} "
            f"({_esc(data.crawl_status)})</li>"
        )
    parts.append(f"<li><strong>Report generated:</strong> {_esc(data.generated_at)}</li>")
    parts.append("</ul>")

    parts.append("<h2>The deadline</h2>")
    parts.append(
        "<p>Web AppBuilder apps in ArcGIS Online stop being <strong>editable</strong> in "
        "Q4 2026 and stop <strong>working</strong> in Q2 2027. Q4 2026 is the date to plan "
        "against: after it an app still runs, but it cannot be changed &mdash; which means it "
        "cannot be fixed either. There is no converter to Experience Builder, from Esri or "
        "anyone else.</p>"
    )
    if data.wab_count:
        parts.append(
            f"<p>This portal has <strong>{data.wab_count} Web AppBuilder "
            f"app{'s' if data.wab_count != 1 else ''}</strong>.</p>"
        )
    else:
        parts.append("<p>No Web AppBuilder apps were found in this portal.</p>")

    if data.exposure:
        parts.append("<h2>Public apps depending on non-public layers</h2>")
        parts.append(
            "<p>These are shared publicly but reach something that is not. To anyone outside "
            "the organization they are broken right now.</p>"
        )
        parts.append("<ul>")
        for row in data.exposure:
            parts.append(f"<li><strong>{_esc(row['title'])}</strong> &mdash; {_esc(row['detail'])}")
            if row["suggested_action"]:
                parts.append(f"<br><em>{_esc(row['suggested_action'])}</em>")
            parts.append("</li>")
        parts.append("</ul>")

    parts.append("<h2>Where things should go</h2>")
    if data.targets:
        parts.append(
            _table(
                "Recommended target by application count",
                [("Target", False), ("Apps", True)],
                [[_TARGET_LABELS.get(t, t), n] for t, n in data.targets],
            )
        )
    else:
        parts.append("<p>No recommendations generated. Run <code>recommend</code>.</p>")

    if data.plan:
        parts.append("<h3>Migration plan, hardest first</h3>")
        parts.append(
            _table(
                "Applications ranked by estimated effort",
                [
                    ("App", False),
                    ("Target", False),
                    ("Confidence", False),
                    ("Complexity", True),
                    ("Views", True),
                ],
                [
                    [
                        row["title"],
                        _TARGET_LABELS.get(row["target"], row["target"])
                        + (" (human override)" if row["override_target"] else ""),
                        row["confidence"],
                        row["complexity"],
                        "unknown" if row["num_views"] is None else f"{row['num_views']:,}",
                    ]
                    for row in data.plan
                ],
            )
        )

    parts.append("<h2>What is in the portal</h2>")
    parts.append(
        _table(
            "Items by platform",
            [("Platform", False), ("Items", True)],
            [[p, n] for p, n in data.platforms],
        )
    )

    if data.findings_by_rule:
        parts.append("<h2>Findings</h2>")
        parts.append(
            _table(
                "Open findings by rule",
                [("Severity", False), ("Rule", False), ("Items", True)],
                [[r["severity"], r["rule_id"], r["n"]] for r in data.findings_by_rule],
            )
        )

    if data.shared_maps:
        parts.append("<h2>Web maps shared between apps</h2>")
        parts.append("<p>Fix these once and several apps improve at the same time.</p>")
        parts.append(
            _table(
                "Web maps used by more than one application",
                [("Web map", False), ("Used by", True)],
                [[r["title"], r["app_count"]] for r in data.shared_maps],
            )
        )

    if data.orphans:
        parts.append("<h2>Items with no current owner</h2>")
        parts.append("<ul>")
        for row in data.orphans:
            parts.append(
                f"<li><strong>{_esc(row['title'])}</strong> &mdash; owner "
                f"<code>{_esc(row['owner'])}</code> no longer exists</li>"
            )
        parts.append("</ul>")

    parts.append("<h2>What this report does not know</h2>")
    parts.append('<div class="gaps">')
    if data.gaps:
        parts.append("<ul>")
        for gap in data.gaps:
            parts.append(f"<li>{_esc(gap)}</li>")
        parts.append("</ul>")
    else:
        parts.append("<p>Nothing outstanding: every item was read and every endpoint probed.</p>")
    parts.append("</div>")

    parts += ["</main>", "</body>", "</html>"]
    return "\n".join(parts) + "\n"

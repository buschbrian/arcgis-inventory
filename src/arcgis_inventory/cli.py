"""Command-line interface.

One crawl, one SQLite database, several views over it. The subcommands below
are views on shared data, not separate programs --- splitting them would mean
crawling an org four times to answer four questions about the same items.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .config import RuntimeConfig, load_config
from .crawl import CrawlResult, PortalClient, crawl_inventory
from .db import SCHEMA_VERSION, open_database
from .errors import ArcgisInventoryError, ConfigError
from .reprocess import reprocess_inventory
from .transport import FixtureTransport, HttpTransport, Transport

console = Console()
err_console = Console(stderr=True)

# The fixture org's portal URL. Only ever used with --fixture, where every
# hostname is IANA-reserved and nothing resolves.
_FIXTURE_PORTAL_URL = "https://northgate.example.gov/portal"

app = typer.Typer(
    name="arcgis-inventory",
    help=(
        "Inventory, dependency-map, and audit an ArcGIS portal ahead of the Web AppBuilder "
        "retirement.\n\n"
        "This tool does NOT convert Web AppBuilder apps to Experience Builder. No such "
        "converter exists, from Esri or anyone else."
    ),
    no_args_is_help=True,
    add_completion=False,
)

DbOption = Annotated[
    Path | None,
    typer.Option(
        "--db", help="SQLite database path. Defaults to $ARCGIS_DB or output/inventory.sqlite."
    ),
]

# Every subcommand below that is not yet implemented raises this rather than
# printing a friendly no-op. A crawl command that silently does nothing is how
# you end up believing an org is clean.
_ROADMAP = (
    "Not implemented yet --- see the build order in docs/roadmap.md. "
    "Implemented so far: init-db, doctor, inventory, reprocess."
)


def _fail(message: str) -> None:
    err_console.print(f"[bold red]error[/] {message}")
    raise typer.Exit(code=2)


def _resolve_db(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    try:
        return load_config().database
    except ConfigError:
        # A local database path should not require a portal URL.
        return Path("output/inventory.sqlite")


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"arcgis-inventory {__version__} (schema v{SCHEMA_VERSION})")
        raise typer.Exit()


@app.callback()
def _root(
    _version: Annotated[
        bool,
        typer.Option("--version", callback=_version_callback, is_eager=True, help="Show version."),
    ] = False,
) -> None:
    return None


# ---------------------------------------------------------------------------
# Implemented
# ---------------------------------------------------------------------------


@app.command("init-db")
def init_db(db: DbOption = None) -> None:
    """Create an empty inventory database, or verify an existing one."""
    target = _resolve_db(db)
    existed = target.exists()
    try:
        conn = open_database(target)
    except ArcgisInventoryError as exc:
        _fail(str(exc))
        return
    tables = conn.execute(
        "SELECT COUNT(*) AS n FROM sqlite_master WHERE type IN ('table','view')"
    ).fetchone()["n"]
    conn.close()
    verb = "verified" if existed else "created"
    console.print(f"{verb} [bold]{target}[/] --- schema v{SCHEMA_VERSION}, {tables} tables/views")


@app.command()
def doctor() -> None:
    """Check configuration and report what a crawl would do, without crawling."""
    table = Table(show_header=False, box=None, pad_edge=False)
    try:
        cfg: RuntimeConfig = load_config()
    except ConfigError as exc:
        err_console.print(f"[bold red]config[/] {exc}")
        raise typer.Exit(code=2) from exc

    auth = (
        "anonymous"
        if cfg.portal.is_anonymous
        else ("token" if cfg.portal.token else f"user {cfg.portal.username}")
    )
    table.add_row("portal", cfg.portal.url)
    table.add_row("auth", auth)
    table.add_row("verify ssl", str(cfg.portal.verify_ssl))
    table.add_row("ca bundle", cfg.portal.ca_bundle or "(system)")
    table.add_row("database", str(cfg.database))
    table.add_row("output dir", str(cfg.output_dir))
    table.add_row("page size", str(cfg.page_size))
    table.add_row("max rps", str(cfg.max_rps))
    table.add_row("probe services", str(cfg.probe_services))
    console.print(table)

    if cfg.portal.is_anonymous:
        err_console.print(
            "[yellow]warning[/] no credentials configured. An anonymous crawl sees only public "
            "items and will silently under-report."
        )


@app.command()
def inventory(
    db: DbOption = None,
    query: Annotated[
        str, typer.Option("--query", "-q", help="ArcGIS search query. Empty crawls everything.")
    ] = "",
    fixture: Annotated[
        Path | None,
        typer.Option(
            "--fixture",
            help="Crawl a local fixture tree instead of a portal. No network, no credentials.",
        ),
    ] = None,
    page_size: Annotated[
        int | None, typer.Option("--page-size", help="Items per search request.")
    ] = None,
    skip_data: Annotated[
        bool,
        typer.Option(
            "--skip-data", help="Do not fetch item data documents. Faster, far less useful."
        ),
    ] = False,
) -> None:
    """Crawl the portal: paginated item search, typed classification."""
    target = _resolve_db(db)

    if fixture is not None:
        transport: Transport = FixtureTransport(fixture)
        portal_url = _FIXTURE_PORTAL_URL
        size = page_size or 10
    else:
        try:
            cfg = load_config()
        except ConfigError as exc:
            _fail(str(exc))
            return
        if cfg.portal.is_anonymous:
            err_console.print(
                "[yellow]warning[/] crawling anonymously; only public items will be visible."
            )
        transport = HttpTransport(
            timeout=cfg.timeout_seconds,
            max_retries=cfg.max_retries,
            max_rps=cfg.max_rps,
            verify=cfg.portal.ca_bundle or cfg.portal.verify_ssl,
            token=cfg.portal.token,
        )
        portal_url = cfg.portal.url
        size = page_size or cfg.page_size

    client = PortalClient(transport, portal_url, page_size=size)
    conn = open_database(target)
    try:
        result = crawl_inventory(conn, client, query=query, fetch_data=not skip_data)
    finally:
        conn.close()
        transport.close()

    _report_crawl(result, target)


def _report_crawl(result: CrawlResult, database: Path) -> None:
    table = Table("platform", "items", box=None, pad_edge=False)
    for platform, count in sorted(result.platforms.items(), key=lambda kv: (-kv[1], kv[0])):
        table.add_row(platform, str(count))
    if result.platforms:
        console.print(table)

    style = {"complete": "green", "partial": "yellow", "failed": "red"}[result.status]
    console.print(
        f"run {result.run_id}: [{style}]{result.status}[/] --- {result.item_count} items, "
        f"{result.error_count} errors --> {database}"
    )
    if result.status == "partial":
        err_console.print(
            "[yellow]partial[/] some items could not be fully read. They are in `crawl_error` "
            "with the reason --- a 403 there usually means the crawling account cannot see "
            "something, which is often what the public cannot see either."
        )


@app.command()
def reprocess(
    db: DbOption = None,
    show: Annotated[
        int, typer.Option("--show", help="How many changed items to list. 0 lists all.")
    ] = 20,
) -> None:
    """Re-derive classifications from stored raw JSON. No network.

    This is the development loop --- crawling a 5,000-item org takes a long time
    and hammers someone's portal; re-running the rules over stored JSON takes
    seconds. It reports what a rule change actually moved.
    """
    target = _resolve_db(db)
    if not target.exists():
        _fail(f"{target} does not exist. Run `inventory` first --- there is nothing to reprocess.")
        return

    conn = open_database(target)
    try:
        result = reprocess_inventory(conn)
    except ValueError as exc:
        _fail(str(exc))
        return
    finally:
        conn.close()

    console.print(
        f"run {result.run_id}: reprocessed {result.resource_count} items --- "
        f"[bold]{result.change_count}[/] classifications changed"
    )
    if result.skipped:
        err_console.print(
            f"[yellow]note[/] {result.skipped} items had no stored raw document and were "
            "skipped rather than reclassified from nothing."
        )

    if result.changed:
        table = Table("item", "before", "after", box=None, pad_edge=False)
        listed = result.changed if show == 0 else result.changed[:show]
        for change in listed:
            table.add_row(change.title or change.item_id, change.before, change.after)
        console.print(table)
        if len(result.changed) > len(listed):
            console.print(f"... and {len(result.changed) - len(listed)} more (--show 0 for all)")


# ---------------------------------------------------------------------------
# Roadmap --- the subcommands from the design, declared so the shape of the tool
# is visible and the help text is honest about what is missing.
# ---------------------------------------------------------------------------


@app.command()
def dependencies(db: DbOption = None) -> None:
    """Resolve app -> web map -> layers/geocoders/GP/print into a graph."""
    raise NotImplementedError(_ROADMAP)


@app.command()
def scan(db: DbOption = None) -> None:
    """Apply YAML deprecated-tech rules (JS 3.x, dojo/dijit, HTTP, Map Viewer Classic)."""
    raise NotImplementedError(_ROADMAP)


@app.command("audit-sharing")
def audit_sharing(db: DbOption = None) -> None:
    """Find public apps depending on non-public layers, orphaned owners, dev-host refs."""
    raise NotImplementedError(_ROADMAP)


@app.command()
def recommend(db: DbOption = None) -> None:
    """Classify each app Retire / Instant App / Experience Builder / Custom, with reasoning."""
    raise NotImplementedError(_ROADMAP)


@app.command()
def report(db: DbOption = None) -> None:
    """Roll the database up into Markdown and HTML."""
    raise NotImplementedError(_ROADMAP)


@app.command("wab-export")
def wab_export(db: DbOption = None) -> None:
    """Dump Web AppBuilder widget/theme/search config to JSON as migration documentation."""
    raise NotImplementedError(_ROADMAP)


def main() -> None:
    app()


if __name__ == "__main__":  # pragma: no cover
    main()

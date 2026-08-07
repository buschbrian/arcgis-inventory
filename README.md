# arcgis-inventory

**Web AppBuilder apps in ArcGIS Online stop being editable in Q4 2026 and stop
working in Q2 2027.** Most organizations cannot currently answer the first
question that deadline raises: *what do we actually have, and what depends on
what?*

This is a Python CLI that answers it. One crawl of a portal, one SQLite
database, several views over it: an inventory of every item, a dependency graph
from apps down to individual layers, a scan for deprecated technology, a sharing
audit, and a rule-based recommendation for where each app should land.

> **What this tool does not do:** it does not convert Web AppBuilder apps to
> Experience Builder. No such converter exists — not from Esri, not from anyone
> else, and none is planned. Esri's own guidance is to reconfigure or rebuild.
> This tool tells you *what* to rebuild, in what order, and what breaks if you
> don't. The rebuilding is still yours.

Status: **early development.** The crawl and classification work; the analysis
subcommands do not yet. Anything unimplemented raises rather than quietly
succeeding — see [docs/roadmap.md](docs/roadmap.md).

## Why it is one program and not six

The inventory crawler, the dependency graph, the deprecated-tech scanner, the
sharing audit, the usage analysis, and the recommendation engine all need the
same crawl of the same portal. Shipping them separately means crawling an
organization's portal four times to answer four questions about the same items.

```
arcgis-inventory inventory      # crawl + classify                      [works]
arcgis-inventory dependencies   # app -> web map -> layers/geocoders/GP/print
arcgis-inventory scan           # deprecated tech (JS 3.x, dojo, http://)
arcgis-inventory audit-sharing  # public apps on private layers, orphans, dev refs
arcgis-inventory recommend      # retire / instant app / experience builder / custom
arcgis-inventory report         # Markdown + HTML rollup
arcgis-inventory reprocess      # re-derive from stored JSON, no network    [works]
```

Every crawl keeps the portal's raw responses, so `reprocess` re-runs the rules
over stored JSON in seconds instead of re-crawling:

```bash
arcgis-inventory reprocess --db /tmp/demo.sqlite
```

```
run 2: reprocessed 30 items --- 0 classifications changed
```

Zero is the expected answer immediately after a crawl, and it is worth having a
command that proves it: if reprocessing ever *did* change something, either the
crawl is not storing what it classified from, or classification depends on
something the database does not hold.

After a rule change it lists what moved, item by item, before and after — which
is the only honest way to review one.

### The query worth the price of admission

`audit-sharing` answers questions nobody can currently answer and a director
immediately understands:

- Which **public** apps depend on layers that are *not* public — i.e. which
  public-facing apps are quietly broken for the public right now?
- Which apps are owned by accounts that no longer exist?
- Which production apps reference **dev or staging** services?
- Which services would break, and in which apps, if a given layer changed?

That last one is the dependency graph read backwards. It is what turns this from
a migration tool into something you keep running after the migration is over.

## Install

```bash
pip install arcgis-inventory
```

Python 3.11+. Pure pip, no conda — this has to install on a locked-down
government workstation. The `arcgis` Python package is deliberately *not* a
dependency; the tool talks raw ArcGIS REST over `httpx`.

## Use

```bash
cp env.example .env    # then fill it in; .env is gitignored
arcgis-inventory doctor
arcgis-inventory inventory
```

Configuration is environment variables and user-supplied files only. There is no
portal URL, item id, service URL, domain, or layer name anywhere in this
repository, and there never will be.

### Try it without a portal

The repository ships a synthetic organization, so you can see what a crawl
produces before pointing this at anything real:

```bash
arcgis-inventory inventory --fixture tests/fixtures/northgate --db /tmp/demo.sqlite
```

```
platform            items
web_map             13
web_appbuilder      9
experience_builder  2
custom_js_app       1
dashboard           1
instant_app         1
storymap            1
web_scene           1
widget_package      1
run 1: partial --- 30 items, 2 errors --> /tmp/demo.sqlite
```

`partial` is correct there, not a bug: two items in the fixture are deliberately
unreadable — one with malformed data, one returning a permission error. They are
recorded in `crawl_error` with the reason, and the other 28 still land. A crawl
that dies on item 400 of 5,000 is worse than useless.

Every classification records *which signal fired*, because the first question
anyone asks about "you have 9 Web AppBuilder apps" is how the tool knows:

```sql
SELECT title, platform, platform_confidence, platform_evidence FROM resource;
```

## Handle the output carefully

A completed crawl is a **complete map of your organization's GIS attack
surface**, including which services are public that shouldn't be. `output/` and
root-level `*.csv` / `*.json` are gitignored from the first commit, and every
screenshot, example, and demo report in this repository is generated from a
synthetic fixture organization rather than a live portal.

Treat a real `inventory.sqlite` the way you would treat a vulnerability scan
result, because that is what it is.

## Development

```bash
python -m venv .venv && .venv/Scripts/activate   # Windows
pip install -e ".[dev]"
pytest
ruff check .
```

The test suite runs against a synthetic fixture organization committed to the
repository — no network, no credentials — so CI passes for anyone and
contributors need access to nothing. See
[tests/fixtures/README.md](tests/fixtures/README.md).

## Documentation

- [docs/data-model.md](docs/data-model.md) — the schema and the three decisions
  that shape it
- [docs/roadmap.md](docs/roadmap.md) — what is built, what is next
- [tests/fixtures/README.md](tests/fixtures/README.md) — the fixture org

## License

MIT.

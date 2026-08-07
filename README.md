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
arcgis-inventory dependencies   # app -> web map -> layers/GP/print     [works]
arcgis-inventory scan           # deprecated tech (WAB, JS 3.x, dojo)   [works]
arcgis-inventory audit-sharing  # public apps on private layers         [works]
arcgis-inventory recommend      # retire / instant app / EXB / custom    [works]
arcgis-inventory report         # Markdown + HTML rollup               [works]
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

Then build the graph:

```bash
arcgis-inventory dependencies --db /tmp/demo.sqlite
```

```
relation           edges
operational_layer  43
data_source        12
basemap            2
geocoder           2
arcade_source      1
gp_service         1
print_service      1
widget_config      1
run 2: 63 dependencies across 21 service endpoints
```

Every edge records the **JSON pointer it came from**, which is what makes a
dependency arguable rather than asserted — and what turns the graph into impact
analysis when you read it backwards:

```
what breaks if Public/Parcels/FeatureServer changes?
  Parcels & Zoning       operational_layer  /operationalLayers/0/url
  Parcels & Zoning       operational_layer  /operationalLayers/1/url
  Development Projects   operational_layer  /operationalLayers/2/url
  Development Projects   operational_layer  /operationalLayers/3/url
```

Dependencies the tool goes looking for that most inventories miss: layers nested
inside group layers to any depth, services referenced by URL with no portal item
behind them, search-widget configurations pointing somewhere the app's web map
never mentions, and **Arcade expressions** reaching into a layer the map does not
list at all.

Then audit it:

```bash
arcgis-inventory audit-sharing --db /tmp/demo.sqlite --probe --fixture tests/fixtures/northgate
```

```
probing service endpoints anonymously (fixture)...
  22 endpoints: 15 public, 6 restricted, 1 unreachable
rule                     findings
public-app-private-dep   3
dev-host-reference       1
http-service-dependency  1
orphaned-owner           1
unreachable-dependency   1
run 3: 7 findings (7 new, 0 resolved since the last audit)
```

Every finding names the chain and what to do about it:

```
[critical] Publicly shared item depends on non-public DevelopmentProjects (FeatureServer)
  Development Projects Map is shared publicly but reaches DevelopmentProjects
  (FeatureServer) (access: org) via Development Projects Map -> Development
  Projects -> DevelopmentProjects (FeatureServer). Anyone outside the
  organization sees it broken.
  -> Either share the dependency publicly or stop sharing the app publicly.
     Confirm which was intended before changing either.
```

Note the middle hop. An app almost never touches a layer directly — it goes app
→ web map → layer — so the graph is walked **transitively**. A rule that only
looked at direct references would miss essentially every real instance.

### `--probe` is opt-in, and it has to be

Whether the *public* can reach a service cannot be determined by a crawl
authenticated as someone who can see everything. It takes one **unauthenticated**
request per endpoint — outbound traffic, often to hosts that don't belong to you.
So it happens only when you ask.

Without `--probe`, service sharing is unknown, and the exposure rule **stays
silent rather than guessing**. Unknown is not private: reporting it as private is
the false positive that trains people to ignore the rule.

Findings carry stable fingerprints, so a re-run does not resurrect something
you already dismissed — that failure mode is why people stop running scanners.
A finding that stops firing gets `resolved_run` set rather than being deleted;
that's *observed* resolved, which is a different claim from someone marking it
fixed, and disagreement between the two is interesting.

The dev/staging hostname patterns live in
[`rules/sharing.yaml`](src/arcgis_inventory/rules/sharing.yaml) and are replaced
wholesale by pointing `--rules` at your own copy. No organization's naming
convention is baked into this repository.

### Scan for deprecated technology

```bash
arcgis-inventory scan --db /tmp/demo.sqlite
```

```
severity  rule                     items
critical  arcgis-js-3              1
critical  web-appbuilder-retiring  9
high      dojo-dijit               1
high      wab-custom-widget        1
low       unused-and-stale         1
run 3: 13 findings across 30 items (13 new, 0 resolved since the last scan)
```

```
[high] Web AppBuilder app with a custom widget
  Snow Plow Route Status: Custom widget code does not carry across to
  Experience Builder. This app is a rewrite, not a reconfiguration, and the
  widget's behaviour has to be rebuilt or dropped deliberately.
  -> Decide early whether the custom behaviour is still required. It is
     frequently cheaper to drop it than to reimplement it.
```

Rules are **data**, in [`rules/scan.yaml`](src/arcgis_inventory/rules/scan.yaml),
with a small matcher vocabulary (`platform`, `type_keyword`, `data_matches`,
`data_has_key`, `custom_wab_widget`, `views_below`, …) and `all` / `any` / `none`
combinators. A scanner nobody can extend gets replaced by a spreadsheet within a
month, so `--rules` swaps the file wholesale.

Two behaviours worth knowing:

- A **typo in a matcher name is an error**, not a silent pass. A rule that
  quietly matches nothing is worse than no scanner.
- `views_below` **never matches an unknown view count.** NULL means "we don't
  know", and retiring an app on the strength of a missing number is how you
  delete something people depend on.

### Decide where each app should go

```bash
arcgis-inventory recommend --db /tmp/demo.sqlite --show 2
```

```
target              apps
keep                5
instant_app         4
custom              2
unknown             2
experience_builder  1
retire              1
run 3: 15 applications

Public Works Asset Viewer --- experience_builder (likely, complexity 69)
  Rebuild in Experience Builder --- it has more moving parts than a
  configurable template covers --- multiple pages, many widgets, or
  geoprocessing. Experience Builder is the like-for-like replacement.
  Signals: 1 web map, 8 layers, 7 widgets, 3 pages, printing, a geocoder, a
  web map shared with other apps, 3,120 views.

Snow Plow Route Status --- custom (certain, complexity 64)
  Rebuild as custom development --- it uses custom widget code, which no
  configurable app can reproduce. Decide early whether that behaviour is
  still needed --- dropping it is often cheaper than rebuilding it. Signals:
  1 web map, 4 layers, 4 widgets (1 custom), geoprocessing, 91,455 views.
```

**The reasoning is the output; the label is a summary of it.** A bare verdict of
"Experience Builder" gets ignored — "7 widgets, 3 pages, printing, a geocoder,
3,120 views" gets acted on, and can be argued with. Every recommendation is
built from the same numbers the rule matched on, so a reader can check it.

Three things the rules are opinionated about:

- **Instant Apps are the default for simple apps**, not Experience Builder.
  Most Web AppBuilder apps in the wild are one map, a search box, and a legend.
  This matches Esri's own guidance.
- **Retirement comes before rebuilding.** The cheapest migration is the one you
  don't do, so orphaned and unused apps are flagged to retire first.
- **An app whose config couldn't be read gets `unknown`, not a guess.** A
  confident "Instant App" for an app nobody could inspect is worse than silence.

`complexity` is 0–100 and is not an estimate in hours — it's a way to sort 200
apps so the cheap ones get done first. Rules and weights live in
[`rules/recommend.yaml`](src/arcgis_inventory/rules/recommend.yaml); first match
wins, so ordering is part of the meaning.

Set `override_target` on any row to record a human decision. Re-running the
engine updates the generated verdict beside it and never touches the override.

### Roll it up for people who won't run the tool

```bash
arcgis-inventory report --db /tmp/demo.sqlite --out /tmp/report
```

Writes `inventory-report.md` and `inventory-report.html` — self-contained, no
external stylesheet or script, so it survives being emailed around. The HTML is
built to the accessibility standard this project argues for: headings in order,
table captions and scoped headers, nothing meaningful carried by colour alone.

The report leads with the deadline, then the public-exposure findings, then the
migration plan ordered hardest-first. It ends with the section that matters
most:

```
## What this report does not know

- 2 item(s) could not be fully read during the crawl. Their configuration was
  not analysed, so any conclusion about them is weaker than it looks.
- 1 service endpoint(s) did not answer when probed, so their sharing could not
  be established either. Whatever depends on them is already broken.
- 2 application(s) have no recommendation, because their configuration could
  not be read. They still need a decision; the tool declines to guess.
- 1 item(s) report no view count. Unknown usage is not zero usage, and nothing
  here treats it as such.
```

A rollup like this gets forwarded and read as a complete picture, so it has to
say what it skipped. If sharing was never probed, it says so in those words —
**absence of exposure findings is not evidence of no exposure.**

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

# Roadmap

Order is chosen so that each step is testable against the synthetic fixture org
before anything is pointed at a real portal.

## Built

- **Schema** — `src/arcgis_inventory/db/schema.sql`, nine tables and eight
  views. See [data-model.md](data-model.md).
- **URL normalization** — `urls.py`, with property-based idempotency tests. Get
  this wrong and edges silently fail to dedupe and the graph is wrong in a way
  that is hard to see.
- **Finding fingerprints** — `fingerprint.py`. Stable identity across runs is
  what lets authored triage state survive a re-crawl.
- **Transport** — `transport.py`. `HttpTransport` (httpx, throttled, retrying)
  and `FixtureTransport` (disk, loud on an unmapped URL) behind one protocol, so
  tests exercise the real pagination/classification/extraction code.
- **Config** — `config.py`, environment only, refuses to guess.
- **CLI shape** — `cli.py`. `init-db`, `doctor`, `inventory`, `reprocess`,
  `dependencies`, `audit-sharing`, `scan`, `recommend`, and `report` work;
  `wab-export` raises rather than quietly succeeding.
- **Fixture org** — `tests/fixtures/northgate/`, 30 items and 22 services
  generated from `spec.yaml`, plus a second crawl as an overlay. Self-verifying:
  every `source_path` it claims is resolved against the JSON it points at.
- **`inventory`** — `crawl.py` + `classify.py` + `db/store.py`. Paginated
  search, item-data fetch, raw retention, classification with
  `platform_confidence` and `platform_evidence`. Golden-tested against
  `expected/inventory.json`. Runs against a local fixture with `--fixture`, so
  the tool is demonstrable with no portal and no credentials.

- **`reprocess`** — `reprocess.py`. Re-derives classification from stored raw
  documents with no network, and reports what a rule change moved, item by item.
  Guaranteed structurally: the module cannot import a transport, and a test
  asserts it. Does not advance `last_seen_run` — a reprocess observes nothing.

- **`dependencies`** — `extract.py` (pure functions over JSON) + `dependencies.py`
  (resolution onto rows). App → web map → layers / geocoders / GP / print, with
  a JSON pointer on every edge. Golden-tested against `expected/edges.json`: 64
  edges, exact. Endpoint *sharing* is deliberately left NULL — see below.

- **`audit-sharing`** — `audit.py` + `rules/sharing.yaml`. Five rules over the
  graph: public exposure (walked transitively via NetworkX), orphaned owners,
  non-production host references, plaintext HTTP dependencies, and unreachable
  services. Findings carry stable fingerprints, so authored triage survives a
  re-run and a rule that stops firing sets `resolved_run` rather than deleting
  the row. `--probe` is opt-in; without it the exposure rule stays silent.

- **`scan`** — `scan.py` + `rules/scan.yaml`. Deprecated-tech detection driven
  by a small matcher vocabulary with `all`/`any`/`none` combinators. Rules are
  data and are replaced wholesale via `--rules`. An unknown matcher name raises
  rather than silently passing.

- **`recommend`** — `recommend.py` + `rules/recommend.yaml`. Ordered rules over
  graph-derived signals; first match wins. Generates the *reasoning* from the
  same numbers the rule matched on, plus a 0–100 complexity score for sorting.
  Biased toward Instant Apps; refuses to guess about items whose config could
  not be read; never overwrites a human `override_target`.

- **`report`** — `report.py`. One gathered structure, two renderers, so Markdown
  and HTML cannot drift. Self-contained accessible HTML (headings in order,
  captioned and scoped tables, escaped titles). Always ends with a "what this
  report does not know" section listing unread items, unprobed endpoints,
  missing graph, and missing recommendations.

## Next

1. **`wab-export`** — dump WAB widget/theme/search config as migration
   documentation.

## Known gaps

- **`v_public_app_private_dep` reports *direct* edges only.** The view is still
  useful for a quick look in a SQLite browser, but `audit-sharing` walks the
  graph transitively and its findings are authoritative. Do not read the view
  and conclude the org is clean.
- **Probe results are a point in time** and are stored on the endpoint rather
  than per run, so there is no history of when a service changed sharing.
- **Mermaid and Graphviz export** of the graph is not built yet.
- **`recommend` needs `dependencies` to have run.** Without the graph every app
  reads as a single-map app with no layers, which skews every verdict the same
  way. The CLI warns; it does not refuse.
- **`scan` reads item documents only.** The two fixture items whose data could
  not be read therefore match almost nothing — that is missing knowledge, not a
  clean bill, and `recommend` has to treat low-signal items as unknown rather
  than as simple.

## Explicitly out of scope

- **Converting Web AppBuilder apps to Experience Builder.** Not possible
  automatically at useful fidelity; Esri does not do it either.
- **Accessibility findings.** Separate concern, separate repo
  (`gis-a11y-harness`), separate store. Tempting to unify; resist until both
  exist and the shape is known.
- **Writing to a portal.** This tool is read-only, permanently. Remediation is a
  separate CLI with dry-run defaults, rollback files, and audit logs.
- **Side-by-side app comparison, cutover management.** Two browser windows and a
  checklist, respectively. Not software.

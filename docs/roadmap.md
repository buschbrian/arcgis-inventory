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
- **CLI shape** — `cli.py`. `init-db`, `doctor`, `inventory`, and `reprocess`
  work; every other subcommand raises rather than quietly succeeding.
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
  a JSON pointer on every edge. Golden-tested against `expected/edges.json`: 63
  edges, exact. Endpoint *sharing* is deliberately left NULL — see below.

## Next

1. **`audit-sharing`** — the views are already in the schema and the graph is
   now built; this wires them to findings with stable fingerprints. Highest-value
   single feature in the tool.

   **It has to probe.** `dependencies` leaves `resource.access` NULL for bare
   service endpoints, because a crawl authenticated as someone who can see
   everything cannot tell whether the *public* can reach a service. Determining
   that takes an unauthenticated request per endpoint. Until it exists,
   `v_public_app_private_dep` correctly returns nothing rather than a false
   clean bill of health.

2. **`scan`** — YAML rule format, deprecated-tech detection. Note that the two
   items whose data could not be read have no dependencies recorded; the rules
   must treat that as missing knowledge, not as a simple app.
3. **`recommend`** — rule-based target with generated reasoning. Bias toward
   Instant Apps for simple single-map apps rather than defaulting to Experience
   Builder.
4. **`report`** — Markdown + HTML, generated from the fixture for the README.
5. **`wab-export`** — dump WAB widget/theme/search config as migration
   documentation.

## Known gaps

- **Transitive reachability.** `v_public_app_private_dep` reports *direct*
  edges, so a public app reaching a private layer through a web map is caught at
  the web map, not at the app. Walking the graph transitively is
  `audit-sharing`'s job; NetworkX is already a dependency and still unused.
- **Mermaid and Graphviz export** of the graph is not built yet.

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

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
- **CLI shape** — `cli.py`. `init-db` and `doctor` work; every other subcommand
  raises rather than quietly succeeding.

## Next

1. **Build the fixture org** — `tests/fixtures/northgate/spec.yaml` plus
   `build.py`, expanded into the portal-shaped JSON tree, with golden output
   files. Everything below is blocked on this, and it is the piece that keeps
   any real organization's structure out of the repo.
2. **`inventory`** — paginated search, item + item-data fetch, raw retention,
   classification with `platform_confidence` / `platform_evidence`. Golden-file
   tested against the fixture.
3. **`reprocess`** — re-derive everything from `raw_json` with no network. Build
   this early: it is the development loop, and it runs in CI.
4. **`dependencies`** — recursive app → web map → layers / geocoders / GP /
   print, into `edge` with `source_path`. NetworkX load, Mermaid export.
5. **`audit-sharing`** — the views are already in the schema; this wires them to
   findings with stable fingerprints. Highest-value single feature in the tool.
6. **`scan`** — YAML rule format, deprecated-tech detection.
7. **`recommend`** — rule-based target with generated reasoning. Bias toward
   Instant Apps for simple single-map apps rather than defaulting to Experience
   Builder.
8. **`report`** — Markdown + HTML, generated from the fixture for the README.
9. **`wab-export`** — dump WAB widget/theme/search config as migration
   documentation.

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

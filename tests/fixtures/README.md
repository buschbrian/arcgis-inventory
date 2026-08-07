# Synthetic Fixture Org

Status: **specified, not yet built.** `northgate/` does not exist yet — this
document is the build order for it. `FixtureTransport` in
`src/arcgis_inventory/transport.py` is the consumer.

A fake ArcGIS organization, committed to the repo, that the crawler runs
against with no network and no credentials. It exists so CI passes for anyone,
contributors need access to nothing, and — the reason that matters most — **no
real organization's structure ever leaks into the repo through tests, sample
output, or README screenshots.**

---

## Design rules

### It must be obviously fake

Fictional org `Northgate` at `https://northgate.example.gov/portal`, plus an
ArcGIS Online-shaped org at `https://northgate.maps.arcgis.example`. All
hostnames use IANA-reserved `.example` / `example.com` / `example.gov` names —
guaranteed non-routable, so a bug that accidentally makes a real HTTP request
fails loudly instead of hitting somebody's server.

Item IDs are deterministic 32-char hex with a readable prefix
(`a0000000000000000000000000000001`), not realistic-looking random hex. Anyone
reading a test failure should immediately know they're looking at a fixture.

Layer names, field names, and folder names are generic municipal vocabulary
(`Parcels`, `Zoning`, `Storm Drain`, `Address Points`) — the vocabulary is
common to every jurisdiction in the country and encodes nothing about any one
of them.

### The transport is swappable, so tests exercise the real code

The HTTP layer sits behind a `Transport` protocol with two implementations:
`HttpTransport` and `FixtureTransport`. The fixture transport resolves a URL to
a file on disk and returns the parsed JSON. Everything above it — pagination,
classification, edge extraction, rules — is the same code in tests and in
production. Tests that mock at a higher level than this stop testing the thing
that actually breaks.

Unmapped URL in the fixture transport = loud failure, never an empty result.
A silently-empty response makes a broken crawler look like a clean org.

### Committed generated, with the generator alongside

`fixtures/spec.yaml` is a compact human-editable description of the org.
`fixtures/build.py` expands it into the portal-shaped JSON tree.
**Both the spec and the generated JSON are committed.** Tests read the JSON —
so diffs are visible in review and a fixture change is legible — and the
generator means adding a twenty-fifth item doesn't mean hand-writing four files
and a paginated search response. CI re-runs the generator and fails if the
committed output drifts.

---

## Layout

```
tests/fixtures/northgate/
  spec.yaml                    # source of truth, human-edited
  build.py                     # spec.yaml -> everything below
  portal/
    self.json                  # /sharing/rest/portals/self
    users.json                 # community/users — includes a deleted user
    groups.json
  search/
    page-1.json                # num=100&start=1 ... paginated, 3 pages
    page-2.json
    page-3.json
  items/
    <itemId>.json              # item description
    <itemId>.data.json         # item data, where the item has data
  services/
    <host>/<path>.json         # service metadata
  expected/
    inventory.json             # golden output: classification per item
    edges.json                 # golden output: full dependency edge list
    findings.json              # golden output: rule_id + fingerprint per finding
    recommendations.json       # golden output: target + confidence per app
```

Golden-output files are the regression suite. When a classification rule
changes, the diff in `expected/inventory.json` *is* the review.

---

## What the org has to contain

Roughly 30 items. Every one exists to exercise a specific failure mode — a
fixture org of thirty plausible-but-uninteresting apps tests nothing. Each
entry below is a test case first and a fake app second.

### Applications

| # | Item | Exercises |
|---|---|---|
| 1 | WAB app, 1 map + search + legend, healthy usage | Classification: **Instant App** recommendation |
| 2 | WAB app, 6 widgets, multi-page | Classification: **Experience Builder** recommendation |
| 3 | WAB app with a **custom widget** package | Classification: **Custom development**; widget package detection |
| 4 | WAB app, owner is a **deleted user**, 0 views | **Retire** recommendation; `owner_exists=0`; `v_orphaned` |
| 5 | **Public** WAB app → web map → **org-only** feature layer | ⭐ The `audit-sharing` headline case |
| 6 | WAB app referencing `gis-dev.northgate.example.gov` | Dev/staging host detection |
| 7 | WAB app depending on an **`http://`** map service | Non-HTTPS finding |
| 8 | Custom HTML app loading `js.arcgis.com/3.42/` | JS 3.x scanner rule; `custom_js_app` classification |
| 9 | Experience Builder app, well-formed | Negative case — must produce **no** findings |
| 10 | Experience Builder app, config a11y problems | Feeds BU2's static auditor later; must classify cleanly here |
| 11 | Dashboard | Classified, but **not** recommended for migration |
| 12 | Instant App | Same |
| 13 | StoryMap | Same |
| 14 | WAB app whose data JSON is **malformed** | `crawl_error` path; crawl continues, run status `partial` |
| 15 | WAB app whose item data returns **403** | Permission failure recorded as a finding, not a crash |

### Web maps

| # | Item | Exercises |
|---|---|---|
| 16 | Web map used by **apps 1, 2, and 5** | Fan-in; `v_shared_maps`; "migrate once, fix many" |
| 17 | Web map with a **broken layer** (service 404) | Reachability finding; `reachable=0` |
| 18 | Web map referencing a service **by URL only**, no item | ⭐ Forces an `endpoint` node with no `item_id` |
| 19 | Web map with a **deeply nested group layer** (4 levels) | Recursion depth in edge extraction |
| 20 | Web map with a basemap from **outside the org** | External domain detection |
| 21 | Web map with **Arcade** expressions referencing another layer | `arcade_source` edges |
| 22 | Web map with the **same layer twice** at different indexes | URL normalization: one endpoint node, two edges |
| 23 | Web scene | Non-map spatial item classification |

### Services and endpoints

| # | Resource | Exercises |
|---|---|---|
| 24 | Hosted feature service, public | Baseline |
| 25 | Hosted feature service, **org-only** | Target of case 5 |
| 26 | Legacy `MapServer` over `http://` | Case 7's target |
| 27 | Geocode service, external host | `geocoder` relation |
| 28 | GP service | `gp_service` relation |
| 29 | Print service | `print_service` relation |

### Deliberately nasty metadata

Spread across the items above, not as extra items:

- A title with a **comma, a double quote, and a newline** — CSV/Excel export
  escaping. This breaks more tools than any architectural problem.
- A title with **non-ASCII characters and an emoji** — encoding on Windows,
  where the default console codepage will find you.
- **Two items with identical titles** in different folders — duplicate
  detection must key on item id, not title.
- An item with `numViews = 0` and one with `numViews = null` — null is not zero,
  and "unknown usage" is a different recommendation than "unused".
- An item **modified in the future** (clock skew is real) and one with a
  **null `modified`**.
- An item with **200 tags** and one with none.
- A folder name containing a **forward slash** — path construction on export.
- A **very long** service URL near typical path limits.

### Two runs, not one

The fixture ships a **second search response set** (`search/run2/`) representing
the same org a month later, with:

- one WAB app **deleted** → `last_seen_run` stops advancing, appears in "what
  disappeared"
- one new EXB app **added** → replacement candidate
- view counts **increased** on three items → usage slope
- one finding **resolved** → `resolved_run` set
- one item **re-shared** from org to public → new finding on an existing
  resource, and the fingerprint must be *new* while the resource stays the same

This is what makes the incremental-crawl and fingerprint-stability behavior
testable at all. Without a second run, the diff machinery is untested and the
authored-data-survives-recrawl guarantee is a claim rather than a test.

---

## The tests this makes possible

1. **Classification** — every item lands in the right `platform` with the right
   confidence. Golden file.
2. **Edge extraction** — the full edge list matches, including `source_path`.
   Golden file.
3. **URL normalization** — property-based: normalizing twice equals normalizing
   once; the same service reached over http and https, with and without a
   trailing slash, at two layer indexes, produces exactly one endpoint node.
4. **Fingerprint stability** — ⭐ run the whole pipeline twice over run-1 data;
   every fingerprint is identical. Then mark a finding `wontfix`, run over run-2
   data, and assert the status survived. **This is the test that protects the
   most important property in the schema.**
5. **Authored-data survival** — populate `migration` and overrides, wipe all
   derived tables, reprocess from `raw_json`, assert the authored rows are
   untouched and still correctly joined.
6. **Error handling** — malformed and 403 items produce `crawl_error` rows, the
   run completes with status `partial`, and the other 28 items still land.
7. **Export escaping** — the nasty-title item round-trips through CSV and JSON
   export without corruption or row-splitting.
8. **Sharing audit** — `v_public_app_private_dep` returns exactly item 5.
9. **Diff behavior** — run 1 then run 2; assert the deleted app's
   `last_seen_run` is stale, the new app's `first_seen_run` is run 2, and the
   burn-down view moves.

---

## Also useful: the fixture is the demo

README examples, screenshots, and the sample HTML report all get generated from
this fixture rather than from a live portal. Documentation stays honest,
reproducible by anyone, and free of any real organization's data — which is the
whole constraint restated as a workflow rather than a rule to remember.

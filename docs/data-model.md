# Data Model

The reasoning behind `src/arcgis_inventory/db/schema.sql`, which is the
authoritative DDL — this document explains *why*, the SQL file is *what*.

SQLite. Every table below is portal-agnostic — nothing here encodes any
organization's structure, naming, or business rules.

---

## The three decisions that shape everything else

### 1. Derived data and authored data are separated absolutely

Some rows are *facts about the portal* and can be thrown away and rebuilt from
a fresh crawl at any time. Other rows are *human judgment* — "we're not fixing
this", "this app is being retired instead of migrated", "Marcy owns the
rebuild" — and losing them means losing weeks of triage.

| Derived (rebuildable) | Authored (must survive everything) |
|---|---|
| `resource`, `edge`, `usage_snapshot`, `crawl_error` | `migration` |
| `finding` rows themselves | `finding.status`, `finding.status_note` |
| `recommendation.target` and reasoning | `recommendation.override_target`, `override_note` |

A wipe-and-recrawl must never touch the right-hand column. This is the single
most important property of the schema, and it's why findings need stable
fingerprints (below) rather than autoincrement identity.

### 2. Raw responses are retained, so re-classification never needs a re-crawl

`resource.raw_json` and `resource.raw_data_json` hold the portal's own item
description and item data verbatim. Classification rules, scanner rules, and
recommendation rules *will* change constantly in early development. Crawling a
5,000-item org takes a long time and hammers someone's portal; re-running the
rules over stored JSON takes seconds and can run in CI.

This buys a `reprocess` subcommand that re-derives every classification,
edge, finding, and recommendation from stored raw data with no network at all.
Build that command early — it's the development loop.

### 3. Nothing is ever deleted; rows carry `first_seen_run` / `last_seen_run`

A resource that stops appearing in crawls isn't removed — its `last_seen_run`
just stops advancing. That makes "what disappeared since last month", "what's
new", and migration burn-down free, and those diffs are most of what makes the
tool feel like a program rather than a report.

---

## Entity overview

```
portal ──┬── run ──┬── crawl_error
         │         └── usage_snapshot
         │
         └── resource ──┬── edge (resource → resource)
                        ├── finding
                        ├── recommendation
                        └── migration
```

`resource` is deliberately one table for two node kinds — see below.

---

## Tables

### `portal`

Supports multiple portals in one database (an AGOL org *and* an Enterprise
deployment, which is the normal case).

```sql
CREATE TABLE portal (
  portal_id     INTEGER PRIMARY KEY,
  url           TEXT NOT NULL UNIQUE,   -- normalized, no trailing slash
  kind          TEXT NOT NULL,          -- 'online' | 'enterprise'
  org_id        TEXT,                   -- portal's own org identifier
  name          TEXT,
  version       TEXT,                   -- Enterprise version, e.g. '11.4'
  added_at      TEXT NOT NULL           -- ISO-8601 UTC
);
```

### `run`

One row per crawl. Everything derived is stamped with the run that produced it.

```sql
CREATE TABLE run (
  run_id        INTEGER PRIMARY KEY,
  portal_id     INTEGER NOT NULL REFERENCES portal(portal_id),
  started_at    TEXT NOT NULL,
  finished_at   TEXT,
  status        TEXT NOT NULL,          -- 'running' | 'complete' | 'failed' | 'partial'
  mode          TEXT NOT NULL,          -- 'crawl' | 'reprocess'
  tool_version  TEXT NOT NULL,
  rules_version TEXT,                   -- hash of the loaded rule files
  scope_json    TEXT,                   -- what was asked for: folders, types, owners
  item_count    INTEGER DEFAULT 0,
  error_count   INTEGER DEFAULT 0,
  notes         TEXT
);
```

`rules_version` matters more than it looks: when a finding changes between
runs you need to know whether the *portal* changed or the *rules* did.

### `resource` — the node table

One table for both portal items and bare service endpoints. Many dependencies
are **not portal items** — a web map can reference a map service by URL, a
geocoder hosted elsewhere, a print service on a different server. If items and
endpoints live in separate tables, every graph query becomes a union and the
NetworkX load gets ugly. One node table keeps edges uniform.

```sql
CREATE TABLE resource (
  resource_id     INTEGER PRIMARY KEY,
  portal_id       INTEGER NOT NULL REFERENCES portal(portal_id),
  kind            TEXT NOT NULL,        -- 'item' | 'endpoint'

  -- identity (exactly one of these is authoritative, per kind)
  item_id         TEXT,                 -- portal itemId, when kind='item'
  url_normalized  TEXT,                 -- canonical URL, when kind='endpoint'

  -- portal metadata (items only)
  title           TEXT,
  item_type       TEXT,                 -- portal's own 'type' string, verbatim
  type_keywords   TEXT,                 -- JSON array
  owner           TEXT,
  owner_exists    INTEGER,              -- 0/1/NULL — resolved against the user list
  folder_id       TEXT,
  created_at      TEXT,
  modified_at     TEXT,
  access          TEXT,                 -- 'private'|'org'|'shared'|'public'
  shared_groups   TEXT,                 -- JSON array of group ids
  num_views       INTEGER,
  size_bytes      INTEGER,
  tags            TEXT,                 -- JSON array
  snippet         TEXT,
  url             TEXT,                 -- item's own url field, as returned

  -- derived classification
  platform        TEXT,                 -- see vocabulary below
  platform_confidence TEXT,             -- 'certain'|'likely'|'guess'
  platform_evidence   TEXT,             -- JSON: which signals fired

  -- endpoint-only
  service_type    TEXT,                 -- 'FeatureServer'|'MapServer'|'GeocodeServer'|'GPServer'|'PrintServer'
  is_https        INTEGER,
  host            TEXT,                 -- for dev/staging host detection
  reachable       INTEGER,              -- 0/1/NULL from last probe
  http_status     INTEGER,

  -- raw retention
  raw_json        TEXT,                 -- item description, verbatim
  raw_data_json   TEXT,                 -- item data (web map JSON / WAB / EXB config)
  raw_fetched_run INTEGER REFERENCES run(run_id),

  first_seen_run  INTEGER NOT NULL REFERENCES run(run_id),
  last_seen_run   INTEGER NOT NULL REFERENCES run(run_id)
);

CREATE UNIQUE INDEX ux_resource_item ON resource(portal_id, item_id)
  WHERE item_id IS NOT NULL;
CREATE UNIQUE INDEX ux_resource_endpoint ON resource(portal_id, url_normalized)
  WHERE url_normalized IS NOT NULL;
CREATE INDEX ix_resource_platform ON resource(platform);
CREATE INDEX ix_resource_access ON resource(access);
```

**`platform` vocabulary** (derived, closed set — the portal's own `type` string
is kept verbatim in `item_type` and never overwritten):

`web_appbuilder`, `experience_builder`, `dashboard`, `instant_app`,
`storymap`, `hub_site`, `web_map`, `web_scene`, `feature_service`,
`map_service`, `image_service`, `geocode_service`, `gp_service`,
`print_service`, `custom_js_app`, `widget_package`, `form`, `notebook`,
`other`.

`platform_confidence` and `platform_evidence` exist because classification is
heuristic — a WAB app is identified by a mix of `type`, typeKeywords, the shape
of its data JSON, and its URL, and any of those can be absent. When the tool
tells someone they have 47 Web AppBuilder apps, they will ask how it knows, and
"which signal fired" has to be answerable per item.

### `edge` — dependencies

```sql
CREATE TABLE edge (
  edge_id         INTEGER PRIMARY KEY,
  from_resource   INTEGER NOT NULL REFERENCES resource(resource_id),
  to_resource     INTEGER NOT NULL REFERENCES resource(resource_id),
  relation        TEXT NOT NULL,
  source_path     TEXT,                 -- JSON pointer into the source config
  detail_json     TEXT,                 -- layer index, widget name, etc.
  first_seen_run  INTEGER NOT NULL REFERENCES run(run_id),
  last_seen_run   INTEGER NOT NULL REFERENCES run(run_id)
);

CREATE UNIQUE INDEX ux_edge ON edge(from_resource, to_resource, relation, source_path);
CREATE INDEX ix_edge_to ON edge(to_resource);   -- reverse lookup: "what breaks if this changes"
```

**`relation` vocabulary:** `operational_layer`, `basemap`, `table`,
`geocoder`, `gp_service`, `print_service`, `elevation_service`,
`widget_config`, `arcade_source`, `attachment_source`, `linked_item`,
`embedded_app`, `data_source`.

`source_path` earns its place twice over: it makes edges auditable ("this
dependency comes from `/widgets/3/config/searchLayers/0`") and it makes the
unique index correct — the same layer referenced from two different widgets is
genuinely two dependencies with different remediation work.

The index on `to_resource` is the `audit-sharing` and impact-analysis query.

### `finding` — scanner, sharing, and hygiene output, unified

One table for everything a rule can say about a resource. Separate tables for
"deprecated tech findings" and "sharing findings" would duplicate the entire
triage-state mechanism.

```sql
CREATE TABLE finding (
  finding_id      INTEGER PRIMARY KEY,
  fingerprint     TEXT NOT NULL UNIQUE,   -- stable identity across runs
  portal_id       INTEGER NOT NULL REFERENCES portal(portal_id),
  resource_id     INTEGER REFERENCES resource(resource_id),
  rule_id         TEXT NOT NULL,          -- e.g. 'arcgis-js-3', 'public-app-private-dep'
  category        TEXT NOT NULL,          -- 'deprecated_tech'|'sharing'|'ownership'|'hygiene'|'reachability'
  severity        TEXT NOT NULL,          -- 'critical'|'high'|'medium'|'low'|'info'
  title           TEXT NOT NULL,
  detail          TEXT,
  evidence_json   TEXT,                   -- what matched, where
  suggested_action TEXT,

  -- AUTHORED — survives re-crawl
  status          TEXT NOT NULL DEFAULT 'open',  -- 'open'|'acknowledged'|'wontfix'|'fixed'
  status_note     TEXT,
  status_at       TEXT,

  first_seen_run  INTEGER NOT NULL REFERENCES run(run_id),
  last_seen_run   INTEGER NOT NULL REFERENCES run(run_id),
  resolved_run    INTEGER REFERENCES run(run_id)  -- set when it stops appearing
);
```

**Fingerprint** = stable hash of `rule_id` + resource identity (itemId or
normalized URL) + the salient evidence that distinguishes one instance of the
rule from another on the same resource (e.g. the specific offending layer URL).
Deliberately *excludes* run id, timestamps, counts, and anything cosmetic.

Get this wrong and every crawl resurrects findings someone already dismissed,
which is the failure mode that makes people stop running scanners. Write the
fingerprint tests first.

Note `resolved_run` versus `status='fixed'`: the former is observed (the rule
stopped firing), the latter is claimed. Both are worth having, and disagreement
between them is interesting.

### `recommendation`

```sql
CREATE TABLE recommendation (
  resource_id     INTEGER PRIMARY KEY REFERENCES resource(resource_id),
  run_id          INTEGER NOT NULL REFERENCES run(run_id),
  target          TEXT NOT NULL,        -- 'retire'|'instant_app'|'experience_builder'|'custom'|'keep'|'unknown'
  confidence      TEXT NOT NULL,        -- 'certain'|'likely'|'guess'
  complexity      INTEGER,              -- 0-100, comparable across apps
  rules_fired     TEXT,                 -- JSON array of rule ids
  reasoning       TEXT NOT NULL,        -- human-readable, generated

  -- AUTHORED — survives re-crawl
  override_target TEXT,
  override_note   TEXT,
  override_at     TEXT
);
```

`reasoning` is not optional and not a nice-to-have. A bare verdict of
"Experience Builder" gets ignored; "single web map, 3 standard widgets, no
custom code, 1,240 views in 90 days" gets acted on. The recommendation engine's
real output is the argument, not the label.

Bias the rules toward **Instant Apps** for simple single-map apps rather than
defaulting everything to Experience Builder.

### `migration` — the authored tracking layer

```sql
CREATE TABLE migration (
  resource_id          INTEGER PRIMARY KEY REFERENCES resource(resource_id),
  status               TEXT NOT NULL DEFAULT 'not_started',
    -- 'not_started'|'in_progress'|'built'|'validated'|'cutover'|'retired'|'blocked'
  replacement_resource INTEGER REFERENCES resource(resource_id),
  replacement_url      TEXT,            -- when the replacement isn't a portal item
  owner_ref            TEXT,            -- free text, a name or ticket ref
  due_date             TEXT,
  blocked_reason       TEXT,
  notes                TEXT,
  updated_at           TEXT NOT NULL
);
```

Entirely human-authored. No crawl writes to it. This is the table the burn-down
chart reads from.

### `usage_snapshot`

```sql
CREATE TABLE usage_snapshot (
  resource_id  INTEGER NOT NULL REFERENCES resource(resource_id),
  run_id       INTEGER NOT NULL REFERENCES run(run_id),
  num_views    INTEGER,
  captured_at  TEXT NOT NULL,
  PRIMARY KEY (resource_id, run_id)
);
```

Deltas are computed at query time, never stored. Portal view counts are
cumulative and unreliable in absolute terms, but the *slope* between two crawls
is the only trustworthy "is anyone actually using this" signal available
without external analytics. Two crawls a month apart beats any single snapshot.

### `crawl_error`

```sql
CREATE TABLE crawl_error (
  error_id     INTEGER PRIMARY KEY,
  run_id       INTEGER NOT NULL REFERENCES run(run_id),
  resource_id  INTEGER REFERENCES resource(resource_id),
  target_url   TEXT,
  phase        TEXT NOT NULL,   -- 'search'|'item'|'item_data'|'service'|'user'
  http_status  INTEGER,
  message      TEXT,
  occurred_at  TEXT NOT NULL
);
```

Failures are results, not noise. A service that returns 403 during the crawl is
itself a finding — it usually means an app depends on something the crawling
account can't see, which is very often the same thing the *public* can't see.

---

## Views worth shipping

Ship these as SQL views so the CLI, the report, and anyone poking at the DB
with a SQLite browser all get the same answers.

- `v_public_app_private_dep` — public apps depending on non-public resources.
  The headline query.
- `v_orphaned` — resources whose owner no longer exists.
- `v_dev_host_refs` — production resources referencing dev/staging hosts.
- `v_http_services` — non-HTTPS service dependencies.
- `v_retirement_exposure` — WAB apps ranked by views × dependency fan-in.
- `v_shared_maps` — web maps used by more than one app (migrate once, fix many).
- `v_dead` — no owner, no views, not modified in N days.
- `v_migration_burndown` — `migration.status` counts over run history.

---

## Cross-cutting: URL normalization

One function, used everywhere, or edges will silently fail to dedupe and the
graph will be wrong in a way that's hard to see.

Rules: lowercase scheme and host; strip default ports; strip trailing slash;
preserve case in the path after `/rest/services/` (Enterprise paths are
case-sensitive in practice); separate the layer index from the service URL and
store it in `edge.detail_json`, not in the endpoint URL — otherwise
`.../FeatureServer/0` and `.../FeatureServer/3` become two unrelated nodes and
impact analysis breaks. Record `http` vs `https` in `is_https` but normalize
the stored URL to a single form so the same service reached both ways is one
node.

Property-based tests on this function. It's fifty lines of code that everything
downstream depends on.

---

## What is deliberately *not* in the schema

- **No credentials, tokens, or session state.** Ever. Environment only.
- **No `business_owner`, `due_date`, `last_tested` on `resource`** — the source
  brief listed these, but they're authored fields and they live in `migration`.
- **No accessibility findings.** That's BU2, a separate repo with a separate
  store. Tempting to unify; resist until both exist and the shape is known.
- **No screenshots or binary blobs.** Path references only, if ever.

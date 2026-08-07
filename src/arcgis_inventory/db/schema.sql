-- arcgis-inventory schema, v1
--
-- Three properties this schema exists to guarantee. Changing any of them is a
-- breaking change, not a refactor:
--
--   1. Derived data (rebuildable from a crawl) and authored data (human
--      judgment) are separated absolutely. A wipe-and-recrawl must never touch
--      an authored column.
--   2. Raw portal responses are retained, so re-classification never needs a
--      re-crawl. That is what makes `reprocess` possible.
--   3. Nothing is ever deleted. Rows carry first_seen_run / last_seen_run, and
--      "what disappeared" is a query rather than a lost fact.
--
-- Nothing in this file encodes any organization's structure, naming, or
-- business rules.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- schema_version
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schema_version (
  version     INTEGER NOT NULL,
  applied_at  TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- portal -- multiple portals in one database is the normal case (an AGOL org
-- and an Enterprise deployment).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS portal (
  portal_id     INTEGER PRIMARY KEY,
  url           TEXT NOT NULL UNIQUE,   -- normalized, no trailing slash
  kind          TEXT NOT NULL,          -- 'online' | 'enterprise'
  org_id        TEXT,                   -- portal's own org identifier
  name          TEXT,
  version       TEXT,                   -- Enterprise version, e.g. '11.4'
  added_at      TEXT NOT NULL           -- ISO-8601 UTC
);

-- ---------------------------------------------------------------------------
-- run -- one row per crawl. Everything derived is stamped with the run that
-- produced it.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS run (
  run_id        INTEGER PRIMARY KEY,
  portal_id     INTEGER NOT NULL REFERENCES portal(portal_id),
  started_at    TEXT NOT NULL,
  finished_at   TEXT,
  status        TEXT NOT NULL,          -- 'running'|'complete'|'failed'|'partial'
  mode          TEXT NOT NULL,          -- 'crawl' | 'reprocess'
  tool_version  TEXT NOT NULL,
  -- Hash of the loaded rule files. When a finding changes between runs you
  -- need to know whether the portal changed or the rules did.
  rules_version TEXT,
  scope_json    TEXT,                   -- what was asked for: folders, types, owners
  item_count    INTEGER DEFAULT 0,
  error_count   INTEGER DEFAULT 0,
  notes         TEXT
);

CREATE INDEX IF NOT EXISTS ix_run_portal ON run(portal_id, started_at);

-- ---------------------------------------------------------------------------
-- resource -- the node table.
--
-- One table for both portal items and bare service endpoints. Many
-- dependencies are not portal items: a web map can reference a map service by
-- URL, a geocoder hosted elsewhere, a print service on another server. Two
-- tables would make every graph query a union.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS resource (
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
  owner_exists    INTEGER,              -- 0/1/NULL -- resolved against the user list
  folder_id       TEXT,
  created_at      TEXT,
  modified_at     TEXT,
  access          TEXT,                 -- 'private'|'org'|'shared'|'public'
  shared_groups   TEXT,                 -- JSON array of group ids
  num_views       INTEGER,              -- NULL is not 0: unknown != unused
  size_bytes      INTEGER,
  tags            TEXT,                 -- JSON array
  snippet         TEXT,
  url             TEXT,                 -- item's own url field, as returned

  -- derived classification
  platform            TEXT,             -- closed vocabulary, see platform.py
  platform_confidence TEXT,             -- 'certain'|'likely'|'guess'
  platform_evidence   TEXT,             -- JSON: which signals fired

  -- endpoint-only
  service_type    TEXT,                 -- 'FeatureServer'|'MapServer'|'GeocodeServer'|'GPServer'|'PrintServer'
  is_https        INTEGER,
  host            TEXT,                 -- for dev/staging host detection
  reachable       INTEGER,              -- 0/1/NULL from last probe
  http_status     INTEGER,

  -- raw retention -- this is what makes `reprocess` possible
  raw_json        TEXT,                 -- item description, verbatim
  raw_data_json   TEXT,                 -- item data (web map / WAB / EXB config)
  raw_fetched_run INTEGER REFERENCES run(run_id),

  first_seen_run  INTEGER NOT NULL REFERENCES run(run_id),
  last_seen_run   INTEGER NOT NULL REFERENCES run(run_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_resource_item
  ON resource(portal_id, item_id) WHERE item_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_resource_endpoint
  ON resource(portal_id, url_normalized) WHERE url_normalized IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_resource_platform ON resource(platform);
CREATE INDEX IF NOT EXISTS ix_resource_access ON resource(access);
CREATE INDEX IF NOT EXISTS ix_resource_owner ON resource(owner);

-- ---------------------------------------------------------------------------
-- edge -- dependencies.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS edge (
  edge_id         INTEGER PRIMARY KEY,
  from_resource   INTEGER NOT NULL REFERENCES resource(resource_id),
  to_resource     INTEGER NOT NULL REFERENCES resource(resource_id),
  relation        TEXT NOT NULL,
  -- JSON pointer into the source config. Makes the edge auditable ("this
  -- dependency comes from /widgets/3/config/searchLayers/0") and makes the
  -- unique index correct -- the same layer referenced from two widgets is two
  -- dependencies with different remediation work.
  source_path     TEXT,
  detail_json     TEXT,                 -- layer index, widget name, etc.
  first_seen_run  INTEGER NOT NULL REFERENCES run(run_id),
  last_seen_run   INTEGER NOT NULL REFERENCES run(run_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_edge
  ON edge(from_resource, to_resource, relation, source_path);
-- The reverse lookup: "what breaks if this layer changes". This index is the
-- audit-sharing and impact-analysis query.
CREATE INDEX IF NOT EXISTS ix_edge_to ON edge(to_resource);

-- ---------------------------------------------------------------------------
-- finding -- scanner, sharing, ownership, and hygiene output, unified.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS finding (
  finding_id      INTEGER PRIMARY KEY,
  -- Stable identity across runs. Get this wrong and every crawl resurrects
  -- findings someone already dismissed, which is the failure mode that makes
  -- people stop running scanners. See fingerprint.py.
  fingerprint     TEXT NOT NULL UNIQUE,
  portal_id       INTEGER NOT NULL REFERENCES portal(portal_id),
  resource_id     INTEGER REFERENCES resource(resource_id),
  rule_id         TEXT NOT NULL,        -- e.g. 'arcgis-js-3', 'public-app-private-dep'
  category        TEXT NOT NULL,        -- 'deprecated_tech'|'sharing'|'ownership'|'hygiene'|'reachability'
  severity        TEXT NOT NULL,        -- 'critical'|'high'|'medium'|'low'|'info'
  title           TEXT NOT NULL,
  detail          TEXT,
  evidence_json   TEXT,                 -- what matched, where
  suggested_action TEXT,

  -- AUTHORED -- survives re-crawl and reprocess
  status          TEXT NOT NULL DEFAULT 'open',  -- 'open'|'acknowledged'|'wontfix'|'fixed'
  status_note     TEXT,
  status_at       TEXT,

  first_seen_run  INTEGER NOT NULL REFERENCES run(run_id),
  last_seen_run   INTEGER NOT NULL REFERENCES run(run_id),
  -- Observed, not claimed: set when the rule stops firing. Disagreement with
  -- status='fixed' is interesting.
  resolved_run    INTEGER REFERENCES run(run_id)
);

CREATE INDEX IF NOT EXISTS ix_finding_resource ON finding(resource_id);
CREATE INDEX IF NOT EXISTS ix_finding_rule ON finding(rule_id, severity);
CREATE INDEX IF NOT EXISTS ix_finding_status ON finding(status);

-- ---------------------------------------------------------------------------
-- recommendation
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS recommendation (
  resource_id     INTEGER PRIMARY KEY REFERENCES resource(resource_id),
  run_id          INTEGER NOT NULL REFERENCES run(run_id),
  target          TEXT NOT NULL,        -- 'retire'|'instant_app'|'experience_builder'|'custom'|'keep'|'unknown'
  confidence      TEXT NOT NULL,        -- 'certain'|'likely'|'guess'
  complexity      INTEGER,              -- 0-100, comparable across apps
  rules_fired     TEXT,                 -- JSON array of rule ids
  -- Not optional. A bare verdict of "Experience Builder" gets ignored; "single
  -- web map, 3 standard widgets, no custom code, 1,240 views in 90 days" gets
  -- acted on. The real output is the argument, not the label.
  reasoning       TEXT NOT NULL,

  -- AUTHORED -- survives re-crawl and reprocess
  override_target TEXT,
  override_note   TEXT,
  override_at     TEXT
);

-- ---------------------------------------------------------------------------
-- migration -- the authored tracking layer. No crawl ever writes to this table.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS migration (
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

-- ---------------------------------------------------------------------------
-- usage_snapshot -- deltas are computed at query time, never stored. Portal
-- view counts are unreliable in absolute terms; the slope between two crawls is
-- the only trustworthy "is anyone using this" signal without external analytics.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS usage_snapshot (
  resource_id  INTEGER NOT NULL REFERENCES resource(resource_id),
  run_id       INTEGER NOT NULL REFERENCES run(run_id),
  num_views    INTEGER,
  captured_at  TEXT NOT NULL,
  PRIMARY KEY (resource_id, run_id)
);

-- ---------------------------------------------------------------------------
-- crawl_error -- failures are results, not noise. A service that returns 403
-- during the crawl is itself a finding: it usually means an app depends on
-- something the crawling account can't see, which is very often the same thing
-- the public can't see.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS crawl_error (
  error_id     INTEGER PRIMARY KEY,
  run_id       INTEGER NOT NULL REFERENCES run(run_id),
  resource_id  INTEGER REFERENCES resource(resource_id),
  target_url   TEXT,
  phase        TEXT NOT NULL,   -- 'search'|'item'|'item_data'|'service'|'user'
  http_status  INTEGER,
  message      TEXT,
  occurred_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_crawl_error_run ON crawl_error(run_id);

-- ---------------------------------------------------------------------------
-- Views. Shipped as SQL so the CLI, the report, and anyone poking at the file
-- with a SQLite browser all get the same answers.
-- ---------------------------------------------------------------------------

-- The headline query: public apps depending on non-public resources -- i.e.
-- which public-facing apps are quietly broken for the public right now.
CREATE VIEW IF NOT EXISTS v_public_app_private_dep AS
SELECT
  app.resource_id      AS app_resource_id,
  app.item_id          AS app_item_id,
  app.title            AS app_title,
  app.platform         AS app_platform,
  app.access           AS app_access,
  dep.resource_id      AS dep_resource_id,
  dep.title            AS dep_title,
  COALESCE(dep.url_normalized, dep.item_id) AS dep_identity,
  dep.access           AS dep_access,
  e.relation           AS relation,
  e.source_path        AS source_path
FROM resource app
JOIN edge e     ON e.from_resource = app.resource_id
JOIN resource dep ON dep.resource_id = e.to_resource
WHERE app.access = 'public'
  AND dep.access IS NOT NULL
  AND dep.access <> 'public';

-- Resources whose owner no longer exists.
CREATE VIEW IF NOT EXISTS v_orphaned AS
SELECT resource_id, item_id, title, platform, owner, access, num_views, modified_at
FROM resource
WHERE kind = 'item' AND owner_exists = 0;

-- Production resources referencing dev/staging hosts. The host pattern list is
-- config-driven, not baked in -- this view exposes the host so the caller can
-- apply its own rules, rather than encoding somebody's naming convention here.
CREATE VIEW IF NOT EXISTS v_host_refs AS
SELECT DISTINCT
  src.resource_id AS from_resource_id,
  src.title       AS from_title,
  src.access      AS from_access,
  dep.host        AS host,
  dep.url_normalized AS dep_url,
  e.relation      AS relation
FROM edge e
JOIN resource src ON src.resource_id = e.from_resource
JOIN resource dep ON dep.resource_id = e.to_resource
WHERE dep.host IS NOT NULL;

-- Non-HTTPS service dependencies.
CREATE VIEW IF NOT EXISTS v_http_services AS
SELECT
  src.resource_id AS from_resource_id,
  src.title       AS from_title,
  src.access      AS from_access,
  dep.url_normalized AS dep_url,
  e.relation      AS relation
FROM edge e
JOIN resource src ON src.resource_id = e.from_resource
JOIN resource dep ON dep.resource_id = e.to_resource
WHERE dep.is_https = 0;

-- Web maps used by more than one app: migrate once, fix many.
CREATE VIEW IF NOT EXISTS v_shared_maps AS
SELECT
  m.resource_id,
  m.item_id,
  m.title,
  COUNT(DISTINCT e.from_resource) AS app_count
FROM resource m
JOIN edge e ON e.to_resource = m.resource_id
WHERE m.platform = 'web_map'
GROUP BY m.resource_id, m.item_id, m.title
HAVING COUNT(DISTINCT e.from_resource) > 1;

-- Web AppBuilder apps ranked by exposure: views x dependency fan-in. Where the
-- retirement deadline actually hurts.
CREATE VIEW IF NOT EXISTS v_retirement_exposure AS
SELECT
  r.resource_id,
  r.item_id,
  r.title,
  r.access,
  r.num_views,
  (SELECT COUNT(*) FROM edge e WHERE e.from_resource = r.resource_id) AS dep_count,
  COALESCE(r.num_views, 0)
    * (1 + (SELECT COUNT(*) FROM edge e WHERE e.from_resource = r.resource_id))
    AS exposure_score
FROM resource r
WHERE r.platform = 'web_appbuilder';

-- No owner, no views, untouched for a long time. The threshold is applied by
-- the caller; the view exposes the inputs rather than baking in a policy.
CREATE VIEW IF NOT EXISTS v_dead AS
SELECT resource_id, item_id, title, platform, owner, owner_exists,
       num_views, modified_at, access
FROM resource
WHERE kind = 'item'
  AND COALESCE(num_views, 0) = 0
  AND (owner_exists = 0 OR owner IS NULL);

-- Migration burn-down: authored status counts.
CREATE VIEW IF NOT EXISTS v_migration_burndown AS
SELECT status, COUNT(*) AS n
FROM migration
GROUP BY status;

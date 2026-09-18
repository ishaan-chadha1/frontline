-- Frontline retail intelligence schema.
-- SQLite for the pilot; ports to Postgres by swapping INTEGER PRIMARY KEY for
-- BIGSERIAL, TEXT timestamps for TIMESTAMPTZ, and dropping WITHOUT ROWID.
--
-- Rule: the event fact table holds only integers and two short dates.
-- Words live in the dimension tables, joined at render time, never in a count.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- dimensions

-- One self-referencing hierarchy for any industry.
-- Retail: country > state > cluster > store. Pharma: country > region > territory.
CREATE TABLE IF NOT EXISTS org_unit (
  id          INTEGER PRIMARY KEY,
  name        TEXT    NOT NULL,
  code        TEXT    UNIQUE,
  level       TEXT    NOT NULL,          -- named by the vertical config
  parent_id   INTEGER REFERENCES org_unit(id),
  depth       INTEGER NOT NULL DEFAULT 0,
  attrs_json  TEXT,
  is_active   INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_org_parent ON org_unit(parent_id);

-- One table for anything talked about: products, competitors, leads, molecules.
-- entity_type is declared per vertical, never hardcoded.
CREATE TABLE IF NOT EXISTS entity (
  id           INTEGER PRIMARY KEY,
  entity_type  TEXT    NOT NULL,
  name         TEXT    NOT NULL,         -- canonical form
  attrs_json   TEXT,
  is_active    INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_entity_type ON entity(entity_type, name);

-- "Ather", "athar", "AT" must collapse to one id at WRITE time,
-- or every competitive number is quietly understated.
CREATE TABLE IF NOT EXISTS entity_alias (
  id          INTEGER PRIMARY KEY,
  entity_id   INTEGER NOT NULL REFERENCES entity(id),
  alias_norm  TEXT    NOT NULL,          -- lowercased, unaccented, collapsed
  source      TEXT    NOT NULL,          -- seed | llm_proposed | human_confirmed
  created_at  TEXT    NOT NULL,
  UNIQUE(entity_id, alias_norm)
);
CREATE INDEX IF NOT EXISTS ix_alias_lookup ON entity_alias(alias_norm);

CREATE TABLE IF NOT EXISTS person (
  id             INTEGER PRIMARY KEY,
  name           TEXT    NOT NULL,
  phone_e164     TEXT    NOT NULL UNIQUE, -- the WhatsApp identity, the ingest key
  unit_id        INTEGER REFERENCES org_unit(id),
  role           TEXT    NOT NULL,        -- sales_exec | store_manager | asm | admin
  reports_to_id  INTEGER REFERENCES person(id),
  language_pref  TEXT,
  joined_on      TEXT,
  is_active      INTEGER NOT NULL DEFAULT 1
);

-- Integer ids are permanent and never reused: renaming a label touches one row,
-- not millions of event rows.
CREATE TABLE IF NOT EXISTS taxonomy_node (
  id          INTEGER PRIMARY KEY,
  vertical    TEXT    NOT NULL,
  path        TEXT    NOT NULL,          -- 'objection.PRICE.EMI'
  dimension   TEXT    NOT NULL,          -- 'objection'
  label       TEXT    NOT NULL,
  parent_id   INTEGER REFERENCES taxonomy_node(id),
  depth       INTEGER NOT NULL,
  sort_order  INTEGER NOT NULL DEFAULT 0,
  added_in    TEXT    NOT NULL,          -- taxonomy version first seen
  retired_in  TEXT,                      -- set instead of deleting
  UNIQUE(vertical, path)
);
CREATE INDEX IF NOT EXISTS ix_taxonomy_dim ON taxonomy_node(vertical, dimension);

-- ------------------------------------------------------- the provenance chain

-- Declared before raw_capture: Postgres enforces reference order, and these two
-- point at each other. capture_prompt.capture_id is deliberately left without a
-- foreign key to break the cycle -- it is the one place we trade a constraint
-- for portability.
CREATE TABLE IF NOT EXISTS capture_prompt (
  id           INTEGER PRIMARY KEY,
  person_id    INTEGER NOT NULL REFERENCES person(id),
  sent_at      TEXT    NOT NULL,
  prompt_type  TEXT    NOT NULL,   -- scheduled | manual | eod_gap | closing_count
  responded    INTEGER NOT NULL DEFAULT 0,
  capture_id   INTEGER
);

CREATE TABLE IF NOT EXISTS raw_capture (
  id             INTEGER PRIMARY KEY,
  external_id    TEXT    NOT NULL UNIQUE, -- WhatsApp message id; idempotency key
  person_id      INTEGER NOT NULL REFERENCES person(id),
  unit_id        INTEGER REFERENCES org_unit(id),
  audio_sha256   TEXT    NOT NULL,
  audio_uri      TEXT    NOT NULL,
  audio_bytes    INTEGER,
  duration_ms    INTEGER,
  mime_type      TEXT,
  captured_at    TEXT    NOT NULL,        -- exact instant, UTC
  captured_on    TEXT    NOT NULL,        -- local calendar date = reporting grain
  received_at    TEXT    NOT NULL,
  prompt_id      INTEGER REFERENCES capture_prompt(id),
  status         TEXT    NOT NULL DEFAULT 'received',
  failure_reason TEXT,
  -- Seeded demo data must never be mistakable for real dealer data, including
  -- six months from now when nobody remembers which is which.
  is_simulated   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_capture_person_date ON raw_capture(person_id, captured_on);
CREATE INDEX IF NOT EXISTS ix_capture_status ON raw_capture(status);

CREATE TABLE IF NOT EXISTS transcript (
  id              INTEGER PRIMARY KEY,
  capture_id      INTEGER NOT NULL REFERENCES raw_capture(id),
  engine          TEXT    NOT NULL,
  engine_version  TEXT    NOT NULL,
  language        TEXT,
  text            TEXT    NOT NULL,
  segments_json   TEXT,
  mean_confidence REAL,
  duration_ms     INTEGER,
  cost_usd        REAL,
  created_at      TEXT    NOT NULL,
  is_active       INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_transcript_capture ON transcript(capture_id, is_active);

-- Append-only. A better prompt next month writes a NEW run and supersedes the
-- old one, so improvements are measurable instead of assumed.
CREATE TABLE IF NOT EXISTS extraction_run (
  id               INTEGER PRIMARY KEY,
  transcript_id    INTEGER NOT NULL REFERENCES transcript(id),
  model            TEXT    NOT NULL,
  prompt_version   TEXT    NOT NULL,
  taxonomy_version TEXT    NOT NULL,
  input_tokens     INTEGER,
  output_tokens    INTEGER,
  cost_usd         REAL,
  latency_ms       INTEGER,
  created_at       TEXT    NOT NULL,
  is_active        INTEGER NOT NULL DEFAULT 1,
  superseded_by    INTEGER REFERENCES extraction_run(id)
);
CREATE INDEX IF NOT EXISTS ix_run_transcript ON extraction_run(transcript_id, is_active);

-- The fact table. Integers only. Every aggregate is a GROUP BY over this with
-- zero joins, which is why org ancestors are flattened onto the row.
CREATE TABLE IF NOT EXISTS event (
  id                INTEGER PRIMARY KEY,
  run_id            INTEGER NOT NULL REFERENCES extraction_run(id),
  capture_id        INTEGER NOT NULL REFERENCES raw_capture(id),

  person_id         INTEGER NOT NULL,
  unit_id           INTEGER NOT NULL,
  unit_l1           INTEGER NOT NULL,
  unit_l2           INTEGER,
  unit_l3           INTEGER,
  occurred_on       TEXT    NOT NULL,

  taxonomy_node_id  INTEGER NOT NULL REFERENCES taxonomy_node(id),
  subject_id        INTEGER REFERENCES entity(id),   -- slot A, declared per vertical
  rival_id          INTEGER REFERENCES entity(id),   -- slot B
  actor_id          INTEGER REFERENCES entity(id),   -- slot C
  slot_d_id         INTEGER REFERENCES entity(id),   -- spare
  polarity          INTEGER NOT NULL DEFAULT 0,      -- -1 / 0 / +1
  intensity         INTEGER,                         -- 1..3 when signalled

  span_start        INTEGER,
  span_end          INTEGER,
  confidence        INTEGER NOT NULL,                -- 0..100
  rep_confirmed     INTEGER,                         -- NULL / 1 / 0
  is_active         INTEGER NOT NULL DEFAULT 1
);
-- Partial on is_active: superseded rows cost disk, never query time.
CREATE INDEX IF NOT EXISTS ix_event_node_date   ON event(taxonomy_node_id, occurred_on) WHERE is_active = 1;
CREATE INDEX IF NOT EXISTS ix_event_unit_date   ON event(unit_l1, occurred_on, taxonomy_node_id) WHERE is_active = 1;
CREATE INDEX IF NOT EXISTS ix_event_person_date ON event(person_id, occurred_on) WHERE is_active = 1;
CREATE INDEX IF NOT EXISTS ix_event_capture     ON event(capture_id);
CREATE INDEX IF NOT EXISTS ix_event_rival       ON event(rival_id, occurred_on) WHERE is_active = 1;

-- A new competitor entering the market shows up here first.
CREATE TABLE IF NOT EXISTS unresolved_mention (
  id            INTEGER PRIMARY KEY,
  event_id      INTEGER REFERENCES event(id),
  slot          TEXT    NOT NULL,
  surface_form  TEXT    NOT NULL,
  occurrences   INTEGER NOT NULL DEFAULT 1,
  reviewed      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_unresolved ON unresolved_mention(reviewed, occurrences DESC);

-- --------------------------------------------------------- capture and consent


CREATE TABLE IF NOT EXISTS confirmation (
  id               INTEGER PRIMARY KEY,
  capture_id       INTEGER NOT NULL REFERENCES raw_capture(id),
  sent_at          TEXT    NOT NULL,
  responded_at     TEXT,
  response_type    TEXT,            -- confirmed | corrected | ignored
  correction_text  TEXT,
  correction_audio TEXT,
  new_run_id       INTEGER REFERENCES extraction_run(id)
);

-- expected_captures must come from an INDEPENDENT source (footfall, enquiry log,
-- manager's closing count). A rep's own rolling median is circular: it reports
-- 100% coverage for someone capturing 40% of reality.
CREATE TABLE IF NOT EXISTS shift (
  id                INTEGER PRIMARY KEY,
  person_id         INTEGER NOT NULL REFERENCES person(id),
  occurred_on       TEXT    NOT NULL,
  scheduled         INTEGER NOT NULL DEFAULT 1,
  expected_captures INTEGER,
  count_source      TEXT,            -- footfall | enquiry_log | manager_count | baseline
  UNIQUE(person_id, occurred_on)
);

CREATE TABLE IF NOT EXISTS consent (
  person_id    INTEGER PRIMARY KEY REFERENCES person(id),
  granted_on   TEXT NOT NULL,
  version      TEXT NOT NULL,
  withdrawn_on TEXT
);

-- ------------------------------------------------------------- derived (warm)

-- Sparse. Stores the PREDICATE (its own primary key), never a copy of its
-- evidence -- drill-down re-runs the predicate, so evidence cannot go stale.
CREATE TABLE IF NOT EXISTS rollup_daily (
  occurred_on      TEXT    NOT NULL,
  unit_id          INTEGER NOT NULL,
  unit_l1          INTEGER NOT NULL,
  subject_id       INTEGER,
  taxonomy_node_id INTEGER NOT NULL,
  event_count      INTEGER NOT NULL,
  confirmed_count  INTEGER NOT NULL,
  capture_count    INTEGER NOT NULL,   -- the denominator
  person_count     INTEGER NOT NULL,   -- guards single-rep artifacts
  mean_confidence  INTEGER NOT NULL,
  PRIMARY KEY (occurred_on, unit_id, subject_id, taxonomy_node_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_rollup_node_date ON rollup_daily(taxonomy_node_id, occurred_on);
CREATE INDEX IF NOT EXISTS ix_rollup_unit_date ON rollup_daily(unit_l1, occurred_on, taxonomy_node_id);

CREATE TABLE IF NOT EXISTS person_day_profile (
  person_id          INTEGER NOT NULL,
  occurred_on        TEXT    NOT NULL,
  captures_expected  INTEGER NOT NULL,
  captures_actual    INTEGER NOT NULL,
  coverage_pct       INTEGER NOT NULL,
  event_count        INTEGER NOT NULL,
  confirm_rate_pct   INTEGER NOT NULL,
  top_nodes_json     TEXT    NOT NULL,   -- render-ready, read whole, never filtered
  delta_vs_peer_json TEXT    NOT NULL,
  PRIMARY KEY (person_id, occurred_on)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS schema_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- OKEngine PostgreSQL read projection (okengine#566).
-- Derived state only: canonical state lives in Markdown.

CREATE TABLE IF NOT EXISTS projection_runs (
    epoch           bigserial PRIMARY KEY,
    started_at      timestamptz NOT NULL DEFAULT now(),
    finished_at     timestamptz,
    ok              boolean NOT NULL DEFAULT false,
    stats           jsonb NOT NULL DEFAULT '{}'::jsonb,
    error           text,
    corpus_epoch    bigint NOT NULL DEFAULT 0,
    mode            text NOT NULL DEFAULT 'reconcile'
);
ALTER TABLE projection_runs ADD COLUMN IF NOT EXISTS corpus_epoch bigint NOT NULL DEFAULT 0;
ALTER TABLE projection_runs ADD COLUMN IF NOT EXISTS mode text NOT NULL DEFAULT 'reconcile';

CREATE TABLE IF NOT EXISTS pages (
    path            text PRIMARY KEY,
    namespace       text NOT NULL,
    canonical_id    text,
    slug            text NOT NULL,
    type            text,
    title           text,
    status          text,
    is_tombstoned   boolean NOT NULL DEFAULT false,
    superseded_by   text,
    published       date,
    ingested        date,
    updated         date,
    fm              jsonb NOT NULL DEFAULT '{}'::jsonb,
    body_chars      integer NOT NULL DEFAULT 0,
    content_digest  text NOT NULL,
    fm_error        text,
    file_mtime      timestamptz,
    epoch           bigint NOT NULL REFERENCES projection_runs(epoch),
    indexed_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS pages_namespace_type
    ON pages (namespace, type) WHERE NOT is_tombstoned;
CREATE INDEX IF NOT EXISTS pages_status ON pages (status);
CREATE INDEX IF NOT EXISTS pages_published ON pages (published DESC) WHERE NOT is_tombstoned;
CREATE INDEX IF NOT EXISTS pages_ingested ON pages (ingested DESC) WHERE NOT is_tombstoned;
CREATE INDEX IF NOT EXISTS pages_id ON pages (canonical_id) WHERE canonical_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS pages_slug ON pages (slug);
CREATE INDEX IF NOT EXISTS pages_epoch ON pages (epoch);
CREATE INDEX IF NOT EXISTS pages_fm_gin ON pages USING gin (fm jsonb_path_ops);

CREATE TABLE IF NOT EXISTS links (
    src_path        text NOT NULL REFERENCES pages(path) ON DELETE CASCADE,
    target_ref      text NOT NULL,
    target_path     text,
    resolution      text NOT NULL CHECK
                    (resolution IN ('exact', 'id', 'alias', 'slug', 'ambiguous', 'unresolved')),
    section         text NOT NULL DEFAULT '',
    PRIMARY KEY (src_path, target_ref, section)
);

CREATE INDEX IF NOT EXISTS links_target ON links (target_path)
    WHERE target_path IS NOT NULL;
CREATE INDEX IF NOT EXISTS links_resolution ON links (resolution);

-- A view's existing column names and order cannot be changed by CREATE OR REPLACE.
-- The view is derived and has no data of its own, so recreate it on every bootstrap;
-- this permits projection-schema upgrades without touching the projected tables.
DROP VIEW IF EXISTS v_projection_health;
CREATE VIEW v_projection_health AS
    SELECT epoch, corpus_epoch, mode, started_at, finished_at, stats,
           EXTRACT(epoch FROM now() - finished_at)::bigint AS age_seconds
    FROM projection_runs
    WHERE ok AND finished_at IS NOT NULL
    ORDER BY epoch DESC
    LIMIT 1;

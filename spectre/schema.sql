-- Spectre — SQLite schema
-- Applied idempotently at startup (CREATE ... IF NOT EXISTS).

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- Media outlets, mirrored from config/sources.yaml at each ingest run.
CREATE TABLE IF NOT EXISTS sources (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    orientation TEXT NOT NULL CHECK (orientation IN
                    ('gauche', 'centre-gauche', 'centre', 'centre-droit', 'droite')),
    owner       TEXT,
    active      INTEGER NOT NULL DEFAULT 1
);

-- One row per RSS entry. Only what the feed exposes: title + summary.
CREATE TABLE IF NOT EXISTS articles (
    id           INTEGER PRIMARY KEY,
    source_id    TEXT NOT NULL REFERENCES sources(id),
    guid         TEXT,                -- RSS guid, may be NULL
    url          TEXT NOT NULL UNIQUE, -- canonical URL (tracking params stripped)
    title        TEXT NOT NULL,
    summary      TEXT,                -- plain text, HTML stripped
    published_at TEXT,                -- ISO 8601 UTC
    fetched_at   TEXT NOT NULL,       -- ISO 8601 UTC
    embedding    BLOB                 -- float32 numpy bytes; NULL until clustered
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_articles_source_guid
    ON articles(source_id, guid) WHERE guid IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_articles_published_at ON articles(published_at);

-- One cluster = one news event.
CREATE TABLE IF NOT EXISTS clusters (
    id               INTEGER PRIMARY KEY,
    title            TEXT,             -- title of the most central member
    centroid         BLOB NOT NULL,    -- float32 numpy bytes, L2-normalized
    n_members        INTEGER NOT NULL DEFAULT 0,
    blindspot_score  REAL,             -- -1 (left-only) .. +1 (right-only); NULL = not computed
    divergence_score REAL,             -- 0 (same vocabulary) .. 1 (disjoint framing)
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_clusters_updated_at ON clusters(updated_at);

CREATE TABLE IF NOT EXISTS cluster_members (
    cluster_id INTEGER NOT NULL REFERENCES clusters(id),
    article_id INTEGER NOT NULL REFERENCES articles(id),
    similarity REAL,                  -- cosine sim to centroid at attach time
    PRIMARY KEY (cluster_id, article_id)
);
-- An article belongs to at most one cluster.
CREATE UNIQUE INDEX IF NOT EXISTS idx_cluster_members_article
    ON cluster_members(article_id);

-- Analysis results, one JSON payload per (cluster, kind).
-- kind = 'blindspot'      -> {"score": .., "left": n, "center": n, "right": n, "weighted": {..}}
-- kind = 'vocab_contrast' -> {"left_terms": [[term, z], ..], "right_terms": [..], "divergence": ..}
-- kind = 'llm_framing'    -> {"summary": .., "framing": {..}, "omissions": [..], "model": ..}
CREATE TABLE IF NOT EXISTS analyses (
    id         INTEGER PRIMARY KEY,
    cluster_id INTEGER NOT NULL REFERENCES clusters(id),
    kind       TEXT NOT NULL CHECK (kind IN ('blindspot', 'vocab_contrast', 'llm_framing')),
    payload    TEXT NOT NULL,         -- JSON
    created_at TEXT NOT NULL,
    UNIQUE (cluster_id, kind)         -- re-analysis overwrites (INSERT OR REPLACE)
);

-- One row per feed per ingest run; a failing feed never aborts the run.
CREATE TABLE IF NOT EXISTS fetch_log (
    id         INTEGER PRIMARY KEY,
    source_id  TEXT NOT NULL,
    feed_url   TEXT NOT NULL,
    status     TEXT NOT NULL CHECK (status IN ('ok', 'http_error', 'parse_error')),
    http_code  INTEGER,
    n_entries  INTEGER,               -- entries seen in the feed
    n_new      INTEGER,               -- entries actually inserted
    error      TEXT,
    fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fetch_log_fetched_at ON fetch_log(fetched_at);

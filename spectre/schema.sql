PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sources (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    orientation TEXT NOT NULL,
    editorial_style TEXT NOT NULL DEFAULT 'mixte' CHECK (editorial_style IN
                    ('factuel', 'mixte', 'opinion')),
    paywall     TEXT NOT NULL DEFAULT 'none' CHECK (paywall IN ('none', 'partial', 'full')),
    owner       TEXT,
    active      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS articles (
    id           INTEGER PRIMARY KEY,
    source_id    TEXT NOT NULL REFERENCES sources(id),
    guid         TEXT,
    url          TEXT NOT NULL UNIQUE,
    title        TEXT NOT NULL,
    summary      TEXT,
    published_at TEXT,
    fetched_at   TEXT NOT NULL,
    embedding    BLOB
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_articles_source_guid
    ON articles(source_id, guid) WHERE guid IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_articles_published_at ON articles(published_at);

CREATE TABLE IF NOT EXISTS clusters (
    id               INTEGER PRIMARY KEY,
    title            TEXT,
    centroid         BLOB NOT NULL,
    n_members        INTEGER NOT NULL DEFAULT 0,
    blindspot_score  REAL,
    divergence_score REAL,
    category         TEXT,
    suspect_merge    INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_clusters_updated_at ON clusters(updated_at);

CREATE TABLE IF NOT EXISTS cluster_members (
    cluster_id INTEGER NOT NULL REFERENCES clusters(id),
    article_id INTEGER NOT NULL REFERENCES articles(id),
    similarity REAL,
    PRIMARY KEY (cluster_id, article_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_cluster_members_article
    ON cluster_members(article_id);

CREATE TABLE IF NOT EXISTS analyses (
    id         INTEGER PRIMARY KEY,
    cluster_id INTEGER NOT NULL REFERENCES clusters(id),
    kind       TEXT NOT NULL CHECK (kind IN ('blindspot', 'vocab_contrast', 'ollama')),
    payload    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (cluster_id, kind)
);

CREATE TABLE IF NOT EXISTS fetch_log (
    id         INTEGER PRIMARY KEY,
    source_id  TEXT NOT NULL,
    feed_url   TEXT NOT NULL,
    status     TEXT NOT NULL CHECK (status IN ('ok', 'http_error', 'parse_error')),
    http_code  INTEGER,
    n_entries  INTEGER,
    n_new      INTEGER,
    error      TEXT,
    fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fetch_log_fetched_at ON fetch_log(fetched_at);

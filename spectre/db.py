"""SQLite layer: connection, schema, queries.

The whole app shares this module so SQL never leaks into business code.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from .models import Article, Source

logger = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).parent / "schema.sql"

# Articles older than this are deleted at purge time (the DB is committed to
# git, so it must stay small).
PURGE_MAX_AGE_DAYS = 30
# Articles outside the clustering window no longer need their embedding NOR
# their summary. Embeddings: keeps the committed DB lean. Summaries: the repo
# is public, so keeping them would redistribute press excerpts — they are
# transient working data (droits voisins), erased once analyses are done.
CLUSTERING_WINDOW_HOURS = 72


def compact(db_path: str | Path) -> None:
    """Prepare the DB file for a git commit: single compact file, no -wal/-shm.

    Uses a raw connection on purpose: connect() would re-apply the schema's
    journal_mode=WAL pragma and recreate the sidecar files. The next normal
    connect() switches back to WAL automatically.
    """
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("VACUUM")
    conn.close()
    logger.info("compacted %s (WAL checkpointed, journal=DELETE, vacuumed)", db_path)


def utcnow_iso() -> str:
    """Current UTC time as ISO 8601 with second precision."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open (and initialize if needed) the database."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.execute("PRAGMA foreign_keys = ON")
    _migrate_analyses_kind(conn)
    _migrate_clusters_category(conn)
    _migrate_sources_editorial_style(conn)
    _migrate_sources_paywall(conn)
    _migrate_clusters_suspect(conn)
    _migrate_sources_orientation_check(conn)
    return conn


def _migrate_sources_orientation_check(conn: sqlite3.Connection) -> None:
    """Drop the legacy orientation CHECK (5-value) so the 7-position axis fits.

    SQLite can't ALTER a CHECK, so the table is rebuilt once. Incoming FK from
    articles.source_id is preserved (ids are unchanged); foreign_keys is toggled
    off for the swap.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='sources'"
    ).fetchone()
    if row is None or "CHECK (orientation IN" not in row["sql"]:
        return
    logger.info("migrating sources table: dropping legacy orientation CHECK")
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.executescript(
        """
        BEGIN;
        CREATE TABLE sources_new (
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            orientation TEXT NOT NULL,
            editorial_style TEXT NOT NULL DEFAULT 'mixte' CHECK (editorial_style IN
                            ('factuel', 'mixte', 'opinion')),
            paywall     TEXT NOT NULL DEFAULT 'none' CHECK (paywall IN ('none', 'partial', 'full')),
            owner       TEXT,
            active      INTEGER NOT NULL DEFAULT 1
        );
        INSERT INTO sources_new
            SELECT id, name, orientation, editorial_style, paywall, owner, active FROM sources;
        DROP TABLE sources;
        ALTER TABLE sources_new RENAME TO sources;
        COMMIT;
        """
    )
    conn.execute("PRAGMA foreign_keys=ON")


def connect_readonly(db_path: str | Path) -> sqlite3.Connection:
    """Open an existing database for read commands.

    When the schema is current this avoids PRAGMAs that dirty the committed
    SQLite file after compaction. If an old checked-out DB needs a local
    migration, fall back to connect() once.
    """
    path = Path(db_path).resolve()
    uri = f"file:{quote(str(path), safe='/')}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if not _schema_current_for_reads(conn):
        conn.close()
        return connect(path)
    return conn


def _table_has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return column in {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def _schema_current_for_reads(conn: sqlite3.Connection) -> bool:
    """Whether read-only render/inspect queries can run without migrations."""
    return (
        _table_has_column(conn, "sources", "editorial_style")
        and _table_has_column(conn, "sources", "paywall")
        and _table_has_column(conn, "clusters", "suspect_merge")
        and _table_has_column(conn, "clusters", "category")
    )


def _migrate_clusters_category(conn: sqlite3.Connection) -> None:
    """One-shot migration: add clusters.category to pre-existing databases."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(clusters)")}
    if "category" not in cols:
        logger.info("migrating clusters table: adding category column")
        conn.execute("ALTER TABLE clusters ADD COLUMN category TEXT")
        conn.commit()


def _migrate_sources_editorial_style(conn: sqlite3.Connection) -> None:
    """One-shot migration: add sources.editorial_style to existing databases."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sources)")}
    if "editorial_style" not in cols:
        logger.info("migrating sources table: adding editorial_style column")
        conn.execute(
            """
            ALTER TABLE sources ADD COLUMN editorial_style TEXT NOT NULL DEFAULT 'mixte'
            CHECK (editorial_style IN ('factuel', 'mixte', 'opinion'))
            """
        )
        conn.commit()


def _migrate_sources_paywall(conn: sqlite3.Connection) -> None:
    """One-shot migration: add sources.paywall to existing databases."""
    if not _table_has_column(conn, "sources", "paywall"):
        logger.info("migrating sources table: adding paywall column")
        conn.execute(
            """
            ALTER TABLE sources ADD COLUMN paywall TEXT NOT NULL DEFAULT 'none'
            CHECK (paywall IN ('none', 'partial', 'full'))
            """
        )
        conn.commit()


def _migrate_clusters_suspect(conn: sqlite3.Connection) -> None:
    """One-shot migration: add clusters.suspect_merge to existing databases."""
    if not _table_has_column(conn, "clusters", "suspect_merge"):
        logger.info("migrating clusters table: adding suspect_merge column")
        conn.execute("ALTER TABLE clusters ADD COLUMN suspect_merge INTEGER NOT NULL DEFAULT 0")
        conn.commit()


def _migrate_analyses_kind(conn: sqlite3.Connection) -> None:
    """One-shot migration: allow kind='ollama' in pre-existing databases.

    SQLite cannot ALTER a CHECK constraint, so the table is rebuilt once.
    Fresh databases already carry the new constraint from schema.sql.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'analyses'"
    ).fetchone()
    if row is None or "'ollama'" in row["sql"]:
        return
    logger.info("migrating analyses table: allowing kind='ollama'")
    conn.executescript(
        """
        BEGIN;
        ALTER TABLE analyses RENAME TO analyses_old;
        CREATE TABLE analyses (
            id         INTEGER PRIMARY KEY,
            cluster_id INTEGER NOT NULL REFERENCES clusters(id),
            kind       TEXT NOT NULL CHECK (kind IN ('blindspot', 'vocab_contrast', 'ollama')),
            payload    TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (cluster_id, kind)
        );
        INSERT INTO analyses SELECT * FROM analyses_old WHERE kind != 'llm_framing';
        DROP TABLE analyses_old;
        COMMIT;
        """
    )


def sync_sources(conn: sqlite3.Connection, sources: list[Source]) -> None:
    """Mirror config/sources.yaml into the sources table."""
    conn.executemany(
        """
        INSERT INTO sources (id, name, orientation, editorial_style, paywall, owner, active)
        VALUES (:id, :name, :orientation, :editorial_style, :paywall, :owner, :active)
        ON CONFLICT(id) DO UPDATE SET
            name = excluded.name,
            orientation = excluded.orientation,
            editorial_style = excluded.editorial_style,
            paywall = excluded.paywall,
            owner = excluded.owner,
            active = excluded.active
        """,
        [
            {
                "id": s.id,
                "name": s.name,
                "orientation": s.orientation,
                "editorial_style": s.editorial_style,
                "paywall": s.paywall,
                "owner": s.owner,
                "active": int(s.active),
            }
            for s in sources
        ],
    )
    conn.commit()


def insert_article(conn: sqlite3.Connection, article: Article) -> bool:
    """Insert an article; return True if it was new.

    Dedup relies on the schema: UNIQUE(url) and UNIQUE(source_id, guid).
    """
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO articles
            (source_id, guid, url, title, summary, published_at, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            article.source_id,
            article.guid,
            article.url,
            article.title,
            article.summary,
            article.published_at,
            article.fetched_at,
        ),
    )
    return cur.rowcount == 1


def log_fetch(
    conn: sqlite3.Connection,
    source_id: str,
    feed_url: str,
    status: str,
    http_code: int | None = None,
    n_entries: int | None = None,
    n_new: int | None = None,
    error: str | None = None,
) -> None:
    """Record the outcome of one feed fetch."""
    conn.execute(
        """
        INSERT INTO fetch_log
            (source_id, feed_url, status, http_code, n_entries, n_new, error, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (source_id, feed_url, status, http_code, n_entries, n_new, error, utcnow_iso()),
    )
    conn.commit()


def purge(conn: sqlite3.Connection, max_age_days: int = PURGE_MAX_AGE_DAYS) -> dict[str, int]:
    """Delete old rows and NULL stale embeddings; return counts per action.

    Two independent horizons:
    - articles older than `max_age_days` are deleted (with their cluster
      memberships; clusters left empty are deleted too);
    - articles outside the 72h clustering window keep their row but lose
      their embedding BLOB and their RSS summary: both are working data only
      (the DB is committed to a PUBLIC git repo on every CI run — summaries
      must not be redistributed beyond the processing window).
    """
    now = datetime.now(timezone.utc)
    delete_cutoff = (now - timedelta(days=max_age_days)).isoformat(timespec="seconds")
    embed_cutoff = (now - timedelta(hours=CLUSTERING_WINDOW_HOURS)).isoformat(timespec="seconds")

    old = "COALESCE(published_at, fetched_at) < ?"
    counts: dict[str, int] = {}
    counts["members_deleted"] = conn.execute(
        f"DELETE FROM cluster_members WHERE article_id IN (SELECT id FROM articles WHERE {old})",
        (delete_cutoff,),
    ).rowcount
    counts["articles_deleted"] = conn.execute(
        f"DELETE FROM articles WHERE {old}", (delete_cutoff,)
    ).rowcount
    counts["analyses_deleted"] = conn.execute(
        """
        DELETE FROM analyses WHERE cluster_id IN (
            SELECT c.id FROM clusters c
            LEFT JOIN cluster_members m ON m.cluster_id = c.id
            WHERE m.article_id IS NULL
        )
        """
    ).rowcount
    counts["clusters_deleted"] = conn.execute(
        """
        DELETE FROM clusters WHERE id NOT IN (SELECT DISTINCT cluster_id FROM cluster_members)
        """
    ).rowcount
    counts["embeddings_nulled"] = conn.execute(
        """
        UPDATE articles SET embedding = NULL
        WHERE embedding IS NOT NULL AND COALESCE(published_at, fetched_at) < ?
        """,
        (embed_cutoff,),
    ).rowcount
    counts["summaries_nulled"] = conn.execute(
        """
        UPDATE articles SET summary = NULL
        WHERE summary IS NOT NULL AND COALESCE(published_at, fetched_at) < ?
        """,
        (embed_cutoff,),
    ).rowcount
    counts["fetch_log_deleted"] = conn.execute(
        "DELETE FROM fetch_log WHERE fetched_at < ?", (delete_cutoff,)
    ).rowcount
    conn.commit()
    logger.info("purge: %s", counts)
    return counts


def articles_to_embed(conn: sqlite3.Connection, since: str) -> list[sqlite3.Row]:
    """Window articles that still need an embedding."""
    return conn.execute(
        """
        SELECT id, title, summary FROM articles
        WHERE embedding IS NULL AND COALESCE(published_at, fetched_at) >= ?
        ORDER BY COALESCE(published_at, fetched_at)
        """,
        (since,),
    ).fetchall()


def store_embeddings(conn: sqlite3.Connection, pairs: list[tuple[int, bytes]]) -> None:
    """Persist (article_id, embedding_blob) pairs."""
    conn.executemany(
        "UPDATE articles SET embedding = ? WHERE id = ?", [(b, i) for i, b in pairs]
    )
    conn.commit()


def unclustered_articles(conn: sqlite3.Connection, since: str) -> list[sqlite3.Row]:
    """Embedded window articles not yet assigned to a cluster."""
    return conn.execute(
        """
        SELECT a.id, a.title, a.embedding FROM articles a
        LEFT JOIN cluster_members m ON m.article_id = a.id
        WHERE m.article_id IS NULL AND a.embedding IS NOT NULL
          AND COALESCE(a.published_at, a.fetched_at) >= ?
        ORDER BY COALESCE(a.published_at, a.fetched_at)
        """,
        (since,),
    ).fetchall()


def active_clusters(conn: sqlite3.Connection, since: str) -> list[sqlite3.Row]:
    """Clusters touched within the window (candidates for attachment)."""
    return conn.execute(
        "SELECT id, centroid FROM clusters WHERE updated_at >= ?", (since,)
    ).fetchall()


def create_cluster(
    conn: sqlite3.Connection, centroid: bytes, title: str, first_article_id: int
) -> int:
    """New singleton cluster seeded by one article. Returns the cluster id."""
    now = utcnow_iso()
    cur = conn.execute(
        """
        INSERT INTO clusters (title, centroid, n_members, created_at, updated_at)
        VALUES (?, ?, 1, ?, ?)
        """,
        (title, centroid, now, now),
    )
    cluster_id = cur.lastrowid
    assert cluster_id is not None
    conn.execute(
        "INSERT INTO cluster_members (cluster_id, article_id, similarity) VALUES (?, ?, 1.0)",
        (cluster_id, first_article_id),
    )
    return cluster_id


def add_cluster_member(
    conn: sqlite3.Connection, cluster_id: int, article_id: int, similarity: float
) -> None:
    """Attach an article to an existing cluster."""
    conn.execute(
        "INSERT INTO cluster_members (cluster_id, article_id, similarity) VALUES (?, ?, ?)",
        (cluster_id, article_id, similarity),
    )


def update_cluster(
    conn: sqlite3.Connection, cluster_id: int, centroid: bytes, title: str, n_members: int
) -> None:
    """Refresh a cluster's centroid, auto title and member count."""
    conn.execute(
        """
        UPDATE clusters SET centroid = ?, title = ?, n_members = ?, updated_at = ?
        WHERE id = ?
        """,
        (centroid, title, n_members, utcnow_iso(), cluster_id),
    )


def cluster_sizes(conn: sqlite3.Connection, cluster_ids: list[int]) -> dict[int, int]:
    """n_members for the given cluster ids."""
    marks = ",".join("?" * len(cluster_ids))
    rows = conn.execute(
        f"SELECT id, n_members FROM clusters WHERE id IN ({marks})", cluster_ids
    ).fetchall()
    return {r["id"]: r["n_members"] for r in rows}


def cluster_member_count(conn: sqlite3.Connection, cluster_id: int) -> int:
    """Actual member count, including rows whose transient embedding was purged."""
    return conn.execute(
        "SELECT COUNT(*) FROM cluster_members WHERE cluster_id = ?", (cluster_id,)
    ).fetchone()[0]


def merge_cluster_into(conn: sqlite3.Connection, dead_id: int, survivor_id: int) -> None:
    """Move members of dead_id into survivor_id and drop the dead cluster.

    The dead cluster's analyses are stale by construction — dropped too; the
    survivor's analyses are also stale after its membership changes.
    """
    conn.execute(
        "UPDATE cluster_members SET cluster_id = ? WHERE cluster_id = ?",
        (survivor_id, dead_id),
    )
    conn.execute(
        "DELETE FROM analyses WHERE cluster_id IN (?, ?)", (dead_id, survivor_id)
    )
    conn.execute("DELETE FROM clusters WHERE id = ?", (dead_id,))


def cluster_top_article(conn: sqlite3.Connection, cluster_id: int) -> sqlite3.Row | None:
    """Most central member (highest attach similarity): representative link."""
    return conn.execute(
        """
        SELECT a.url, a.title, a.published_at FROM cluster_members m
        JOIN articles a ON a.id = m.article_id
        WHERE m.cluster_id = ? ORDER BY m.similarity DESC LIMIT 1
        """,
        (cluster_id,),
    ).fetchone()


def cluster_member_embeddings(conn: sqlite3.Connection, cluster_id: int) -> list[sqlite3.Row]:
    """Embeddings + titles of a cluster's members."""
    return conn.execute(
        """
        SELECT a.id, a.title, a.embedding FROM cluster_members m
        JOIN articles a ON a.id = m.article_id
        WHERE m.cluster_id = ? AND a.embedding IS NOT NULL
        """,
        (cluster_id,),
    ).fetchall()


def window_articles_with_embeddings(conn: sqlite3.Connection, since: str) -> list[sqlite3.Row]:
    """All embedded window articles (for pairwise calibration)."""
    return conn.execute(
        """
        SELECT a.id, a.title, a.source_id, a.embedding FROM articles a
        WHERE a.embedding IS NOT NULL AND COALESCE(a.published_at, a.fetched_at) >= ?
        """,
        (since,),
    ).fetchall()


def random_clusters(conn: sqlite3.Connection, n: int, min_size: int) -> list[sqlite3.Row]:
    """N random clusters having at least min_size members."""
    return conn.execute(
        "SELECT * FROM clusters WHERE n_members >= ? ORDER BY RANDOM() LIMIT ?",
        (min_size, n),
    ).fetchall()


def cluster_members_detail(conn: sqlite3.Connection, cluster_id: int) -> list[sqlite3.Row]:
    """Members of a cluster with source metadata, in publication order."""
    return conn.execute(
        """
        SELECT a.title, a.url, a.published_at, m.similarity,
               s.id AS source_id, s.name AS source_name, s.orientation, s.owner, s.editorial_style, s.paywall
        FROM cluster_members m
        JOIN articles a ON a.id = m.article_id
        JOIN sources s ON s.id = a.source_id
        WHERE m.cluster_id = ?
        ORDER BY COALESCE(a.published_at, a.fetched_at)
        """,
        (cluster_id,),
    ).fetchall()


def active_counts_by_orientation(conn: sqlite3.Connection) -> dict[str, int]:
    """Number of active sources per orientation (blindspot weighting)."""
    rows = conn.execute(
        "SELECT orientation, COUNT(*) AS n FROM sources WHERE active = 1 GROUP BY orientation"
    ).fetchall()
    return {r["orientation"]: r["n"] for r in rows}


def blindspot_inputs(conn: sqlite3.Connection, min_articles: int) -> list[sqlite3.Row]:
    """(cluster_id, source_id, orientation) for clusters big enough to score."""
    return conn.execute(
        """
        SELECT DISTINCT m.cluster_id, s.id AS source_id, s.orientation
        FROM cluster_members m
        JOIN clusters c ON c.id = m.cluster_id
        JOIN articles a ON a.id = m.article_id
        JOIN sources s ON s.id = a.source_id
        WHERE c.n_members >= ?
        """,
        (min_articles,),
    ).fetchall()


def vocab_inputs(
    conn: sqlite3.Connection, min_articles: int, since: str
) -> list[sqlite3.Row]:
    """(cluster_id, orientation, title, summary) for vocabulary contrast.

    Restricted to clusters still inside the processing window: outside it the
    summaries have been purged, so recomputing would silently degrade the
    stored result to titles-only. Older clusters keep their stored payload.
    """
    return conn.execute(
        """
        SELECT m.cluster_id, s.orientation, a.title, a.summary
        FROM cluster_members m
        JOIN clusters c ON c.id = m.cluster_id
        JOIN articles a ON a.id = m.article_id
        JOIN sources s ON s.id = a.source_id
        WHERE c.n_members >= ? AND c.updated_at >= ?
        """,
        (min_articles, since),
    ).fetchall()


def ollama_inputs(
    conn: sqlite3.Connection, min_articles: int, since: str
) -> list[sqlite3.Row]:
    """Members with ids and source names, for the qualitative LLM analysis.

    Same window restriction as vocab_inputs: summaries only exist inside it.
    """
    return conn.execute(
        """
        SELECT m.cluster_id, a.id AS article_id, a.title, a.summary,
               s.name AS source_name, s.orientation
        FROM cluster_members m
        JOIN clusters c ON c.id = m.cluster_id
        JOIN articles a ON a.id = m.article_id
        JOIN sources s ON s.id = a.source_id
        WHERE c.n_members >= ? AND c.updated_at >= ?
        """,
        (min_articles, since),
    ).fetchall()


def all_article_texts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every stored article's text fields (Dirichlet prior + IDF corpus)."""
    return conn.execute("SELECT title, summary FROM articles").fetchall()


def save_analysis(conn: sqlite3.Connection, cluster_id: int, kind: str, payload: str) -> None:
    """Insert or overwrite the analysis of one (cluster, kind)."""
    conn.execute(
        """
        INSERT INTO analyses (cluster_id, kind, payload, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(cluster_id, kind) DO UPDATE SET
            payload = excluded.payload, created_at = excluded.created_at
        """,
        (cluster_id, kind, payload, utcnow_iso()),
    )


def set_cluster_blindspot(conn: sqlite3.Connection, cluster_id: int, score: float | None) -> None:
    conn.execute("UPDATE clusters SET blindspot_score = ? WHERE id = ?", (score, cluster_id))


def set_cluster_suspect(conn: sqlite3.Connection, cluster_id: int, suspect: bool) -> None:
    conn.execute("UPDATE clusters SET suspect_merge = ? WHERE id = ?",
                 (int(suspect), cluster_id))


def cluster_is_suspect(conn: sqlite3.Connection, cluster_id: int) -> bool:
    row = conn.execute("SELECT suspect_merge FROM clusters WHERE id = ?", (cluster_id,)).fetchone()
    return bool(row and row["suspect_merge"])


def set_cluster_divergence(conn: sqlite3.Connection, cluster_id: int, score: float) -> None:
    conn.execute("UPDATE clusters SET divergence_score = ? WHERE id = ?", (score, cluster_id))


def top_blindspots(conn: sqlite3.Connection, min_abs_score: float, limit: int) -> list[sqlite3.Row]:
    """Most one-sided clusters, most extreme first."""
    return conn.execute(
        """
        SELECT id, title, n_members, blindspot_score FROM clusters
        WHERE blindspot_score IS NOT NULL AND ABS(blindspot_score) >= ?
        ORDER BY ABS(blindspot_score) DESC, n_members DESC LIMIT ?
        """,
        (min_abs_score, limit),
    ).fetchall()


def top_divergent(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    """Clusters whose two blocs use the most disjoint vocabulary."""
    return conn.execute(
        """
        SELECT c.id, c.title, c.n_members, c.divergence_score, a.payload
        FROM clusters c
        JOIN analyses a ON a.cluster_id = c.id AND a.kind = 'vocab_contrast'
        WHERE c.divergence_score IS NOT NULL
        ORDER BY c.divergence_score DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()


def cluster_source_rows(
    conn: sqlite3.Connection, since: str, min_members: int
) -> list[sqlite3.Row]:
    """One row per (cluster, source) for clusters recently updated."""
    return conn.execute(
        """
        SELECT DISTINCT c.id AS cluster_id, c.title, c.n_members, c.updated_at, c.created_at, c.suspect_merge,
               c.divergence_score, c.blindspot_score, c.category,
               MAX(COALESCE(a.published_at, a.fetched_at)) OVER (PARTITION BY c.id) AS latest_published,
               s.id AS source_id, s.name AS source_name, s.orientation,
               s.editorial_style, s.paywall
        FROM clusters c
        JOIN cluster_members m ON m.cluster_id = c.id
        JOIN articles a ON a.id = m.article_id
        JOIN sources s ON s.id = a.source_id
        WHERE c.n_members >= ? AND c.updated_at >= ?
        """,
        (min_members, since),
    ).fetchall()


def get_analyses(conn: sqlite3.Connection, cluster_id: int) -> dict[str, str]:
    """All analysis payloads of one cluster, keyed by kind."""
    rows = conn.execute(
        "SELECT kind, payload FROM analyses WHERE cluster_id = ?", (cluster_id,)
    ).fetchall()
    return {r["kind"]: r["payload"] for r in rows}


def all_sources(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every source (active or not), in spectrum order (about page)."""
    return conn.execute(
        """
        SELECT * FROM sources
        ORDER BY
            CASE orientation
                WHEN 'gauche' THEN 0 WHEN 'centre-gauche' THEN 1 WHEN 'centre' THEN 2
                WHEN 'centre-droit' THEN 3 WHEN 'droite' THEN 4
            END,
            name
        """
    ).fetchall()


def feed_health(conn: sqlite3.Connection) -> dict:
    """Latest fetch status per feed: {'ok': n, 'total': n, 'last': iso|None}."""
    rows = conn.execute(
        """
        SELECT f.status FROM fetch_log f
        JOIN (SELECT feed_url, MAX(id) AS mid FROM fetch_log GROUP BY feed_url) l
          ON l.mid = f.id
        """
    ).fetchall()
    last = conn.execute("SELECT MAX(fetched_at) AS m FROM fetch_log").fetchone()["m"]
    return {
        "ok": sum(1 for r in rows if r["status"] == "ok"),
        "total": len(rows),
        "last": last,
    }


def source_profiles(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Per-source aggregates for the static source pages (30-day scope)."""
    return conn.execute(
        """
        SELECT s.id, s.name, s.orientation, s.editorial_style, s.paywall, s.owner,
               COUNT(DISTINCT a.id) AS n_articles,
               COUNT(DISTINCT m.cluster_id) AS n_clusters,
               COUNT(DISTINCT CASE WHEN multi.orientations >= 2 THEN m.cluster_id END)
                   AS n_multi_clusters
        FROM sources s
        LEFT JOIN articles a ON a.source_id = s.id
        LEFT JOIN cluster_members m ON m.article_id = a.id
        LEFT JOIN (
            SELECT m2.cluster_id, COUNT(DISTINCT s2.orientation) AS orientations
            FROM cluster_members m2
            JOIN articles a2 ON a2.id = m2.article_id
            JOIN sources s2 ON s2.id = a2.source_id
            GROUP BY m2.cluster_id
        ) multi ON multi.cluster_id = m.cluster_id
        WHERE s.active = 1
        GROUP BY s.id
        ORDER BY s.name
        """
    ).fetchall()


def public_stats(conn: sqlite3.Connection) -> dict:
    """Aggregates for the public stats page — transparency is credibility."""
    last_fetch = conn.execute("SELECT MAX(fetched_at) AS m FROM fetch_log").fetchone()["m"]
    feeds = conn.execute(
        """
        SELECT f.source_id, s.name, f.feed_url, f.status, f.http_code
        FROM fetch_log f
        JOIN (SELECT feed_url, MAX(id) AS mid FROM fetch_log GROUP BY feed_url) l
          ON l.mid = f.id
        JOIN sources s ON s.id = f.source_id
        ORDER BY (f.status = 'ok'), s.name
        """
    ).fetchall()
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
    volumes = conn.execute(
        """
        SELECT s.name, s.orientation, COUNT(a.id) AS n
        FROM sources s
        LEFT JOIN articles a ON a.source_id = s.id
          AND COALESCE(a.published_at, a.fetched_at) >= ?
        WHERE s.active = 1
        GROUP BY s.id ORDER BY n DESC
        """,
        (week_ago,),
    ).fetchall()
    agg = conn.execute(
        """
        SELECT COUNT(*) AS n_clusters,
               AVG(divergence_score) AS avg_divergence,
               SUM(CASE WHEN n_members >= 2 THEN 1 ELSE 0 END) AS multi_article,
               SUM(suspect_merge) AS n_suspect
        FROM clusters
        """
    ).fetchone()
    return {
        "last_fetch": last_fetch,
        "feeds": feeds,
        "volumes": volumes,
        "n_clusters": agg["n_clusters"],
        "avg_divergence": agg["avg_divergence"],
        "multi_article": agg["multi_article"],
        "n_suspect": agg["n_suspect"] or 0,
    }


def source_stats(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Article count per active source, for the CLI report."""
    return conn.execute(
        """
        SELECT s.id, s.name, s.orientation, COUNT(a.id) AS n_articles,
               MAX(a.published_at) AS latest
        FROM sources s
        LEFT JOIN articles a ON a.source_id = s.id
        WHERE s.active = 1
        GROUP BY s.id
        ORDER BY
            CASE s.orientation
                WHEN 'gauche' THEN 0 WHEN 'centre-gauche' THEN 1 WHEN 'centre' THEN 2
                WHEN 'centre-droit' THEN 3 WHEN 'droite' THEN 4
            END,
            n_articles DESC
        """
    ).fetchall()


def last_run_fetch_log(conn: sqlite3.Connection, since: str) -> list[sqlite3.Row]:
    """Fetch-log rows recorded since the given ISO timestamp."""
    return conn.execute(
        "SELECT * FROM fetch_log WHERE fetched_at >= ? ORDER BY source_id", (since,)
    ).fetchall()

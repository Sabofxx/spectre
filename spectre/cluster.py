"""Event clustering: embed articles, group them by news event.

Greedy incremental clustering over a 72h sliding window: each new article
joins the closest active cluster if cosine similarity clears a threshold,
otherwise it starts its own cluster. Embeddings are L2-normalized at encode
time, so cosine similarity is a plain dot product.
"""

from __future__ import annotations

import logging
import random
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import numpy as np

from . import db

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)

MODEL_NAME = "intfloat/multilingual-e5-small"
# E5 models expect a task prefix; for symmetric similarity both sides get it.
E5_PREFIX = "query: "
# Calibrated on real data (2026-07-11, 894 articles). E5 similarities are
# compressed upward (corpus median 0.811, p99 0.881): 0.88 produced 110-item
# megaclusters, 0.90 still glued a 69-item canicule blob, 0.92 gave clean
# single-event clusters at the cost of splitting a few true pairs sitting
# at 0.90-0.92. Precision over recall, as always for framing analysis.
DEFAULT_THRESHOLD = 0.92
MAX_CONSOLIDATED_MEMBERS = 60
MAX_CONSOLIDATED_ARTICLES_PER_SOURCE = 8
_TITLE_WORD_RE = re.compile(r"[a-zà-öø-ÿœ0-9]+")
_TITLE_STOPWORDS = frozenset("""
actualité actualités annonce après avant avec avoir cette comme dans direct
elle elles entre être fait faire france grand grande grands grandes hommes
info infos leur leurs mais même moins monde nouveau nouveaux nouvelle nouvelles
plus pour pourquoi quand quel quelle quels quelles selon sera sont sous toute
toutes tous très vers vidéo voici votre vous
""".split())


def load_model() -> "SentenceTransformer":
    """Load the embedding model (imported lazily: torch is heavy)."""
    from sentence_transformers import SentenceTransformer

    logger.info("loading model %s (CPU)", MODEL_NAME)
    return SentenceTransformer(MODEL_NAME, device="cpu")


def window_start(hours: int = db.CLUSTERING_WINDOW_HOURS) -> str:
    """ISO timestamp of the start of the clustering window."""
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")


def embedding_text(title: str, summary: str | None) -> str:
    """Text fed to the encoder for one article."""
    return f"{title} {summary}" if summary else title


def _to_vec(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


def _title_tokens(title: str) -> set[str]:
    """Strong-ish lexical tokens used as a consolidation sanity check."""
    return {
        w for w in _TITLE_WORD_RE.findall(title.lower())
        if len(w) >= 4 and w not in _TITLE_STOPWORDS and not w.isdigit()
    }


def embed_pending(conn: sqlite3.Connection, model: "SentenceTransformer") -> int:
    """Embed window articles that have no embedding yet. Returns the count."""
    rows = db.articles_to_embed(conn, window_start())
    if not rows:
        return 0
    texts = [E5_PREFIX + embedding_text(r["title"], r["summary"]) for r in rows]
    vecs = model.encode(
        texts, normalize_embeddings=True, batch_size=64, show_progress_bar=False
    )
    db.store_embeddings(
        conn, [(r["id"], np.asarray(v, dtype=np.float32).tobytes()) for r, v in zip(rows, vecs)]
    )
    logger.info("embedded %d articles", len(rows))
    return len(rows)


def _refresh_cluster(conn: sqlite3.Connection, cluster_id: int) -> np.ndarray | None:
    """Recompute centroid, title and member count after membership changed.

    Returns the new (normalized) centroid, or None when every member's
    embedding was already purged (old cluster): counts are refreshed, the
    stored centroid is kept as-is.
    """
    members = db.cluster_member_embeddings(conn, cluster_id)
    if not members:
        n = conn.execute(
            "SELECT COUNT(*) FROM cluster_members WHERE cluster_id = ?", (cluster_id,)
        ).fetchone()[0]
        conn.execute(
            "UPDATE clusters SET n_members = ?, updated_at = ? WHERE id = ?",
            (n, db.utcnow_iso(), cluster_id),
        )
        return None
    vecs = np.stack([_to_vec(m["embedding"]) for m in members])
    centroid = vecs.mean(axis=0)
    centroid /= np.linalg.norm(centroid)
    # Cluster title = title of the most central member.
    title = members[int(np.argmax(vecs @ centroid))]["title"]
    db.update_cluster(
        conn,
        cluster_id,
        centroid.astype(np.float32).tobytes(),
        title,
        db.cluster_member_count(conn, cluster_id),
    )
    return centroid


def cluster_pending(
    conn: sqlite3.Connection, threshold: float = DEFAULT_THRESHOLD
) -> dict[str, int]:
    """Attach every unclustered window article to a cluster (greedy).

    Articles are processed in publication order so behaviour is stable
    across repeated runs (idempotent given the same inputs).
    """
    since = window_start()
    active = db.active_clusters(conn, since)
    ids: list[int] = [c["id"] for c in active]
    cents: list[np.ndarray] = [_to_vec(c["centroid"]) for c in active]
    # Member-embedding matrices per cluster, loaded lazily and kept in sync.
    member_mats: dict[int, np.ndarray] = {}

    def members_matrix(cluster_id: int) -> np.ndarray | None:
        """Member embeddings, or None when the purge already NULLed them all
        (an old cluster can outlive its articles' 72h embedding window)."""
        if cluster_id in member_mats:
            return member_mats[cluster_id]
        rows = db.cluster_member_embeddings(conn, cluster_id)
        mat = np.stack([_to_vec(r["embedding"]) for r in rows]) if rows else None
        member_mats[cluster_id] = mat
        return mat

    stats = {"attached": 0, "created": 0}
    for art in db.unclustered_articles(conn, since):
        emb = _to_vec(art["embedding"])
        best_i, best_sim = -1, -1.0
        if cents:
            sims = np.stack(cents) @ emb
            best_i = int(np.argmax(sims))
            best_sim = float(sims[best_i])
        # Anti-chaining rule: joining requires clearing the threshold against
        # BOTH the centroid and the nearest existing member. A drifted
        # centroid alone can no longer recruit an article that no actual
        # member resembles. Only the best centroid candidate is considered.
        attach = False
        if best_i >= 0 and best_sim >= threshold:
            mat = members_matrix(ids[best_i])
            attach = mat is not None and float(np.max(mat @ emb)) >= threshold
        if attach:
            cid = ids[best_i]
            db.add_cluster_member(conn, cid, art["id"], best_sim)
            existing = member_mats.get(cid)
            member_mats[cid] = (
                np.vstack([existing, emb]) if existing is not None else emb[np.newaxis, :]
            )
            new_centroid = _refresh_cluster(conn, cid)
            if new_centroid is not None:
                cents[best_i] = new_centroid
            stats["attached"] += 1
        else:
            cluster_id = db.create_cluster(
                conn, art["embedding"], art["title"], art["id"]
            )
            ids.append(cluster_id)
            cents.append(emb)
            member_mats[cluster_id] = emb[np.newaxis, :]
            stats["created"] += 1
    conn.commit()
    logger.info("clustering done: %s (threshold=%.2f)", stats, threshold)
    return stats


def _can_consolidate_pair(
    conn: sqlite3.Connection,
    left_id: int,
    right_id: int,
    combined_size: int,
    title_tokens: dict[int, set[str]],
) -> bool:
    """Cheap precision gates before merging two already-similar centroids."""
    if combined_size > MAX_CONSOLIDATED_MEMBERS:
        return False
    if not (title_tokens[left_id] & title_tokens[right_id]):
        return False
    rows = conn.execute(
        """
        SELECT a.source_id, COUNT(*) AS n
        FROM cluster_members m
        JOIN articles a ON a.id = m.article_id
        WHERE m.cluster_id IN (?, ?)
        GROUP BY a.source_id
        """,
        (left_id, right_id),
    ).fetchall()
    return all(r["n"] <= MAX_CONSOLIDATED_ARTICLES_PER_SOURCE for r in rows)


def consolidate(conn: sqlite3.Connection, threshold: float = DEFAULT_THRESHOLD) -> int:
    """Merge active cluster pairs whose centroids converged above the threshold.

    The greedy pass splits an event when its early articles arrive under two
    angles (observed: same event sitting at sim 0.90-0.92). Once centroids
    have absorbed more members, true duplicates converge.

    This pass stays deliberately conservative: no transitive union-find
    components, a size cap, a per-source cap, and a minimal title-token overlap.
    E5 similarities are compressed upward; without these gates, generic news
    centroids can chain into megaclusters. Returns the number of clusters
    absorbed.
    """
    active = db.active_clusters(conn, window_start())
    if len(active) < 2:
        return 0
    ids = [c["id"] for c in active]
    mat = np.stack([_to_vec(c["centroid"]) for c in active])
    sims = mat @ mat.T

    merged = 0
    sizes = db.cluster_sizes(conn, ids)
    title_tokens = {
        c["id"]: _title_tokens(c["title"] or "")
        for c in conn.execute("SELECT id, title FROM clusters").fetchall()
    }
    alive = set(ids)
    used: set[int] = set()
    pairs_i, pairs_j = np.where(np.triu(sims, k=1) >= threshold)
    pairs = sorted(
        (
            (float(sims[i, j]), ids[i], ids[j])
            for i, j in zip(pairs_i.tolist(), pairs_j.tolist())
        ),
        reverse=True,
    )
    for _sim, left_id, right_id in pairs:
        if left_id not in alive or right_id not in alive:
            continue
        if left_id in used or right_id in used:
            continue
        combined_size = sizes[left_id] + sizes[right_id]
        if not _can_consolidate_pair(conn, left_id, right_id, combined_size, title_tokens):
            continue
        survivor, dead = (
            (left_id, right_id) if sizes[left_id] >= sizes[right_id]
            else (right_id, left_id)
        )
        db.merge_cluster_into(conn, dead, survivor)
        alive.remove(dead)
        used.update({survivor, dead})
        sizes[survivor] = combined_size
        title_tokens[survivor] |= title_tokens[dead]
        _refresh_cluster(conn, survivor)
        merged += 1
    conn.commit()
    if merged:
        logger.info("consolidate: %d clusters absorbed", merged)
    return merged


def run(conn: sqlite3.Connection, threshold: float = DEFAULT_THRESHOLD) -> dict[str, int]:
    """Embed, cluster, consolidate, then categorize (model is loaded here)."""
    from .categorize import categorize_clusters

    model = load_model()
    n_embedded = embed_pending(conn, model)
    stats = cluster_pending(conn, threshold)
    merged = consolidate(conn, threshold)
    categorize_clusters(conn, model=model)
    return {"embedded": n_embedded, **stats, "merged": merged}


# ---------------------------------------------------------------------------
# Manual evaluation helpers (threshold calibration)
# ---------------------------------------------------------------------------

def sample_clusters(conn: sqlite3.Connection, n: int = 10, min_size: int = 2) -> list[dict]:
    """N random clusters (with >= min_size members) and their members."""
    clusters = db.random_clusters(conn, n, min_size)
    out = []
    for c in clusters:
        members = db.cluster_members_detail(conn, c["id"])
        out.append({"cluster": c, "members": members})
    return out


def gray_zone_pairs(
    conn: sqlite3.Connection,
    lo: float = 0.45,
    hi: float = 0.65,
    n: int = 15,
    seed: int | None = None,
) -> list[tuple[float, sqlite3.Row, sqlite3.Row]]:
    """Random cross-source article pairs whose similarity falls in [lo, hi].

    This is the gray zone that actually decides the threshold — obvious
    matches and obvious non-matches teach nothing.
    """
    rows = db.window_articles_with_embeddings(conn, window_start())
    if len(rows) < 2:
        return []
    mat = np.stack([_to_vec(r["embedding"]) for r in rows])
    sims = mat @ mat.T
    idx_i, idx_j = np.triu_indices(len(rows), k=1)
    candidates = [
        (float(sims[i, j]), rows[i], rows[j])
        for i, j in zip(idx_i.tolist(), idx_j.tolist())
        if lo <= sims[i, j] <= hi and rows[i]["source_id"] != rows[j]["source_id"]
    ]
    rng = random.Random(seed)
    rng.shuffle(candidates)
    return sorted(candidates[:n], key=lambda t: -t[0])

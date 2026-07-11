"""Event clustering: embed articles, group them by news event.

Greedy incremental clustering over a 72h sliding window: each new article
joins the closest active cluster if cosine similarity clears a threshold,
otherwise it starts its own cluster. Embeddings are L2-normalized at encode
time, so cosine similarity is a plain dot product.
"""

from __future__ import annotations

import logging
import random
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


def _refresh_cluster(conn: sqlite3.Connection, cluster_id: int) -> np.ndarray:
    """Recompute centroid, title and member count after membership changed.

    Returns the new (normalized) centroid.
    """
    members = db.cluster_member_embeddings(conn, cluster_id)
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

    def members_matrix(cluster_id: int) -> np.ndarray:
        mat = member_mats.get(cluster_id)
        if mat is None:
            rows = db.cluster_member_embeddings(conn, cluster_id)
            mat = np.stack([_to_vec(r["embedding"]) for r in rows])
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
            nearest = float(np.max(members_matrix(ids[best_i]) @ emb))
            attach = nearest >= threshold
        if attach:
            cid = ids[best_i]
            db.add_cluster_member(conn, cid, art["id"], best_sim)
            member_mats[cid] = np.vstack([member_mats[cid], emb])
            cents[best_i] = _refresh_cluster(conn, cid)
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


def consolidate(conn: sqlite3.Connection, threshold: float = DEFAULT_THRESHOLD) -> int:
    """Merge active clusters whose centroids converged above the threshold.

    The greedy pass splits an event when its early articles arrive under two
    angles (observed: same event sitting at sim 0.90-0.92). Once centroids
    have absorbed more members, true duplicates converge; merging them here
    recovers that recall without lowering the attach threshold. Union-find
    over the centroid similarity graph; the largest cluster of each group
    survives. Returns the number of clusters absorbed.
    """
    active = db.active_clusters(conn, window_start())
    if len(active) < 2:
        return 0
    ids = [c["id"] for c in active]
    mat = np.stack([_to_vec(c["centroid"]) for c in active])
    sims = mat @ mat.T

    parent = list(range(len(ids)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    pairs_i, pairs_j = np.where(np.triu(sims, k=1) >= threshold)
    for i, j in zip(pairs_i.tolist(), pairs_j.tolist()):
        parent[find(i)] = find(j)

    groups: dict[int, list[int]] = {}
    for i in range(len(ids)):
        groups.setdefault(find(i), []).append(i)

    merged = 0
    sizes = db.cluster_sizes(conn, ids)
    for members in groups.values():
        if len(members) < 2:
            continue
        ordered = sorted(members, key=lambda i: -sizes[ids[i]])
        survivor = ids[ordered[0]]
        for i in ordered[1:]:
            db.merge_cluster_into(conn, ids[i], survivor)
            merged += 1
        _refresh_cluster(conn, survivor)
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

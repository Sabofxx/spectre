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

MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"
# Calibrated on real data (2026-07-11, 790 articles): 0.55 produced a 48-item
# drift megacluster, 0.65 still glued distinct same-domain events (three
# unrelated fires at sim 0.71-0.72). 0.70 trades a little recall (big events
# may split in two) for the precision that framing analysis needs.
DEFAULT_THRESHOLD = 0.70


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
    texts = [embedding_text(r["title"], r["summary"]) for r in rows]
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
    db.update_cluster(conn, cluster_id, centroid.astype(np.float32).tobytes(), title, len(members))
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


def run(conn: sqlite3.Connection, threshold: float = DEFAULT_THRESHOLD) -> dict[str, int]:
    """Embed then cluster everything pending in the window."""
    model = load_model()
    n_embedded = embed_pending(conn, model)
    stats = cluster_pending(conn, threshold)
    return {"embedded": n_embedded, **stats}


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

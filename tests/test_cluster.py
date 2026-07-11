"""Clustering logic with hand-built (mocked) embeddings — no model needed."""

import numpy as np

from spectre import db as dbmod
from spectre.cluster import cluster_pending

from conftest import make_article


def put_article(conn, vec: list[float], hours_ago: float, title: str = "t") -> int:
    """Insert an article with a unit-normalized embedding; returns its id."""
    art = make_article(title=title, hours_ago=hours_ago)
    dbmod.insert_article(conn, art)
    article_id = conn.execute(
        "SELECT id FROM articles WHERE url = ?", (art.url,)
    ).fetchone()[0]
    v = np.asarray(vec, dtype=np.float32)
    v /= np.linalg.norm(v)
    dbmod.store_embeddings(conn, [(article_id, v.tobytes())])
    return article_id


def cluster_of(conn, article_id: int) -> int:
    return conn.execute(
        "SELECT cluster_id FROM cluster_members WHERE article_id = ?", (article_id,)
    ).fetchone()[0]


def test_similar_articles_group_distant_starts_new(conn):
    a = put_article(conn, [1.0, 0.0, 0.0], hours_ago=3)
    b = put_article(conn, [0.98, 0.2, 0.0], hours_ago=2)  # sim ~0.98
    c = put_article(conn, [0.0, 0.0, 1.0], hours_ago=1)  # orthogonal
    stats = cluster_pending(conn, threshold=0.7)
    assert stats == {"attached": 1, "created": 2}
    assert cluster_of(conn, a) == cluster_of(conn, b) != cluster_of(conn, c)


def test_anti_chaining_rejects_centroid_only_match(conn):
    """THE rule that protects the data: C clears the (drifted) centroid but no
    actual member, so it must NOT join.

    Geometry (unit vectors, threshold 0.7):
      M1, M2 at ±alpha around the x-axis with cos(2*alpha) = 0.72
        -> sim(M1, M2) = 0.72 >= 0.7: they legitimately cluster;
           centroid = (1, 0, 0), sim(Mi, centroid) ~= 0.927.
      C = 0.75 * centroid + 0.661 * z_axis
        -> sim(C, centroid) = 0.75  >= 0.7  (centroid check passes)
           sim(C, Mi) = 0.75 * 0.927 ~= 0.695 < 0.7 (nearest-member check fails)
    """
    cos_a = float(np.sqrt((1 + 0.72) / 2))
    sin_a = float(np.sqrt(1 - cos_a**2))
    m1 = put_article(conn, [cos_a, sin_a, 0.0], hours_ago=3)
    m2 = put_article(conn, [cos_a, -sin_a, 0.0], hours_ago=2)
    c = put_article(conn, [0.75, 0.0, float(np.sqrt(1 - 0.75**2))], hours_ago=1)

    stats = cluster_pending(conn, threshold=0.7)

    assert stats == {"attached": 1, "created": 2}
    assert cluster_of(conn, m1) == cluster_of(conn, m2)
    assert cluster_of(conn, c) != cluster_of(conn, m1)
    # C's cluster is a singleton: it was rejected, not re-routed.
    n = conn.execute(
        "SELECT n_members FROM clusters WHERE id = ?", (cluster_of(conn, c),)
    ).fetchone()[0]
    assert n == 1


def test_articles_outside_window_are_ignored(conn):
    old = put_article(conn, [1.0, 0.0, 0.0], hours_ago=100)  # outside 72h
    recent = put_article(conn, [1.0, 0.0, 0.0], hours_ago=1)
    stats = cluster_pending(conn, threshold=0.7)
    assert stats == {"attached": 0, "created": 1}
    assert conn.execute(
        "SELECT COUNT(*) FROM cluster_members WHERE article_id = ?", (old,)
    ).fetchone()[0] == 0
    assert cluster_of(conn, recent) is not None


def test_rerun_is_idempotent(conn):
    put_article(conn, [1.0, 0.0, 0.0], hours_ago=2)
    put_article(conn, [0.99, 0.1, 0.0], hours_ago=1)
    first = cluster_pending(conn, threshold=0.7)
    second = cluster_pending(conn, threshold=0.7)
    assert first["created"] == 1
    assert second == {"attached": 0, "created": 0}

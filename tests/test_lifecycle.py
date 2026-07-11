"""Purge lifecycle: summaries and embeddings are transient working data."""

import numpy as np

from spectre import db as dbmod

from conftest import make_article


def insert_with_payloads(conn, hours_ago: float) -> int:
    art = make_article(summary="Un chapô de presse protégé par les droits voisins.",
                       hours_ago=hours_ago)
    dbmod.insert_article(conn, art)
    article_id = conn.execute(
        "SELECT id FROM articles WHERE url = ?", (art.url,)
    ).fetchone()[0]
    emb = np.ones(4, dtype=np.float32) / 2.0
    dbmod.store_embeddings(conn, [(article_id, emb.tobytes())])
    return article_id


def fetch(conn, article_id: int):
    return conn.execute(
        "SELECT summary, embedding FROM articles WHERE id = ?", (article_id,)
    ).fetchone()


def test_summary_and_embedding_nulled_after_72h(conn):
    old = insert_with_payloads(conn, hours_ago=80)  # outside the 72h window
    recent = insert_with_payloads(conn, hours_ago=10)

    counts = dbmod.purge(conn)

    assert counts["summaries_nulled"] == 1
    assert counts["embeddings_nulled"] == 1
    old_row, recent_row = fetch(conn, old), fetch(conn, recent)
    assert old_row["summary"] is None and old_row["embedding"] is None
    # The article row itself survives (it is < 30 days old): title + URL stay.
    assert old_row is not None
    assert recent_row["summary"] is not None and recent_row["embedding"] is not None


def test_old_articles_fully_deleted_after_30_days(conn):
    ancient = insert_with_payloads(conn, hours_ago=31 * 24)
    recent = insert_with_payloads(conn, hours_ago=1)

    counts = dbmod.purge(conn)

    assert counts["articles_deleted"] == 1
    remaining = [r[0] for r in conn.execute("SELECT id FROM articles").fetchall()]
    assert remaining == [recent]
    assert ancient not in remaining


def test_purge_is_idempotent(conn):
    insert_with_payloads(conn, hours_ago=80)
    dbmod.purge(conn)
    second = dbmod.purge(conn)
    assert second["summaries_nulled"] == 0
    assert second["embeddings_nulled"] == 0
    assert second["articles_deleted"] == 0

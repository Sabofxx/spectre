"""Weekly archive snapshots."""

import json

from spectre import db as dbmod
from spectre.archive import load_snapshots, write_snapshot

from conftest import make_article


def test_write_snapshot_stores_metrics_links_and_never_summaries(conn, tmp_path):
    secret = "Ce chapô RSS ne doit pas entrer dans les archives."
    article_ids = []
    for source_id in ("d1", "d2", "d3", "cd1", "cd2"):
        art = make_article(
            source_id=source_id,
            title=f"Titre {source_id}",
            summary=secret,
            hours_ago=2,
        )
        dbmod.insert_article(conn, art)
        article_ids.append(
            conn.execute("SELECT id FROM articles WHERE url = ?", (art.url,)).fetchone()[0]
        )
    cluster_id = dbmod.create_cluster(conn, b"\x00" * 8, "Titre archive", article_ids[0])
    for aid in article_ids[1:]:
        dbmod.add_cluster_member(conn, cluster_id, aid, 0.9)
    dbmod.update_cluster(conn, cluster_id, b"\x00" * 8, "Titre archive", len(article_ids))
    dbmod.set_cluster_blindspot(conn, cluster_id, 1.0)
    conn.commit()

    path = write_snapshot(conn, archive_dir=tmp_path)
    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw)

    assert secret not in raw
    assert data["clusters"][0]["title"] == "Titre archive"
    assert data["clusters"][0]["url"].startswith("https://example.org/")
    assert data["clusters"][0]["counts"] == {"left": 0, "centre": 0, "right": 5}
    assert data["clusters"][0]["blindspot_for"] == "gauche"


def test_load_snapshots_returns_most_recent_week_first(tmp_path):
    (tmp_path / "2026-W27.json").write_text('{"week": "2026-W27"}', encoding="utf-8")
    (tmp_path / "2026-W28.json").write_text('{"week": "2026-W28"}', encoding="utf-8")

    assert [s["week"] for s in load_snapshots(tmp_path)] == ["2026-W28", "2026-W27"]

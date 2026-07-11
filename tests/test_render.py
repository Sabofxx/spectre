"""Rendering: the droits-voisins guard, as a pytest and not only a CI step."""

from spectre import db as dbmod
from spectre.render import build_site, find_leaks

from conftest import make_article

SECRET = "Ce chapô confidentiel ne doit jamais apparaître dans le HTML public."


def seed(conn) -> None:
    for source_id in ("g1", "d1"):
        art = make_article(source_id=source_id, title=f"Titre {source_id}",
                           summary=SECRET, hours_ago=2)
        dbmod.insert_article(conn, art)
    rows = conn.execute("SELECT id, title FROM articles").fetchall()
    cluster_id = dbmod.create_cluster(conn, b"\x00" * 8, rows[0]["title"], rows[0]["id"])
    dbmod.add_cluster_member(conn, cluster_id, rows[1]["id"], 0.9)
    dbmod.update_cluster(conn, cluster_id, b"\x00" * 8, rows[0]["title"], 2)
    conn.commit()


def test_build_site_never_emits_summaries(conn, tmp_path):
    seed(conn)
    stats = build_site(conn, tmp_path)
    assert stats["feed"] == 1
    html = " ".join(p.read_text() for p in tmp_path.rglob("*.html"))
    assert "Titre g1" in html  # titles are public
    assert SECRET not in html  # summaries never
    assert find_leaks(conn, tmp_path) == []


def test_find_leaks_catches_a_leak(conn, tmp_path):
    seed(conn)
    build_site(conn, tmp_path)
    (tmp_path / "oops.html").write_text(f"<p>{SECRET}</p>", encoding="utf-8")
    leaks = find_leaks(conn, tmp_path)
    assert len(leaks) > 0
    assert SECRET in leaks

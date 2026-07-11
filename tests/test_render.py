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


def test_ollama_section_absent_without_payload(conn, tmp_path):
    seed(conn)
    build_site(conn, tmp_path)
    html = next((tmp_path / "cluster").glob("*.html")).read_text()
    assert "Analyse qualitative" not in html


def test_ollama_section_rendered_with_ai_label_and_autoescape(conn, tmp_path):
    import json

    seed(conn)
    cluster_id = conn.execute("SELECT id FROM clusters").fetchone()[0]
    hostile = {
        "event_summary": "Résumé <script>alert('xss')</script> neutre.",
        "framing": {"gauche": "angle & <b>gras</b>", "centre": None, "droite": None},
        "omissions": None,
        "model": "qwen2.5:7b-instruct",
        "article_ids": [1, 2],
    }
    dbmod.save_analysis(conn, cluster_id, "ollama", json.dumps(hostile, ensure_ascii=False))
    conn.commit()

    build_site(conn, tmp_path)
    html = (tmp_path / "cluster" / f"{cluster_id}.html").read_text()

    assert "Analyse qualitative" in html
    assert "générée par un modèle de langage local" in html  # mandatory AI label
    assert "<script>alert" not in html  # autoescape active, no |safe
    assert "&lt;script&gt;alert" in html
    assert "&amp; &lt;b&gt;gras&lt;/b&gt;" in html


def test_find_leaks_catches_a_leak(conn, tmp_path):
    seed(conn)
    build_site(conn, tmp_path)
    (tmp_path / "oops.html").write_text(f"<p>{SECRET}</p>", encoding="utf-8")
    leaks = find_leaks(conn, tmp_path)
    assert len(leaks) > 0
    assert SECRET in leaks

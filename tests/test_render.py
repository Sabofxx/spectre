"""Rendering: the droits-voisins guard, as a pytest and not only a CI step."""

import xml.etree.ElementTree as ET

from spectre import db as dbmod
from spectre.render import build_cards, build_site, find_leaks

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
        "model": "qwen3:4b",
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


def test_first_publisher_ignores_null_dates(conn, tmp_path):
    """Le Parisien has no published_at: it must never be crowned first."""
    arts = []
    for i, (src, pub) in enumerate([("d1", "2026-07-10T08:00:00+00:00"),
                                    ("g1", "2026-07-10T06:00:00+00:00"),
                                    ("c1", None)]):
        art = make_article(source_id=src, title=f"T{i}", hours_ago=2)
        dbmod.insert_article(conn, art)
        row_id = conn.execute("SELECT id FROM articles WHERE url = ?", (art.url,)).fetchone()[0]
        conn.execute("UPDATE articles SET published_at = ? WHERE id = ?", (pub, row_id))
        arts.append(row_id)
    cid = dbmod.create_cluster(conn, b"\x00" * 8, "T0", arts[0])
    for aid in arts[1:]:
        dbmod.add_cluster_member(conn, cid, aid, 0.9)
    dbmod.update_cluster(conn, cid, b"\x00" * 8, "T0", 3)
    conn.commit()

    build_site(conn, tmp_path)
    html = (tmp_path / "cluster" / f"{cid}.html").read_text()
    assert "Premier à publier" in html
    assert "<strong>Gauche 1</strong>" in html  # 06:00 wins; NULL (c1) excluded


def test_slugify():
    from spectre.render import slugify

    assert slugify("économie") == "economie"
    assert slugify("société") == "societe"
    assert slugify("faits-divers") == "faits-divers"
    assert slugify("sciences-tech") == "sciences-tech"


def test_category_pages_and_nav(conn, tmp_path):
    seed(conn)
    cluster_id = conn.execute("SELECT id FROM clusters").fetchone()[0]
    conn.execute("UPDATE clusters SET category = 'économie' WHERE id = ?", (cluster_id,))
    conn.commit()

    build_site(conn, tmp_path)

    cat_page = tmp_path / "categorie" / "economie.html"
    assert cat_page.exists()
    assert "Titre g1" in cat_page.read_text()
    index = (tmp_path / "index.html").read_text()
    assert 'href="categorie/economie.html"' in index  # filter row + badge link


def test_blindspot_label_gated_on_small_clusters(conn, tmp_path):
    """3 friendly outlets picking up a wire story is not an angle mort."""
    from spectre.render import build_cards

    ids = []
    for source_id in ("d1", "d2", "d3"):
        art = make_article(source_id=source_id, title=f"Titre {source_id}", hours_ago=2)
        dbmod.insert_article(conn, art)
        ids.append(conn.execute("SELECT id FROM articles WHERE url = ?", (art.url,)).fetchone()[0])
    cluster_id = dbmod.create_cluster(conn, b"\x00" * 8, "Petit cluster", ids[0])
    for aid in ids[1:]:
        dbmod.add_cluster_member(conn, cluster_id, aid, 0.9)
    dbmod.update_cluster(conn, cluster_id, b"\x00" * 8, "Petit cluster", 3)
    dbmod.set_cluster_blindspot(conn, cluster_id, 1.0)
    conn.commit()

    cards = build_cards(conn, "2000-01-01T00:00:00", 2)
    assert cards[0]["blindspot_score"] == 1.0  # raw score intact
    assert cards[0]["blindspot_for"] is None  # label withheld


def test_blindspots_rss_escapes_xml_text_and_links(conn, tmp_path):
    articles = []
    for source_id in ("d1", "d2", "d3", "cd1", "cd2"):
        art = make_article(
            source_id=source_id,
            title="A & B < C",
            url=f"https://example.org/{source_id}?x=1&y=2",
        )
        dbmod.insert_article(conn, art)
        articles.append(
            conn.execute("SELECT id, title FROM articles WHERE url = ?", (art.url,)).fetchone()
        )

    cluster_id = dbmod.create_cluster(conn, b"\x00" * 8, articles[0]["title"], articles[0]["id"])
    for row in articles[1:]:
        dbmod.add_cluster_member(conn, cluster_id, row["id"], 0.9)
    dbmod.update_cluster(conn, cluster_id, b"\x00" * 8, articles[0]["title"], 5)
    dbmod.set_cluster_blindspot(conn, cluster_id, 1.0)
    conn.commit()

    build_site(conn, tmp_path)

    rss_path = tmp_path / "blindspots.xml"
    root = ET.parse(rss_path).getroot()
    item = root.find("./channel/item")
    assert item is not None
    assert item.findtext("title") == "[angle mort de la gauche] A & B < C"
    assert item.findtext("link") == "https://example.org/d1?x=1&y=2"


def test_feed_cards_sort_by_distinct_sources_before_article_count(conn):
    many_articles_one_source = []
    for i in range(5):
        art = make_article(source_id="g1", title=f"Un seul média {i}", hours_ago=1)
        dbmod.insert_article(conn, art)
        many_articles_one_source.append(
            conn.execute("SELECT id FROM articles WHERE url = ?", (art.url,)).fetchone()[0]
        )
    broad_coverage = []
    for source_id in ("g1", "d1"):
        art = make_article(source_id=source_id, title=f"Deux médias {source_id}", hours_ago=1)
        dbmod.insert_article(conn, art)
        broad_coverage.append(
            conn.execute("SELECT id FROM articles WHERE url = ?", (art.url,)).fetchone()[0]
        )

    c1 = dbmod.create_cluster(conn, b"\x00" * 8, "Un seul média", many_articles_one_source[0])
    for aid in many_articles_one_source[1:]:
        dbmod.add_cluster_member(conn, c1, aid, 0.9)
    dbmod.update_cluster(conn, c1, b"\x00" * 8, "Un seul média", len(many_articles_one_source))
    c2 = dbmod.create_cluster(conn, b"\x00" * 8, "Deux médias", broad_coverage[0])
    dbmod.add_cluster_member(conn, c2, broad_coverage[1], 0.9)
    dbmod.update_cluster(conn, c2, b"\x00" * 8, "Deux médias", len(broad_coverage))
    conn.commit()

    cards = build_cards(conn, "2000-01-01T00:00:00+00:00", min_members=2)

    assert [c["title"] for c in cards[:2]] == ["Deux médias", "Un seul média"]


def test_build_site_renders_archive_pages(conn, tmp_path, monkeypatch):
    archive_dir = tmp_path / "data" / "archive"
    archive_dir.mkdir(parents=True)
    (archive_dir / "2026-W28.json").write_text(
        """
        {
         "week": "2026-W28",
         "generated_at": "2026-07-11T12:00:00+00:00",
         "clusters": [{
          "title": "Archive & test",
          "url": "https://example.org/a?x=1&y=2",
          "n_members": 3,
          "n_sources": 2,
          "counts": {"left": 1, "centre": 0, "right": 1},
          "blindspot_score": null,
          "blindspot_for": null,
          "divergence": null,
          "category": "politique"
         }]
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    build_site(conn, tmp_path / "site")

    assert (tmp_path / "site" / "archives.html").is_file()
    week_html = (tmp_path / "site" / "archives" / "2026-W28.html").read_text(encoding="utf-8")
    assert "Archive &amp; test" in week_html

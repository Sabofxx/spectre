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


def test_main_feed_rfc822_and_own_text_only(conn, tmp_path):
    import re
    import xml.etree.ElementTree as ET

    seed(conn)
    # seed() builds a 2-article cluster; make it eligible (>= 3).
    art = make_article(source_id="c1", title="Titre c1", summary=SECRET, hours_ago=1)
    dbmod.insert_article(conn, art)
    aid = conn.execute("SELECT id FROM articles WHERE url = ?", (art.url,)).fetchone()[0]
    cid = conn.execute("SELECT id FROM clusters").fetchone()[0]
    dbmod.add_cluster_member(conn, cid, aid, 0.9)
    dbmod.update_cluster(conn, cid, b"\x00" * 8, "Titre g1", 3)
    conn.commit()

    build_site(conn, tmp_path)

    root = ET.parse(tmp_path / "feed.xml").getroot()
    item = root.find("./channel/item")
    assert item is not None
    pub = item.findtext("pubDate")
    assert re.match(r"^[A-Z][a-z]{2}, \d{2} [A-Z][a-z]{2} \d{4} \d{2}:\d{2}:\d{2}", pub)
    assert "Couvert par" in item.findtext("description")
    assert SECRET[15:60] not in (tmp_path / "feed.xml").read_text()
    # head links + footer mention
    index = (tmp_path / "index.html").read_text()
    assert 'rel="alternate" type="application/rss+xml"' in index
    assert "S'abonner par RSS" in index


def test_shared_owner_note(conn, tmp_path):
    conn.execute("UPDATE sources SET owner = 'Groupe Test' WHERE id IN ('d1', 'd2')")
    ids = []
    for src in ("d1", "d2", "g1"):
        art = make_article(source_id=src, title=f"Titre {src}", hours_ago=2)
        dbmod.insert_article(conn, art)
        ids.append(conn.execute("SELECT id FROM articles WHERE url = ?", (art.url,)).fetchone()[0])
    cid = dbmod.create_cluster(conn, b"\x00" * 8, "Titre d1", ids[0])
    for aid in ids[1:]:
        dbmod.add_cluster_member(conn, cid, aid, 0.9)
    dbmod.update_cluster(conn, cid, b"\x00" * 8, "Titre d1", 3)
    conn.commit()

    build_site(conn, tmp_path)
    html = (tmp_path / "cluster" / f"{cid}.html").read_text()
    assert "appartiennent" in html and "Groupe Test" in html
    # placeholder owners ('-') never trigger the note
    assert html.count("appartiennent") == 1


def test_og_images_generated(conn, tmp_path):
    seed(conn)
    build_site(conn, tmp_path)

    cid = conn.execute("SELECT id FROM clusters").fetchone()[0]
    img_path = tmp_path / "og" / f"{cid}.png"
    assert img_path.exists()
    from PIL import Image

    with Image.open(img_path) as img:
        assert img.size == (1200, 630)
    detail = (tmp_path / "cluster" / f"{cid}.html").read_text()
    assert f'property="og:image" content="https://sabofxx.github.io/spectre/og/{cid}.png"' in detail
    assert 'summary_large_image' in detail


def test_source_profile_pages(conn, tmp_path):
    seed(conn)
    build_site(conn, tmp_path)

    page = tmp_path / "source" / "g1.html"
    assert page.exists()
    html = page.read_text()
    assert "Gauche 1" in html and "Propriétaire" in html
    assert "cluster/" in html  # live subject links
    about = (tmp_path / "a-propos.html").read_text()
    assert 'href="source/g1.html"' in about


def test_search_index_and_page(conn, tmp_path):
    import json as jsonlib

    seed(conn)
    build_site(conn, tmp_path)

    rows = jsonlib.loads((tmp_path / "data" / "index.json").read_text())
    assert rows and rows[0]["t"].startswith("Titre")
    page = (tmp_path / "recherche.html").read_text()
    assert "data/index.json" in page
    assert "src=" not in page.split("<script>")[1]  # inline JS only


def test_seo_artifacts(conn, tmp_path):
    seed(conn)
    build_site(conn, tmp_path)

    assert (tmp_path / "robots.txt").read_text().startswith("User-agent: *")
    sitemap = (tmp_path / "sitemap.xml").read_text()
    assert "<loc>https://sabofxx.github.io/spectre/index.html</loc>" in sitemap
    assert "cluster/" in sitemap
    assert (tmp_path / "favicon.svg").exists()
    index = (tmp_path / "index.html").read_text()
    assert '<link rel="canonical" href="https://sabofxx.github.io/spectre/index.html">' in index
    assert '<meta name="description"' in index
    detail = next((tmp_path / "cluster").glob("*.html")).read_text()
    assert "couverture comparée" in detail  # informative title


def test_no_tracking_in_generated_site(conn, tmp_path):
    """Anti-algorithm pledge, enforced: no external scripts, no cookies,
    no storage APIs anywhere in the generated site."""
    import re

    seed(conn)
    build_site(conn, tmp_path)
    for page in tmp_path.rglob("*.html"):
        html = page.read_text()
        assert not re.search(r"<script[^>]+src=[\"']https?://", html), page.name
        assert "document.cookie" not in html, page.name
        assert "localStorage" not in html and "sessionStorage" not in html, page.name
        assert not re.search(r"gtag|googletagmanager|plausible\.io|matomo", html), page.name


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

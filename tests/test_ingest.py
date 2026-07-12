"""URL canonicalization, HTML cleaning, deduplication."""

from spectre import db as dbmod
from spectre.ingest import canonicalize_url, strip_html

from conftest import make_article


class TestCanonicalizeUrl:
    def test_strips_tracking_params(self):
        url = "https://ex.fr/a?utm_source=rss&utm_medium=feed&xtor=RSS-1&fbclid=x&at_medium=c"
        assert canonicalize_url(url) == "https://ex.fr/a"

    def test_keeps_meaningful_params(self):
        assert canonicalize_url("https://ex.fr/a?id=42&utm_source=x") == "https://ex.fr/a?id=42"

    def test_strips_fragment(self):
        assert canonicalize_url("https://ex.fr/a#xtor=RSS-1") == "https://ex.fr/a"

    def test_plain_url_unchanged(self):
        assert canonicalize_url("https://ex.fr/a/b.html") == "https://ex.fr/a/b.html"


class TestStripHtml:
    def test_removes_tags(self):
        assert strip_html("<p>Bonjour <b>le</b> monde</p>") == "Bonjour le monde"

    def test_decodes_entities(self):
        assert strip_html("D&eacute;j&agrave; l&#39;&eacute;t&eacute;") == "Déjà l'été"

    def test_collapses_whitespace(self):
        assert strip_html("  a \n\n  b\t c ") == "a b c"

    def test_none_and_empty(self):
        assert strip_html(None) is None
        assert strip_html("") is None
        assert strip_html("<p></p>") is None


class TestLoadSources:
    def test_real_config_loads(self):
        """Lock: the ACTUAL config/sources.yaml must parse into Source objects
        (caught in CI 2026-07-12: a doc-only YAML field crashed the pipeline)."""
        from pathlib import Path

        from spectre.ingest import load_sources

        config = Path(__file__).parent.parent / "config" / "sources.yaml"
        sources = load_sources(config)
        assert len(sources) >= 30
        assert all(s.orientation for s in sources)


class TestDeduplication:
    def test_same_url_inserted_once(self, conn):
        a = make_article(url="https://ex.fr/a", title="v1")
        b = make_article(url="https://ex.fr/a", title="v2")
        assert dbmod.insert_article(conn, a) is True
        assert dbmod.insert_article(conn, b) is False
        assert conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 1

    def test_same_guid_same_source_inserted_once(self, conn):
        a = make_article(source_id="g1", guid="guid-1", url="https://ex.fr/a")
        b = make_article(source_id="g1", guid="guid-1", url="https://ex.fr/b")
        assert dbmod.insert_article(conn, a) is True
        assert dbmod.insert_article(conn, b) is False

    def test_same_guid_different_source_ok(self, conn):
        a = make_article(source_id="g1", guid="guid-1")
        b = make_article(source_id="g2", guid="guid-1")
        assert dbmod.insert_article(conn, a) is True
        assert dbmod.insert_article(conn, b) is True

    def test_null_guids_do_not_collide(self, conn):
        assert dbmod.insert_article(conn, make_article(guid=None)) is True
        assert dbmod.insert_article(conn, make_article(guid=None)) is True

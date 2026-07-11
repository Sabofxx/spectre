"""URL-slug categorization heuristics."""

import numpy as np

from spectre import db as dbmod
from spectre.categorize import CATEGORY_PROTOTYPES, article_category, categorize_clusters, cluster_category

from conftest import make_article


class TestArticleCategory:
    def test_sport_url(self):
        assert article_category("https://ex.fr/sport/football/psg-gagne") == "sport"

    def test_sport_title_fallback(self):
        assert article_category(
            "https://ex.fr/actu/un-article", "Coupe du monde : la France en finale"
        ) == "sport"

    def test_faits_divers(self):
        assert article_category("https://ex.fr/faits-divers/agression-paris") == "faits-divers"
        assert article_category("https://ex.fr/fait-divers/vol") == "faits-divers"

    def test_politique(self):
        assert article_category("https://ex.fr/politique/remaniement") == "politique"

    def test_no_match_is_none(self):
        assert article_category("https://ex.fr/article/un-sujet-quelconque") is None

    def test_first_match_wins(self):
        # "sport" pattern is checked before "international".
        assert article_category("https://ex.fr/sport/international/mondial") == "sport"


class TestClusterCategory:
    def test_majority_vote(self):
        articles = [
            ("https://a.fr/sport/match", ""),
            ("https://b.fr/sport/resultat", ""),
            ("https://c.fr/culture/expo", ""),
        ]
        assert cluster_category(articles) == "sport"

    def test_unmatched_articles_do_not_vote(self):
        articles = [
            ("https://a.fr/page/un", ""),
            ("https://b.fr/page/deux", ""),
            ("https://c.fr/economie/bourse", ""),
        ]
        assert cluster_category(articles) == "économie"

    def test_no_signal_is_none(self):
        assert cluster_category([("https://a.fr/x", "Un titre"), ("https://b.fr/y", "")]) is None


class TestPrototypeFallback:
    def test_embedding_prototype_tags_cluster_without_url_signal(self, conn):
        class FakeModel:
            def encode(self, texts, normalize_embeddings=True, show_progress_bar=False):
                return np.eye(len(texts), dtype=np.float32)

        proto_names = list(CATEGORY_PROTOTYPES)
        idx = proto_names.index("politique")
        centroid = np.eye(len(proto_names), dtype=np.float32)[idx]
        art = make_article(url="https://example.org/article/sans-rubrique", title="Un titre")
        dbmod.insert_article(conn, art)
        article_id = conn.execute("SELECT id FROM articles WHERE url = ?", (art.url,)).fetchone()[0]
        dbmod.store_embeddings(conn, [(article_id, centroid.tobytes())])
        dbmod.create_cluster(conn, centroid.tobytes(), art.title, article_id)
        conn.commit()

        tagged = categorize_clusters(conn, model=FakeModel())

        assert tagged == 1
        assert conn.execute("SELECT category FROM clusters").fetchone()[0] == "politique"

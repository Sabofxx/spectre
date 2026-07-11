"""URL-slug categorization heuristics."""

from spectre.categorize import article_category, cluster_category


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

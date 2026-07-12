"""Blindspot scoring and log-odds vocabulary contrast."""

import json
import math
from collections import Counter

import pytest

from spectre import db as dbmod
from spectre import analyze as analyze_mod
from spectre.analyze import (
    blindspot_payload,
    compute_vocab_contrasts,
    log_odds_z,
    tokenize,
    with_bigrams,
)
from spectre.cluster import cluster_pending

from conftest import make_article
from test_cluster import put_article

# Matches the conftest referential: 4 left-bloc, 5 right-bloc, 1 centre.
ACTIVE = {"gauche": 2, "centre-gauche": 2, "centre": 1, "centre-droit": 2, "droite": 3}


class TestBlindspot:
    def test_right_only_is_left_blindspot(self):
        p = blindspot_payload({"droite": {"d1", "d2"}, "centre-droit": {"cd1"}}, ACTIVE)
        assert p["score"] == 1.0
        assert p["blindspot_for"] == "gauche"

    def test_left_only_is_right_blindspot(self):
        p = blindspot_payload({"gauche": {"g1"}, "centre-gauche": {"cg1"}}, ACTIVE)
        assert p["score"] == -1.0
        assert p["blindspot_for"] == "droite"

    def test_normalization_with_unequal_blocs(self):
        """2 of 4 left sources vs 2 of 5 right sources must NOT give 0:
        coverage is 0.5 vs 0.4, so the score leans left."""
        p = blindspot_payload(
            {"gauche": {"g1"}, "centre-gauche": {"cg1"},
             "centre-droit": {"cd1"}, "droite": {"d1"}},
            ACTIVE,
        )
        # (0.4 - 0.5) / (0.4 + 0.5) = -0.111
        assert p["score"] == pytest.approx(-0.111, abs=0.001)
        assert p["blindspot_for"] is None

    def test_same_source_counted_once_is_callers_contract(self):
        # Inputs are SETS of distinct sources: 4 articles from one outlet
        # arrive as a single source id. Balanced 1/4 vs 1/5 leans left too.
        p = blindspot_payload({"gauche": {"g1"}, "droite": {"d1"}}, ACTIVE)
        assert p["score"] == pytest.approx((0.2 - 0.25) / (0.2 + 0.25), abs=0.001)

    def test_centre_only_has_no_score(self):
        p = blindspot_payload({"centre": {"c1"}}, ACTIVE)
        assert p["score"] is None
        assert p["blindspot_for"] is None


class TestLogOdds:
    def test_hand_computed_toy_corpus(self):
        """Verifiable on paper. With prior_strength = prior total (6), each
        alpha_w equals the corpus count of w.

        'réforme' (A-only): delta = ln((2+2)/(3+6-2-2)) - ln((0+2)/(3+6-0-2))
                                  = ln(4/5) - ln(2/7) = 1.0296
                            var   = 1/4 + 1/2 = 0.75 -> z = 1.0296/0.8660 = 1.189
        'retraites' (balanced): identical terms on both sides -> z = 0.
        """
        counts_a = Counter({"réforme": 2, "retraites": 1})
        counts_b = Counter({"casse": 1, "sociale": 1, "retraites": 1})
        prior = counts_a + counts_b  # total mass 6
        z = log_odds_z(counts_a, counts_b, prior, prior_strength=6.0)

        assert z["réforme"] == pytest.approx(1.189, abs=0.001)
        assert z["retraites"] == pytest.approx(0.0, abs=1e-9)
        assert z["casse"] < 0 and z["sociale"] < 0

    def test_symmetry(self):
        a = Counter({"x": 3, "y": 1})
        b = Counter({"y": 4})
        prior = a + b
        z_ab = log_odds_z(a, b, prior, prior_strength=8.0)
        z_ba = log_odds_z(b, a, prior, prior_strength=8.0)
        for term in z_ab:
            assert z_ab[term] == pytest.approx(-z_ba[term], abs=1e-9)


class TestTokenize:
    def test_stopwords_and_case(self):
        assert tokenize("La motion de censure est votée") == ["motion", "censure", "votée"]

    def test_bigrams(self):
        assert with_bigrams(["motion", "censure"]) == ["motion", "censure", "motion censure"]

    def test_hyphens_and_accents(self):
        assert tokenize("Le cessez-le-feu à Téhéran") == ["cessez-le-feu", "téhéran"]


class TestInsufficientData:
    def test_small_cluster_gets_insufficient_status(self, conn):
        """4 articles, 2 blocs, but way under 50 tokens per side."""
        ids = [
            put_article(conn, [1, 0.01 * i, 0], hours_ago=4 - i, title="petit texte")
            for i in range(4)
        ]
        # Rebind two of them to right-bloc sources (put_article defaults to g1).
        conn.execute("UPDATE articles SET source_id = 'd1' WHERE id = ?", (ids[2],))
        conn.execute("UPDATE articles SET source_id = 'd2' WHERE id = ?", (ids[3],))
        cluster_pending(conn, threshold=0.7)
        assert conn.execute("SELECT MAX(n_members) FROM clusters").fetchone()[0] == 4

        stats = compute_vocab_contrasts(conn)

        assert stats == {"ok": 0, "insufficient_data": 1, "skipped": 0}
        payload = json.loads(
            conn.execute(
                "SELECT payload FROM analyses WHERE kind = 'vocab_contrast'"
            ).fetchone()[0]
        )
        assert payload["status"] == "insufficient_data"
        assert payload["n_tokens_left"] < 50 and payload["n_tokens_right"] < 50


def test_run_can_skip_categorization_for_pipeline(monkeypatch, conn):
    def fail_if_called(_conn):
        raise AssertionError("categorize_clusters should be skipped")

    monkeypatch.setattr(analyze_mod, "compute_blindspots", lambda _conn: 2)
    monkeypatch.setattr(analyze_mod, "compute_vocab_contrasts", lambda _conn: {"ok": 1})
    monkeypatch.setattr("spectre.categorize.categorize_clusters", fail_if_called)

    stats = analyze_mod.run(conn, categorize=False)

    assert stats == {"categorized": None, "blindspots": 2, "vocab": {"ok": 1}}

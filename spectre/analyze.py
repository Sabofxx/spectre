"""Framing analyses: blindspot detection and vocabulary contrast.

Blindspot (3a): pure arithmetic — coverage per political bloc counted in
DISTINCT sources (a outlet publishing 4 articles counts once), normalized by
the number of active sources of each bloc.

Vocabulary contrast (3b): log-odds ratio with informative Dirichlet prior
("Fightin' Words", Monroe et al. 2008) between left-bloc and right-bloc
texts, plus a TF-IDF divergence score. 100% local and free — a paid LLM
would be more qualitative but the statistics already expose the framing.
"""

from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
from collections import Counter, defaultdict

import numpy as np

from . import db
from .cluster import window_start
from .models import LEFT_BLOC, RIGHT_BLOC

logger = logging.getLogger(__name__)

BLINDSPOT_MIN_ARTICLES = 3
BLINDSPOT_SHARE = 0.80  # >= 80% of coverage from one side => blindspot
VOCAB_MIN_ARTICLES = 4
VOCAB_MIN_ORIENTATIONS = 2
# Floor per side, in unigram tokens after stopword removal: below this the
# log-odds output is noise, so we store an 'insufficient_data' status instead.
VOCAB_MIN_TOKENS = 50
# Ceiling for the LLM analysis: megaclusters make small models quote titles
# verbatim instead of analyzing (observed on a 19-article sports cluster).
OLLAMA_MAX_ARTICLES = 15
PRIOR_STRENGTH = 100.0  # total mass (a0) of the Dirichlet prior
TOP_TERMS = 10
# Thematic-merge detector: a REAL framing disagreement still shares the event
# vocabulary; near-zero lexical overlap + extreme divergence means the
# cluster probably glued distinct stories. Flagged, never hidden.
SUSPECT_DIVERGENCE = 0.90
SUSPECT_MAX_OVERLAP = 0.05
MIN_TERM_COUNT = 2  # a term must appear at least twice on its side

# Static French stopword list — no spaCy needed. Includes weekdays/months
# (they only encode publication timing). Caveat: "été" (the season) is
# sacrificed as the past participle of "être".
FRENCH_STOPWORDS = frozenset("""
le la les un une des du de à au aux en y et ou où mais donc or ni car ne pas
plus moins très peu trop assez tout tous toute toutes ce cet cette ces cela ça
ceci celui celle ceux celles ci là je tu il elle on nous vous ils elles me moi
te toi se soi lui leur eux mon ton son ma ta sa mes tes ses notre votre nos
vos leurs qui que quoi dont quand comment pourquoi si oui non avec sans sous
sur dans entre vers chez par pour contre depuis pendant avant après lors dès
jusque durant être est sont était étaient sera seront serait seraient suis es
sommes êtes été étant avoir ai as avons avez ont avait avaient aura auront
aurait auraient ayant eu faire fait faites font faisait fera feront ferait
peut peuvent pouvait pourra pourrait pourraient pu doit doivent devait devra
devrait dû va vont allait ira iront aller aussi encore déjà toujours jamais
souvent parfois ainsi alors puis ensuite enfin surtout comme même mêmes autre
autres quel quelle quels quelles chaque plusieurs certains certaines aucun
aucune quelque quelques deux trois quatre cinq six sept huit neuf dix cent
mille premier première second seconde dernier dernière selon notamment
cependant pourtant toutefois néanmoins afin voici voilà bien mal beaucoup
tant tellement près loin ici ailleurs lundi mardi mercredi jeudi vendredi
samedi dimanche janvier février mars avril mai juin juillet août septembre
octobre novembre décembre
""".split())

_WORD_RE = re.compile(r"[a-zà-öø-ÿœ]+(?:-[a-zà-öø-ÿœ]+)*")


def tokenize(text: str) -> list[str]:
    """Lowercase French tokens, stopwords and single letters removed."""
    words = _WORD_RE.findall(text.lower())
    return [w for w in words if len(w) > 1 and w not in FRENCH_STOPWORDS]


def with_bigrams(tokens: list[str]) -> list[str]:
    """Unigrams + adjacent bigrams (built after stopword removal, so
    'motion de censure' surfaces as 'motion censure')."""
    return tokens + [f"{a} {b}" for a, b in zip(tokens, tokens[1:])]


def article_text(title: str, summary: str | None) -> str:
    return f"{title} {summary}" if summary else title


# ---------------------------------------------------------------------------
# 3a — Blindspots
# ---------------------------------------------------------------------------

def blindspot_payload(
    sources_by_orientation: dict[str, set[str]],
    active_counts: dict[str, int],
) -> dict:
    """Blindspot metrics for one cluster.

    Coverage of a bloc = distinct sources having covered / active sources of
    that bloc. Score in [-1, +1]: -1 = left-bloc only, +1 = right-bloc only.
    """
    left_sources = sorted(set().union(*(sources_by_orientation.get(o, set()) for o in LEFT_BLOC)))
    right_sources = sorted(set().union(*(sources_by_orientation.get(o, set()) for o in RIGHT_BLOC)))
    centre_sources = sorted(sources_by_orientation.get("centre", set()))

    n_left_active = sum(active_counts.get(o, 0) for o in LEFT_BLOC)
    n_right_active = sum(active_counts.get(o, 0) for o in RIGHT_BLOC)
    cov_left = len(left_sources) / n_left_active if n_left_active else 0.0
    cov_right = len(right_sources) / n_right_active if n_right_active else 0.0

    score: float | None = None
    blindspot_for: str | None = None
    if cov_left + cov_right > 0:
        score = round((cov_right - cov_left) / (cov_right + cov_left), 3)
        right_share = cov_right / (cov_left + cov_right)
        if right_share >= BLINDSPOT_SHARE:
            blindspot_for = "gauche"  # the left is not covering this event
        elif right_share <= 1 - BLINDSPOT_SHARE:
            blindspot_for = "droite"
    return {
        "score": score,
        "blindspot_for": blindspot_for,
        "sources_left": left_sources,
        "sources_centre": centre_sources,
        "sources_right": right_sources,
        "coverage_left": round(cov_left, 3),
        "coverage_right": round(cov_right, 3),
    }


def compute_blindspots(conn: sqlite3.Connection) -> int:
    """Score every cluster with >= BLINDSPOT_MIN_ARTICLES articles."""
    active_counts = db.active_counts_by_orientation(conn)
    per_cluster: dict[int, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for row in db.blindspot_inputs(conn, BLINDSPOT_MIN_ARTICLES):
        per_cluster[row["cluster_id"]][row["orientation"]].add(row["source_id"])

    for cluster_id, by_orientation in per_cluster.items():
        payload = blindspot_payload(by_orientation, active_counts)
        db.save_analysis(conn, cluster_id, "blindspot", json.dumps(payload, ensure_ascii=False))
        db.set_cluster_blindspot(conn, cluster_id, payload["score"])
    conn.commit()
    logger.info("blindspot: %d clusters scored", len(per_cluster))
    return len(per_cluster)


# ---------------------------------------------------------------------------
# 3b — Vocabulary contrast (Fightin' Words)
# ---------------------------------------------------------------------------

def log_odds_z(
    counts_a: Counter, counts_b: Counter, prior: Counter, prior_strength: float = PRIOR_STRENGTH
) -> dict[str, float]:
    """Z-scored log-odds ratio per term; positive = overrepresented in A.

    Monroe et al. 2008, eq. 15-22, with an informative Dirichlet prior
    proportional to term frequency in the full corpus.
    """
    prior_total = sum(prior.values())
    n_a, n_b = sum(counts_a.values()), sum(counts_b.values())
    z: dict[str, float] = {}
    for term in set(counts_a) | set(counts_b):
        # Every cluster term exists in the corpus prior (clusters are a
        # subset of the corpus); the max() guards pathological cases.
        alpha = prior_strength * max(prior[term], 1) / prior_total
        ya, yb = counts_a[term], counts_b[term]
        delta = math.log((ya + alpha) / (n_a + prior_strength - ya - alpha)) - math.log(
            (yb + alpha) / (n_b + prior_strength - yb - alpha)
        )
        variance = 1.0 / (ya + alpha) + 1.0 / (yb + alpha)
        z[term] = delta / math.sqrt(variance)
    return z


def _top_terms(z: dict[str, float], counts: Counter, positive: bool) -> list[list]:
    """Top TOP_TERMS terms of one side, filtered on MIN_TERM_COUNT."""
    items = [
        (term, score) for term, score in z.items()
        if (score > 0) == positive and counts[term] >= MIN_TERM_COUNT
    ]
    items.sort(key=lambda t: -abs(t[1]))
    return [[term, round(score, 2)] for term, score in items[:TOP_TERMS]]


def build_corpus_stats(conn: sqlite3.Connection) -> tuple[Counter, object]:
    """Corpus-wide Dirichlet prior + fitted TF-IDF vectorizer (shared state)."""
    from sklearn.feature_extraction.text import TfidfVectorizer

    all_texts = [article_text(r["title"], r["summary"]) for r in db.all_article_texts(conn)]
    prior: Counter = Counter()
    for text in all_texts:
        prior.update(with_bigrams(tokenize(text)))
    vectorizer = TfidfVectorizer(analyzer=lambda t: with_bigrams(tokenize(t)))
    vectorizer.fit(all_texts)
    return prior, vectorizer


def contrast_payload(members: list, prior: Counter, vectorizer: object) -> dict:
    """Vocabulary-contrast payload for one cluster (status ok/insufficient_data).

    `members` are rows with orientation/title/summary. Pure function: no DB.
    """
    from sklearn.metrics.pairwise import cosine_similarity

    left_texts = [article_text(m["title"], m["summary"]) for m in members
                  if m["orientation"] in LEFT_BLOC]
    right_texts = [article_text(m["title"], m["summary"]) for m in members
                   if m["orientation"] in RIGHT_BLOC]
    left_tokens = [tokenize(t) for t in left_texts]
    right_tokens = [tokenize(t) for t in right_texts]
    n_left = sum(len(t) for t in left_tokens)
    n_right = sum(len(t) for t in right_tokens)

    if n_left < VOCAB_MIN_TOKENS or n_right < VOCAB_MIN_TOKENS:
        return {
            "status": "insufficient_data",
            "n_tokens_left": n_left,
            "n_tokens_right": n_right,
        }

    counts_left: Counter = Counter()
    counts_right: Counter = Counter()
    for toks in left_tokens:
        counts_left.update(with_bigrams(toks))
    for toks in right_tokens:
        counts_right.update(with_bigrams(toks))
    z = log_odds_z(counts_left, counts_right, prior)

    vec_left = np.asarray(vectorizer.transform(left_texts).mean(axis=0))
    vec_right = np.asarray(vectorizer.transform(right_texts).mean(axis=0))
    divergence = round(float(1.0 - cosine_similarity(vec_left, vec_right)[0, 0]), 3)

    # Jaccard overlap of each side's top unigrams (bigrams excluded).
    top_left = {t for t, _ in counts_left.most_common(40) if " " not in t}
    top_right = {t for t, _ in counts_right.most_common(40) if " " not in t}
    top_left = set(list(top_left)[:15])
    top_right = set(list(top_right)[:15])
    union = top_left | top_right
    overlap = len(top_left & top_right) / len(union) if union else 0.0
    suspect = divergence > SUSPECT_DIVERGENCE and overlap < SUSPECT_MAX_OVERLAP

    return {
        "status": "ok",
        "left_terms": _top_terms(z, counts_left, positive=True),
        "right_terms": _top_terms(z, counts_right, positive=False),
        "divergence": divergence,
        "lexical_overlap": round(overlap, 3),
        "suspect_merge": suspect,
        "n_tokens_left": n_left,
        "n_tokens_right": n_right,
    }


def compute_vocab_contrasts(conn: sqlite3.Connection) -> dict[str, int]:
    """Log-odds contrast + TF-IDF divergence for every eligible cluster."""
    prior, vectorizer = build_corpus_stats(conn)

    # Only clusters still in the 72h window: beyond it, summaries are purged
    # and analyses must consume their STORED results, never the raw text.
    rows = db.vocab_inputs(conn, VOCAB_MIN_ARTICLES, window_start())
    clusters: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        clusters[row["cluster_id"]].append(row)

    stats = {"ok": 0, "insufficient_data": 0, "skipped": 0}
    for cluster_id, members in clusters.items():
        if len({m["orientation"] for m in members}) < VOCAB_MIN_ORIENTATIONS:
            stats["skipped"] += 1
            continue
        payload = contrast_payload(members, prior, vectorizer)
        db.save_analysis(
            conn, cluster_id, "vocab_contrast", json.dumps(payload, ensure_ascii=False)
        )
        if payload["status"] == "ok":
            db.set_cluster_divergence(conn, cluster_id, payload["divergence"])
            db.set_cluster_suspect(conn, cluster_id, payload["suspect_merge"])
            if payload["suspect_merge"]:
                stats["suspect"] = stats.get("suspect", 0) + 1
            stats["ok"] += 1
        else:
            stats["insufficient_data"] += 1

    conn.commit()
    logger.info("vocab contrast: %s", stats)
    return stats


def compute_ollama(conn: sqlite3.Connection, analyzer=None) -> dict:
    """Qualitative framing via a local LLM, for clusters still in the window.

    Same eligibility as the vocab contrast (>= 4 articles, >= 2 orientations,
    72h window: summaries are purged beyond it). Cache: a cluster is only
    re-analyzed when its composition changed by >= 2 articles since the
    stored payload (a 7B model on CPU is expensive).
    """
    from .analyzers import ClusterData, OllamaAnalyzer

    analyzer = analyzer if analyzer is not None else OllamaAnalyzer()
    stats = {"analyzed": 0, "skipped_cache": 0, "skipped_size": 0, "invalid": 0}
    if not analyzer.available():
        stats["unavailable"] = True
        return stats

    clusters: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for row in db.ollama_inputs(conn, VOCAB_MIN_ARTICLES, window_start()):
        clusters[row["cluster_id"]].append(row)

    for cluster_id, members in clusters.items():
        if len({m["orientation"] for m in members}) < VOCAB_MIN_ORIENTATIONS:
            continue
        if len(members) > OLLAMA_MAX_ARTICLES:
            stats["skipped_size"] += 1
            continue
        if db.cluster_is_suspect(conn, cluster_id):
            continue  # no qualitative analysis on a probably-glued cluster
        ids = sorted(m["article_id"] for m in members)
        stored = db.get_analyses(conn, cluster_id).get("ollama")
        if stored:
            prev_ids = json.loads(stored).get("article_ids", [])
            if len(set(ids) ^ set(prev_ids)) < 2:
                stats["skipped_cache"] += 1
                continue
        payload = analyzer.analyze(ClusterData(cluster_id=cluster_id, members=members))
        if payload is None:
            stats["invalid"] += 1
            continue
        payload["article_ids"] = ids
        payload["model"] = analyzer.model
        db.save_analysis(conn, cluster_id, "ollama", json.dumps(payload, ensure_ascii=False))
        conn.commit()  # commit per cluster: a 7B-on-CPU run can be interrupted
        stats["analyzed"] += 1

    logger.info("ollama analysis: %s", stats)
    return stats


def run(conn: sqlite3.Connection, categorize: bool = False) -> dict:
    """Run both analyses; returns summary stats.

    Categorization belongs to cluster.run() (embedding prototypes need the
    loaded model); the URL-only pass here would overwrite better labels, so
    it stays opt-in for tests only.
    """
    n_categorized: int | None = None
    if categorize:
        from .categorize import categorize_clusters

        n_categorized = categorize_clusters(conn)
    n_blindspots = compute_blindspots(conn)
    vocab_stats = compute_vocab_contrasts(conn)
    return {"categorized": n_categorized, "blindspots": n_blindspots, "vocab": vocab_stats}

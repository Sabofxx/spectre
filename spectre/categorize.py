"""Cluster categorization from URL section slugs (zero-cost heuristics).

French media URLs carry their section ("/sport/", "/faits-divers/", …).
A cluster's category is the majority vote of its members' categories.
Purpose: de-noise the blindspots page — sport and faits-divers coverage
gaps are structural (left-leaning outlets publish little of either), not
editorial blindspots.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections import Counter

from . import db

logger = logging.getLogger(__name__)

# Checked in order; first match wins. Patterns run on the URL path (lowercase).
_URL_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("sport", re.compile(
        r"/sport|/football|/rugby|/tennis|/cyclisme|/basket|/athletisme"
        r"|/coupe-du-monde|/mondial|/ligue-?1|/ligue-des-champions"
        r"|/tour-de-france|/roland-garros|/jo-?20\d\d")),
    ("faits-divers", re.compile(r"/faits?-divers")),
    ("culture", re.compile(
        r"/culture|/cinema|/musique|/livres?|/series|/arts|/theatre"
        r"|/festival|/people|/medias|/television")),
    ("économie", re.compile(
        r"/economie|/entreprises?|/bourse|/conso|/immobilier|/argent|/emploi")),
    ("international", re.compile(
        r"/international|/monde/|/etats-unis|/proche-orient|/moyen-orient"
        r"|/asie|/afrique|/ameriques|/europe/")),
    ("politique", re.compile(
        r"/politique|/elections?|/presidentielle|/assemblee|/gouvernement")),
]

# Title fallback for sport only — the #1 noise source on blindspots, and the
# vocabulary is unambiguous enough for a static list.
_SPORT_TITLE = re.compile(
    r"\b(coupe du monde|ligue 1|ligue des champions|roland-garros"
    r"|tour de france|xv de france|demi-finale|quart de finale)\b",
    re.IGNORECASE,
)

# Categories whose one-sided coverage is structural, not an editorial choice.
STRUCTURAL_CATEGORIES = {"sport", "faits-divers"}


def article_category(url: str, title: str = "") -> str | None:
    """Category of one article, or None when no pattern matches."""
    path = url.lower()
    for category, pattern in _URL_PATTERNS:
        if pattern.search(path):
            return category
    if title and _SPORT_TITLE.search(title):
        return "sport"
    return None


def cluster_category(articles: list[tuple[str, str]]) -> str | None:
    """Majority category over (url, title) pairs; None if nothing matches."""
    votes = Counter(
        c for url, title in articles if (c := article_category(url, title))
    )
    if not votes:
        return None
    return votes.most_common(1)[0][0]


def categorize_clusters(conn: sqlite3.Connection) -> int:
    """(Re)compute the category of every cluster. Returns clusters tagged."""
    rows = conn.execute(
        """
        SELECT m.cluster_id, a.url, a.title
        FROM cluster_members m JOIN articles a ON a.id = m.article_id
        """
    ).fetchall()
    by_cluster: dict[int, list[tuple[str, str]]] = {}
    for r in rows:
        by_cluster.setdefault(r["cluster_id"], []).append((r["url"], r["title"]))
    tagged = 0
    for cluster_id, articles in by_cluster.items():
        category = cluster_category(articles)
        conn.execute("UPDATE clusters SET category = ? WHERE id = ?", (category, cluster_id))
        if category:
            tagged += 1
    conn.commit()
    logger.info("categorize: %d/%d clusters tagged", tagged, len(by_cluster))
    return tagged

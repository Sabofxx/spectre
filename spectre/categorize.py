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
    ("environnement", re.compile(
        r"/environnement|/planete|/planète|/climat|/meteo|/météo"
        r"|/canicule|/secheresse|/sécheresse|/tempete|/tempête|/inondations?")),
    ("sciences-tech", re.compile(
        r"/sciences?|/high-tech|/technologies?|/numerique|/espace"
        r"|/intelligence-artificielle|/tech/")),
    ("société", re.compile(
        r"/societe|/education|/sante|/enseignement|/famille|/religion"
        r"|/logement|/travail|/egalite")),
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
_ENVIRONMENT_TITLE = re.compile(
    r"\b(canicule|vigilance rouge|météo-france|meteo-france|chaleur extrême"
    r"|sécheresse|secheresse|inondations?|tempête|tempete|typhon|cyclone)\b",
    re.IGNORECASE,
)

# Categories whose one-sided coverage is structural, not an editorial choice.
STRUCTURAL_CATEGORIES = {"sport", "faits-divers"}

# Prototype sentences for embedding-based fallback (URL slugs only cover part
# of the corpus). Compared against cluster centroids; E5 similarities are
# compressed, hence the high floor — below it we prefer no category over a
# wrong one.
CATEGORY_PROTOTYPES: dict[str, str] = {
    "sport": "match de football, compétition sportive, championnat, victoire d'une équipe, tournoi, athlète",
    "faits-divers": "fait divers, agression, meurtre, vol, accident, enquête de police, victime, interpellation",
    "environnement": "météo, climat, canicule, chaleur extrême, sécheresse, tempête, inondation, vigilance rouge",
    "sciences-tech": "sciences, découverte scientifique, espace, fusée, satellite, intelligence artificielle, nouvelles technologies, numérique, recherche",
    "société": "éducation, école, université, santé publique, hôpital, logement, famille, inégalités sociales, droits, discriminations",
    "culture": "film, cinéma, musique, livre, exposition, festival, série télévisée, artiste, spectacle",
    "économie": "économie, entreprise, marchés financiers, inflation, emploi, budget, croissance, commerce",
    "international": "relations internationales, guerre, diplomatie, conflit entre pays, sommet, crise à l'étranger",
    "politique": "politique française, gouvernement, élection, parlement, parti politique, ministre, réforme",
}
# Calibrated 2026-07-12 against URL-derived labels (100 clusters): floor 0.84
# keeps 81% coverage at 63% raw agreement, and the errors concentrate in ONE
# harmful pattern — the faits-divers prototype matching any violent news
# (12 wrongly-structural tags). Hence faits-divers is URL-slug-only: the
# prototype fallback may never assign it (hiding a real blindspot is worse
# than showing a noisy one). Remaining confusions (politique/international/
# environnement) are between editorial categories and do not filter anything.
PROTOTYPE_SIM_FLOOR = 0.84
PROTOTYPE_FORBIDDEN = frozenset({"faits-divers"})


def article_category(url: str, title: str = "") -> str | None:
    """Category of one article, or None when no pattern matches."""
    path = url.lower()
    for category, pattern in _URL_PATTERNS:
        if pattern.search(path):
            return category
    if title and _ENVIRONMENT_TITLE.search(title):
        return "environnement"
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


def _prototype_category(centroid, proto_names: list[str], proto_vecs) -> str | None:
    """Best allowed prototype match for a centroid, or None below the floor.

    Categories in PROTOTYPE_FORBIDDEN can only come from URL slugs.
    """
    import numpy as np

    sims = proto_vecs @ centroid
    order = np.argsort(-sims)
    for i in order:
        name = proto_names[int(i)]
        if name in PROTOTYPE_FORBIDDEN:
            continue
        return name if float(sims[int(i)]) >= PROTOTYPE_SIM_FLOOR else None
    return None


def categorize_clusters(conn: sqlite3.Connection, model=None) -> int:
    """(Re)compute the category of every cluster. Returns clusters tagged.

    URL-slug majority vote first (precise); when it yields nothing and an
    embedding model is provided, fall back to prototype matching against the
    cluster centroid.
    """
    import numpy as np

    rows = conn.execute(
        """
        SELECT m.cluster_id, a.url, a.title
        FROM cluster_members m JOIN articles a ON a.id = m.article_id
        """
    ).fetchall()
    by_cluster: dict[int, list[tuple[str, str]]] = {}
    for r in rows:
        by_cluster.setdefault(r["cluster_id"], []).append((r["url"], r["title"]))

    proto_names: list[str] = []
    proto_vecs = None
    centroids: dict[int, object] = {}
    if model is not None:
        from .cluster import E5_PREFIX

        proto_names = list(CATEGORY_PROTOTYPES)
        proto_vecs = model.encode(
            [E5_PREFIX + CATEGORY_PROTOTYPES[n] for n in proto_names],
            normalize_embeddings=True, show_progress_bar=False,
        )
        for c in conn.execute("SELECT id, centroid FROM clusters").fetchall():
            centroids[c["id"]] = np.frombuffer(c["centroid"], dtype=np.float32)

    tagged = 0
    for cluster_id, articles in by_cluster.items():
        category = cluster_category(articles)
        if category is None and proto_vecs is not None and cluster_id in centroids:
            category = _prototype_category(centroids[cluster_id], proto_names, proto_vecs)
        conn.execute("UPDATE clusters SET category = ? WHERE id = ?", (category, cluster_id))
        if category:
            tagged += 1
    conn.commit()
    logger.info("categorize: %d/%d clusters tagged", tagged, len(by_cluster))
    return tagged

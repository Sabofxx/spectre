"""Static site generation with Jinja2.

HARD LEGAL CONSTRAINT (droits voisins de la presse): RSS summaries are NEVER
written into the generated HTML. Public pages carry article TITLES, LINKS to
the originals, and our own computed analyses — nothing else. Summaries exist
solely for internal computation (embeddings, log-odds, TF-IDF).
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import db
from .models import EDITORIAL_STYLES, LEFT_BLOC, ORIENTATIONS, RIGHT_BLOC

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
REPO_URL = "https://github.com/Sabofxx/spectre"
SITE_BASE_URL = "https://sabofxx.github.io/spectre/"  # absolute links for RSS

FEED_WINDOW_HOURS = 48
FEED_MIN_MEMBERS = 2
# A single-outlet "event" is not coverage; it pollutes the feed tail.
FEED_MIN_SOURCES = 2
BLINDSPOT_WINDOW_DAYS = 7
BLINDSPOT_MIN_MEMBERS = 3
BLINDSPOT_THRESHOLD = 0.6
# The blindspot LABEL needs real coverage to mean anything: three friendly
# outlets picking up a wire story is small-cluster noise, not an angle mort.
BLINDSPOT_LABEL_MIN_SOURCES = 5
BLINDSPOT_LABEL_MIN_SIDE = 3

# Recurring program items (daily news bulletins, weather segments) cluster
# with themselves day after day; they are schedule containers, not events.
# Filtered at render time only — the data stays in the DB.
_RECURRING_TITLE = re.compile(
    r"^(journal de \d{1,2}h|le journal de \d|météo du \d|flash info"
    r"|on ne plaisante pas avec l'info)|prévisions météo à \d",
    re.IGNORECASE,
)

_PARIS = ZoneInfo("Europe/Paris")


_FRENCH_DAYS = ("lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim.")


def _fmt_dt(iso: str | None, fmt: str = "%d/%m %H:%M") -> str:
    """Paris-time formatter; %a is rendered as a French weekday (the CI
    runner's C locale would otherwise print English day names)."""
    if not iso:
        return ""
    dt = datetime.fromisoformat(iso).astimezone(_PARIS)
    return dt.strftime(fmt.replace("%a", _FRENCH_DAYS[dt.weekday()]))


def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=select_autoescape(["html", "xml"]),
    )
    env.filters["fmt_dt"] = _fmt_dt
    return env


def _bloc_counts(source_rows: list[sqlite3.Row]) -> dict[str, int]:
    """Distinct-source counts per political bloc for the coverage bar."""
    blocs = {"left": set(), "centre": set(), "right": set()}
    for r in source_rows:
        if r["orientation"] in LEFT_BLOC:
            blocs["left"].add(r["source_id"])
        elif r["orientation"] in RIGHT_BLOC:
            blocs["right"].add(r["source_id"])
        else:
            blocs["centre"].add(r["source_id"])
    return {k: len(v) for k, v in blocs.items()}


def _style_counts(source_rows: list[sqlite3.Row]) -> dict[str, int]:
    """Distinct-source counts per editorial style for the style bar."""
    styles = {style: set() for style in EDITORIAL_STYLES}
    for r in source_rows:
        styles[r["editorial_style"]].add(r["source_id"])
    return {k: len(v) for k, v in styles.items()}


def _blindspot_label(score: float | None) -> str | None:
    """Which side is blind, per the >= 80% share rule (|score| >= 0.6)."""
    if score is None:
        return None
    if score >= BLINDSPOT_THRESHOLD:
        return "gauche"
    if score <= -BLINDSPOT_THRESHOLD:
        return "droite"
    return None


def build_cards(conn: sqlite3.Connection, since: str, min_members: int) -> list[dict]:
    """Cluster cards (feed / blindspot / archive pages).

    Sorted by DISTINCT SOURCES first, then article count: a single prolific
    outlet must not outrank an event that many newsrooms picked up.
    NEVER sorted by divergence_score: a high divergence can flag an imperfect
    cluster as much as a real framing disagreement.
    """
    by_cluster: dict[int, dict] = {}
    for row in db.cluster_source_rows(conn, since, min_members):
        card = by_cluster.setdefault(
            row["cluster_id"],
            {
                "id": row["cluster_id"],
                "title": row["title"],
                "n_members": row["n_members"],
                "divergence": row["divergence_score"],
                "blindspot_score": row["blindspot_score"],
                "blindspot_for": _blindspot_label(row["blindspot_score"]),
                "category": row["category"],
                "updated_at": row["updated_at"],
                "source_rows": [],
            },
        )
        card["source_rows"].append(row)
    cards = []
    for card in by_cluster.values():
        rows = card.pop("source_rows")
        card["counts"] = _bloc_counts(rows)
        card["style_counts"] = _style_counts(rows)
        card["sources"] = sorted(
            {(r["source_name"], r["orientation"], r["editorial_style"]) for r in rows},
            key=lambda t: (ORIENTATIONS.index(t[1]), EDITORIAL_STYLES.index(t[2]), t[0]),
        )
        card["sources"] = [
            {"name": n, "orientation": o, "editorial_style": style}
            for n, o, style in card["sources"]
        ]
        card["n_sources"] = len(card["sources"])
        # Gate the blindspot label on actual coverage: enough total sources
        # AND enough distinct outlets on the covering side.
        if card["blindspot_for"]:
            covering = (
                card["counts"]["right"] if card["blindspot_score"] > 0
                else card["counts"]["left"]
            )
            if (card["n_sources"] < BLINDSPOT_LABEL_MIN_SOURCES
                    or covering < BLINDSPOT_LABEL_MIN_SIDE):
                card["blindspot_for"] = None
        if _RECURRING_TITLE.search(card["title"] or ""):
            continue  # program containers, not events
        cards.append(card)
    cards.sort(key=lambda c: (-c["n_sources"], -c["n_members"]))
    return cards


def _vocab_view(payload: dict) -> dict:
    """Vocab payload + a z-score -> bar-width scaler for the template."""
    if payload.get("status") == "ok":
        peak = max(
            [abs(z) for _, z in payload["left_terms"] + payload["right_terms"]] or [1.0]
        )
        payload["scale"] = lambda z, peak=peak: round(min(abs(z) / peak, 1.0) * 100)
    return payload


def _og_coverage(counts: dict[str, int], n_members: int) -> str:
    return (
        f"Couvert par {counts['left']} source(s) à gauche, {counts['centre']} au centre,"
        f" {counts['right']} à droite — {n_members} articles."
    )


def _cards_overview(cards: list[dict]) -> dict[str, int]:
    """Small aggregate counters for page headers."""
    sources = {(s["name"], s["orientation"], s["editorial_style"]) for c in cards for s in c["sources"]}
    return {
        "events": len(cards),
        "articles": sum(c["n_members"] for c in cards),
        "sources": len(sources),
        "factuel": len({s for s in sources if s[2] == "factuel"}),
        "mixte": len({s for s in sources if s[2] == "mixte"}),
        "opinion": len({s for s in sources if s[2] == "opinion"}),
    }


def find_leaks(conn: sqlite3.Connection, site_dir: Path) -> list[str]:
    """Return RSS summaries whose text appears in the generated HTML.

    Guard for the droits-voisins constraint; the CI deploy fails on any hit.
    Probes a mid-sentence slice of each summary so a title that merely repeats
    the summary's opening words does not false-positive.
    """
    summaries = [
        r["summary"]
        for r in conn.execute(
            "SELECT summary FROM articles WHERE summary IS NOT NULL AND length(summary) > 60"
        )
    ]
    html = " ".join(p.read_text(encoding="utf-8") for p in site_dir.rglob("*.html"))
    return [s for s in summaries if s[15:60] in html]


def build_site(conn: sqlite3.Connection, out_dir: Path) -> dict[str, int]:
    """Generate the whole static site into out_dir. Returns page counts."""
    env = _env()
    # Re-clustering renumbers events: purge previously rendered detail and
    # archive pages so a local `serve` never exposes stale orphans. (CI always
    # starts from a clean checkout; this matters for local browsing.)
    for stale_dir in (out_dir / "cluster", out_dir / "archives"):
        if stale_dir.is_dir():
            for old in stale_dir.glob("*.html"):
                old.unlink()
    (out_dir / "cluster").mkdir(parents=True, exist_ok=True)
    shutil.copy(TEMPLATES_DIR / "style.css", out_dir / "style.css")

    now = datetime.now(timezone.utc)
    base_ctx = {
        "generated_at": now.astimezone(_PARIS).strftime("%d/%m/%Y %H:%M (%Z)"),
        "repo_url": REPO_URL,
        "health": db.feed_health(conn),
    }

    feed_since = (now - timedelta(hours=FEED_WINDOW_HOURS)).isoformat(timespec="seconds")
    week_since = (now - timedelta(days=BLINDSPOT_WINDOW_DAYS)).isoformat(timespec="seconds")
    feed_cards = [
        c for c in build_cards(conn, feed_since, FEED_MIN_MEMBERS)
        if c["n_sources"] >= FEED_MIN_SOURCES
    ]
    week_cards = build_cards(conn, week_since, BLINDSPOT_MIN_MEMBERS)
    blind_cards = [c for c in week_cards if c["blindspot_for"]]
    blind_cards.sort(key=lambda c: (-abs(c["blindspot_score"]), -c["n_members"]))

    (out_dir / "index.html").write_text(
        env.get_template("index.html").render(
            **base_ctx, root="", clusters=feed_cards,
            active_page="index", overview=_cards_overview(feed_cards),
            og_title="Spectre — qui couvre quoi dans la presse française",
            og_description=(
                "Agrégateur d'actualité française : couverture par orientation"
                " politique, contrastes de cadrage et angles morts médiatiques."
            ),
        ),
        encoding="utf-8",
    )

    # Sport and faits-divers one-sidedness is structural (left outlets barely
    # cover them), not an editorial blindspot: keep them out of the columns.
    from .categorize import STRUCTURAL_CATEGORIES

    editorial = [c for c in blind_cards if c["category"] not in STRUCTURAL_CATEGORIES]
    structural = [c for c in blind_cards if c["category"] in STRUCTURAL_CATEGORIES]

    (out_dir / "blindspots.html").write_text(
        env.get_template("blindspots.html").render(
            **base_ctx, root="",
            left_covered=[c for c in editorial if c["blindspot_for"] == "droite"],
            right_covered=[c for c in editorial if c["blindspot_for"] == "gauche"],
            structural=structural,
            active_page="blindspots",
            overview={
                "editorial": len(editorial),
                "structural": len(structural),
                "left_covered": sum(1 for c in editorial if c["blindspot_for"] == "droite"),
                "right_covered": sum(1 for c in editorial if c["blindspot_for"] == "gauche"),
            },
            og_title="Blindspots — Spectre",
            og_description="Les sujets couverts massivement par un bord du spectre"
                           " médiatique et ignorés par l'autre.",
        ),
        encoding="utf-8",
    )

    # Outgoing RSS feed of editorial blindspots (an aggregator you can
    # subscribe to). Items link to the representative ORIGINAL article.
    rss_items = []
    for c in blind_cards[:30]:
        top = db.cluster_top_article(conn, c["id"])
        rss_items.append({
            **c,
            "article_url": top["url"] if top else SITE_BASE_URL,
            "published_at": top["published_at"] if top else None,
        })
    (out_dir / "blindspots.xml").write_text(
        env.get_template("blindspots.xml").render(
            items=rss_items, site_url=SITE_BASE_URL, generated_at=base_ctx["generated_at"],
        ),
        encoding="utf-8",
    )

    # Archive pages, rebuilt from the committed weekly JSON snapshots.
    from .archive import load_snapshots

    snapshots = load_snapshots()
    (out_dir / "archives").mkdir(exist_ok=True)
    for snap in snapshots:
        (out_dir / "archives" / f"{snap['week']}.html").write_text(
            env.get_template("archive_week.html").render(
                **base_ctx, root="../", snap=snap,
                active_page="archives",
                og_title=f"Archives {snap['week']} — Spectre",
                og_description="Instantané hebdomadaire : les événements et leurs"
                               " couvertures par bord.",
            ),
            encoding="utf-8",
        )
    (out_dir / "archives.html").write_text(
        env.get_template("archives.html").render(
            **base_ctx, root="", snapshots=snapshots,
            active_page="archives",
            og_title="Archives — Spectre",
            og_description="La mémoire du spectre médiatique, semaine par semaine.",
        ),
        encoding="utf-8",
    )

    (out_dir / "a-propos.html").write_text(
        env.get_template("apropos.html").render(
            **base_ctx, root="", sources=db.all_sources(conn),
            active_page="about",
            og_title="À propos — Spectre",
            og_description="Méthodologie, référentiel d'orientations (indicatif et"
                           " débattable) et limites connues du projet.",
        ),
        encoding="utf-8",
    )

    # Detail pages: every cluster reachable from the feed or blindspot pages.
    detail_cards = {c["id"]: c for c in feed_cards}
    detail_cards.update({c["id"]: c for c in blind_cards})
    tpl = env.get_template("cluster.html")
    for card in detail_cards.values():
        analyses = {
            k: json.loads(v) for k, v in db.get_analyses(conn, card["id"]).items()
        }
        members = db.cluster_members_detail(conn, card["id"])
        by_orientation = [
            (o, [m for m in members if m["orientation"] == o]) for o in ORIENTATIONS
        ]
        ctx = {
            **card,
            "by_orientation": [(o, ms) for o, ms in by_orientation if ms],
            "vocab": _vocab_view(analyses["vocab_contrast"]) if "vocab_contrast" in analyses else None,
            "ollama": analyses.get("ollama"),
        }
        (out_dir / "cluster" / f"{card['id']}.html").write_text(
            tpl.render(
                **base_ctx, root="../", c=ctx, og_type="article", active_page="index",
                og_title=card["title"],
                og_description=_og_coverage(card["counts"], card["n_members"]),
            ),
            encoding="utf-8",
        )

    stats = {"feed": len(feed_cards), "blindspots": len(blind_cards), "details": len(detail_cards)}
    logger.info("site built in %s: %s", out_dir, stats)
    return stats

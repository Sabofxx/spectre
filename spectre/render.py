"""Static site generation with Jinja2.

HARD LEGAL CONSTRAINT (droits voisins de la presse): RSS summaries are NEVER
written into the generated HTML. Public pages carry article TITLES, LINKS to
the originals, and our own computed analyses — nothing else. Summaries exist
solely for internal computation (embeddings, log-odds, TF-IDF).
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import db
from .models import LEFT_BLOC, ORIENTATIONS, RIGHT_BLOC

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
REPO_URL = "https://github.com/Sabofxx/spectre"

FEED_WINDOW_HOURS = 48
FEED_MIN_MEMBERS = 2
BLINDSPOT_WINDOW_DAYS = 7
BLINDSPOT_MIN_MEMBERS = 3
BLINDSPOT_THRESHOLD = 0.6

_PARIS = ZoneInfo("Europe/Paris")


def _fmt_dt(iso: str | None, fmt: str = "%d/%m %H:%M") -> str:
    if not iso:
        return ""
    return datetime.fromisoformat(iso).astimezone(_PARIS).strftime(fmt)


def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=select_autoescape(["html"]),
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


def _blindspot_label(score: float | None) -> str | None:
    """Which side is blind, per the >= 80% share rule (|score| >= 0.6)."""
    if score is None:
        return None
    if score >= BLINDSPOT_THRESHOLD:
        return "gauche"
    if score <= -BLINDSPOT_THRESHOLD:
        return "droite"
    return None


def _cards(conn: sqlite3.Connection, since: str, min_members: int) -> list[dict]:
    """Cluster cards (feed / blindspot pages), sorted by coverage volume.

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
                "source_rows": [],
            },
        )
        card["source_rows"].append(row)
    cards = []
    for card in by_cluster.values():
        rows = card.pop("source_rows")
        card["counts"] = _bloc_counts(rows)
        card["sources"] = sorted(
            {(r["source_name"], r["orientation"]) for r in rows},
            key=lambda t: (ORIENTATIONS.index(t[1]), t[0]),
        )
        card["sources"] = [{"name": n, "orientation": o} for n, o in card["sources"]]
        card["n_sources"] = len(card["sources"])
        cards.append(card)
    cards.sort(key=lambda c: (-c["n_members"], -c["n_sources"]))
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
    (out_dir / "cluster").mkdir(parents=True, exist_ok=True)
    shutil.copy(TEMPLATES_DIR / "style.css", out_dir / "style.css")

    now = datetime.now(timezone.utc)
    base_ctx = {
        "generated_at": now.astimezone(_PARIS).strftime("%d/%m/%Y %H:%M (%Z)"),
        "repo_url": REPO_URL,
    }

    feed_since = (now - timedelta(hours=FEED_WINDOW_HOURS)).isoformat(timespec="seconds")
    week_since = (now - timedelta(days=BLINDSPOT_WINDOW_DAYS)).isoformat(timespec="seconds")
    feed_cards = _cards(conn, feed_since, FEED_MIN_MEMBERS)
    week_cards = _cards(conn, week_since, BLINDSPOT_MIN_MEMBERS)
    blind_cards = [c for c in week_cards if c["blindspot_for"]]
    blind_cards.sort(key=lambda c: (-abs(c["blindspot_score"]), -c["n_members"]))

    (out_dir / "index.html").write_text(
        env.get_template("index.html").render(
            **base_ctx, root="", clusters=feed_cards,
            og_title="Spectre — qui couvre quoi dans la presse française",
            og_description=(
                "Agrégateur d'actualité française : couverture par orientation"
                " politique, contrastes de cadrage et angles morts médiatiques."
            ),
        ),
        encoding="utf-8",
    )

    (out_dir / "blindspots.html").write_text(
        env.get_template("blindspots.html").render(
            **base_ctx, root="",
            left_covered=[c for c in blind_cards if c["blindspot_for"] == "droite"],
            right_covered=[c for c in blind_cards if c["blindspot_for"] == "gauche"],
            og_title="Blindspots — Spectre",
            og_description="Les sujets couverts massivement par un bord du spectre"
                           " médiatique et ignorés par l'autre.",
        ),
        encoding="utf-8",
    )

    (out_dir / "a-propos.html").write_text(
        env.get_template("apropos.html").render(
            **base_ctx, root="", sources=db.all_sources(conn),
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
            "llm": analyses.get("llm_framing"),
        }
        (out_dir / "cluster" / f"{card['id']}.html").write_text(
            tpl.render(
                **base_ctx, root="../", c=ctx, og_type="article",
                og_title=card["title"],
                og_description=_og_coverage(card["counts"], card["n_members"]),
            ),
            encoding="utf-8",
        )

    stats = {"feed": len(feed_cards), "blindspots": len(blind_cards), "details": len(detail_cards)}
    logger.info("site built in %s: %s", out_dir, stats)
    return stats

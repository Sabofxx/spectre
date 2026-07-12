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
from . import ogimage as og_image
from .models import EDITORIAL_STYLES, LEFT_BLOC, ORIENTATIONS, RIGHT_BLOC

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
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


def slugify(text: str) -> str:
    """ASCII slug for URLs: 'économie' -> 'economie', 'sciences-tech' kept."""
    import unicodedata

    ascii_text = unicodedata.normalize("NFD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")


def _fmt_dt(iso: str | None, fmt: str = "%d/%m %H:%M") -> str:
    """Paris-time formatter; %a is rendered as a French weekday (the CI
    runner's C locale would otherwise print English day names)."""
    if not iso:
        return ""
    dt = datetime.fromisoformat(iso).astimezone(_PARIS)
    return dt.strftime(fmt.replace("%a", _FRENCH_DAYS[dt.weekday()]))


def _rfc822(iso: str | None) -> str:
    """ISO timestamp -> RFC 822 date (RSS requirement)."""
    from email.utils import format_datetime

    if not iso:
        return ""
    return format_datetime(datetime.fromisoformat(iso))


def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=select_autoescape(["html", "xml"]),
    )
    env.filters["fmt_dt"] = _fmt_dt
    env.filters["rfc822"] = _rfc822
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
                "category_slug": slugify(row["category"]) if row["category"] else None,
                "updated_at": row["updated_at"],
                "created_at": row["created_at"],
                "suspect_merge": bool(row["suspect_merge"]),
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
            {(r["source_name"], r["orientation"], r["editorial_style"], r["paywall"],
              r["source_id"]) for r in rows},
            key=lambda t: (ORIENTATIONS.index(t[1]), EDITORIAL_STYLES.index(t[2]), t[0]),
        )
        card["sources"] = [
            {"name": n, "orientation": o, "editorial_style": style, "paywall": pw, "id": sid}
            for n, o, style, pw, sid in card["sources"]
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
    pages = list(site_dir.rglob("*.html")) + list(site_dir.rglob("*.xml"))
    html = " ".join(p.read_text(encoding="utf-8") for p in pages)
    return [s for s in summaries if s[15:60] in html]


def build_site(conn: sqlite3.Connection, out_dir: Path) -> dict[str, int]:
    """Generate the whole static site into out_dir. Returns page counts."""
    env = _env()
    # Re-clustering renumbers events: purge previously rendered detail and
    # archive pages so a local `serve` never exposes stale orphans. (CI always
    # starts from a clean checkout; this matters for local browsing.)
    for stale_dir in (out_dir / "cluster", out_dir / "archives", out_dir / "categorie", out_dir / "source"):
        if stale_dir.is_dir():
            for old in stale_dir.glob("*.html"):
                old.unlink()
    og_dir = out_dir / "og"
    if og_dir.is_dir():
        for old in og_dir.glob("*.png"):
            old.unlink()
    (out_dir / "cluster").mkdir(parents=True, exist_ok=True)
    shutil.copy(TEMPLATES_DIR / "style.css", out_dir / "style.css")
    favicon_ids: set[str] = set()
    if STATIC_DIR.is_dir():
        shutil.copytree(STATIC_DIR, out_dir / "static", dirs_exist_ok=True)
        favicon_ids = {p.stem for p in (STATIC_DIR / "favicons").glob("*.png")}

    now = datetime.now(timezone.utc)
    base_ctx = {
        "generated_at": now.astimezone(_PARIS).strftime("%d/%m/%Y %H:%M (%Z)"),
        "repo_url": REPO_URL,
        "site_url": SITE_BASE_URL,
        "health": db.feed_health(conn),
        "favicon_ids": favicon_ids,
    }

    written_pages: list[str] = []

    def write_page(rel_path: str, template: str, root: str, **ctx) -> None:
        """Render one page; records it for the sitemap and sets canonical."""
        html = env.get_template(template).render(
            **base_ctx, root=root, page_path=rel_path, **ctx
        )
        target = out_dir / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(html, encoding="utf-8")
        written_pages.append(rel_path)

    feed_since = (now - timedelta(hours=FEED_WINDOW_HOURS)).isoformat(timespec="seconds")
    week_since = (now - timedelta(days=BLINDSPOT_WINDOW_DAYS)).isoformat(timespec="seconds")
    feed_cards = [
        c for c in build_cards(conn, feed_since, FEED_MIN_MEMBERS)
        if c["n_sources"] >= FEED_MIN_SOURCES
    ]
    week_cards = build_cards(conn, week_since, BLINDSPOT_MIN_MEMBERS)
    blind_cards = [c for c in week_cards if c["blindspot_for"]]
    blind_cards.sort(key=lambda c: (-abs(c["blindspot_score"]), -c["n_members"]))

    # Category navigation: one static page per category present in the feed.
    categories: dict[str, dict] = {}
    for c in feed_cards:
        if c["category"]:
            entry = categories.setdefault(
                c["category_slug"], {"name": c["category"], "slug": c["category_slug"], "cards": []}
            )
            entry["cards"].append(c)
    cat_nav = sorted(
        ({"name": v["name"], "slug": v["slug"], "count": len(v["cards"])} for v in categories.values()),
        key=lambda x: -x["count"],
    )
    (out_dir / "categorie").mkdir(exist_ok=True)
    for entry in categories.values():
        write_page(
            f"categorie/{entry['slug']}.html", "categorie.html", root="../",
            category=entry["name"], clusters=entry["cards"],
            cat_nav=cat_nav, active_slug=entry["slug"],
            og_title=f"{entry['name'].capitalize()} — Spectre",
            og_description=f"La couverture {entry['name']} des dernières 48 h,"
                           " par orientation des sources.",
        )

    write_page(
        "index.html", "index.html", root="",
        clusters=feed_cards, cat_nav=cat_nav,
        active_page="index", overview=_cards_overview(feed_cards),
            og_title="Spectre — qui couvre quoi dans la presse française",
            og_description=(
                "Agrégateur d'actualité française : couverture par orientation"
                " politique, contrastes de cadrage et angles morts médiatiques."
            ),
    )

    # Sport and faits-divers one-sidedness is structural (left outlets barely
    # cover them), not an editorial blindspot: keep them out of the columns.
    from .categorize import STRUCTURAL_CATEGORIES

    # A blindspot seen on a cluster younger than 48h is provisional: the
    # other side may simply be hours late. It firms up over the 7-day window.
    fresh_cutoff = (now - timedelta(hours=48)).isoformat(timespec="seconds")
    for c in blind_cards:
        c["provisional"] = bool(c["created_at"] and c["created_at"] >= fresh_cutoff)
    editorial = [c for c in blind_cards if c["category"] not in STRUCTURAL_CATEGORIES]
    structural = [c for c in blind_cards if c["category"] in STRUCTURAL_CATEGORIES]

    write_page(
        "blindspots.html", "blindspots.html", root="",
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
    )

    # Main RSS feed: 30 most recent events with >= 3 articles. Descriptions
    # are OUR generated stats (+ divergent terms) — never press content.
    feed_items = sorted(
        (c for c in feed_cards if c["n_members"] >= 3 and not c["suspect_merge"]),
        key=lambda c: c["updated_at"] or "", reverse=True,
    )[:30]
    for item in feed_items:
        payloads = db.get_analyses(conn, item["id"])
        vocab = json.loads(payloads["vocab_contrast"]) if "vocab_contrast" in payloads else {}
        item["terms_left"] = [t for t, _ in vocab.get("left_terms", [])[:3]]
        item["terms_right"] = [t for t, _ in vocab.get("right_terms", [])[:3]]
    # Last COMPLETED week's archive gets its own feed item when it exists.
    from .archive import current_week_id, load_snapshots

    past_weeks = [s for s in load_snapshots() if s["week"] < current_week_id()]
    archive_item = None
    if past_weeks:
        snap = past_weeks[0]
        archive_item = {
            "week": snap["week"],
            "generated_at": snap["generated_at"],
            "n_clusters": len(snap["clusters"]),
        }
    (out_dir / "feed.xml").write_text(
        env.get_template("feed.xml").render(
            items=feed_items, site_url=SITE_BASE_URL, archive_item=archive_item,
        ),
        encoding="utf-8",
    )

    # Outgoing RSS feed of editorial blindspots (an aggregator you can
    # subscribe to). Items link to the representative ORIGINAL article.
    rss_items = []
    for c in [b for b in blind_cards if not b["suspect_merge"]][:30]:
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
    snapshots = load_snapshots()
    (out_dir / "archives").mkdir(exist_ok=True)
    for snap in snapshots:
        write_page(
            f"archives/{snap['week']}.html", "archive_week.html", root="../",
            snap=snap, active_page="archives",
            og_title=f"Archives {snap['week']} — Spectre",
            og_description="Instantané hebdomadaire : les événements et leurs"
                           " couvertures par bord.",
        )
    write_page(
        "archives.html", "archives.html", root="",
        snapshots=snapshots, active_page="archives",
        og_title="Archives — Spectre",
        og_description="La mémoire du spectre médiatique, semaine par semaine.",
    )

    write_page(
        "stats.html", "stats.html", root="",
        stats=db.public_stats(conn), active_page="stats",
        og_title="Statistiques — Spectre",
        og_description="Transparence opérationnelle : santé des flux, volumes,"
                       " état du pipeline.",
    )

    write_page(
        "a-propos.html", "apropos.html", root="",
        sources=db.all_sources(conn), active_page="about",
        og_title="À propos — Spectre",
        og_description="Méthodologie, référentiel d'orientations (indicatif et"
                       " débattable) et limites connues du projet.",
    )

    # Detail pages: every cluster reachable from the feed or blindspot pages.
    detail_cards = {c["id"]: c for c in feed_cards}
    detail_cards.update({c["id"]: c for c in blind_cards})
    for card in detail_cards.values():
        analyses = {
            k: json.loads(v) for k, v in db.get_analyses(conn, card["id"]).items()
        }
        members = db.cluster_members_detail(conn, card["id"])
        by_orientation = [
            (o, [m for m in members if m["orientation"] == o]) for o in ORIENTATIONS
        ]
        # First to publish: only articles carrying a REAL publication date
        # qualify (Le Parisien's feed has none — fetched_at would wrongly
        # crown it first).
        dated = [m for m in members if m["published_at"]]
        first_publisher = min(dated, key=lambda m: m["published_at"]) if dated else None
        # Same-owner concentration: >= 2 distinct outlets of this cluster
        # sharing one owner is worth surfacing (placeholder owners excluded).
        owner_groups: dict[str, set[str]] = {}
        for m in members:
            if m["owner"] and m["owner"] != "-":
                owner_groups.setdefault(m["owner"], set()).add(m["source_name"])
        shared_owners = sorted(
            ((owner, sorted(names)) for owner, names in owner_groups.items()
             if len(names) >= 2),
            key=lambda t: -len(t[1]),
        )
        ctx = {
            **card,
            "by_orientation": [(o, ms) for o, ms in by_orientation if ms],
            "first_publisher": first_publisher,
            "shared_owners": shared_owners,
            "vocab": _vocab_view(analyses["vocab_contrast"]) if "vocab_contrast" in analyses else None,
            "ollama": analyses.get("ollama"),
        }
        og_image.generate_card(card, og_dir / f"{card['id']}.png")
        write_page(
            f"cluster/{card['id']}.html", "cluster.html", root="../",
            c=ctx, og_type="article", active_page="index",
            og_title=f"{card['title']} — couverture comparée",
            og_description=_og_coverage(card["counts"], card["n_members"]),
            og_image=f"{SITE_BASE_URL}og/{card['id']}.png",
        )

    # "Mon spectre" data: active sources + this week's labeled blindspots
    # with their covering sources (all our own metrics).
    (out_dir / "data").mkdir(exist_ok=True)
    active_sources = [
        {"id": p["id"], "name": p["name"], "orientation": p["orientation"],
         "style": p["editorial_style"]}
        for p in db.source_profiles(conn)
    ]
    (out_dir / "data" / "sources.json").write_text(
        json.dumps(active_sources, ensure_ascii=False), encoding="utf-8"
    )
    blindspot_rows = [
        {
            "id": c["id"],
            "title": c["title"],
            "blindspot_for": c["blindspot_for"],
            "sources": [s["id"] for s in c["sources"]],
            "counts": c["counts"],
        }
        for c in blind_cards
    ]
    (out_dir / "data" / "blindspots.json").write_text(
        json.dumps(blindspot_rows, ensure_ascii=False), encoding="utf-8"
    )
    write_page(
        "mon-spectre.html", "mon_spectre.html", root="",
        active_page="monspectre",
        og_title="Mon spectre — Spectre",
        og_description="Cochez les médias que vous lisez : position indicative"
                       " sur le spectre et blindspots que ce régime de lecture"
                       " aurait ratés. Rien ne quitte votre navigateur.",
    )

    # Static source profiles (the honest answer to "source profiles").
    import yaml as yaml_mod

    raw_cfg = yaml_mod.safe_load(
        (Path("config/sources.yaml")).read_text(encoding="utf-8")
    ) if Path("config/sources.yaml").exists() else {"sources": []}
    notes = {str(e.get("id")): e.get("classification_note") for e in raw_cfg["sources"]}
    live_by_source: dict[str, list[dict]] = {}
    for c in detail_cards.values():
        for s in c["sources"]:
            live_by_source.setdefault(s["id"], []).append(c)
    for profile in db.source_profiles(conn):
        subjects = sorted(
            live_by_source.get(profile["id"], []),
            key=lambda c: c["updated_at"] or "", reverse=True,
        )[:10]
        write_page(
            f"source/{profile['id']}.html", "source.html", root="../",
            s=profile, note=notes.get(profile["id"]), subjects=subjects,
            og_title=f"{profile['name']} — profil de source | Spectre",
            og_description=f"Classement source-level, propriétaire et activité"
                           f" de {profile['name']} dans Spectre.",
        )

    # Client-side search index: ONLY clusters that still have a live page
    # (titles + our metrics — never press content beyond the title).
    search_rows = [
        {
            "id": c["id"],
            "t": c["title"],
            "d": c["updated_at"],
            "cat": c["category"],
            "s": c["n_sources"],
            "m": c["n_members"],
        }
        for c in detail_cards.values()
    ]
    (out_dir / "data").mkdir(exist_ok=True)
    (out_dir / "data" / "index.json").write_text(
        json.dumps(search_rows, ensure_ascii=False), encoding="utf-8"
    )
    write_page(
        "recherche.html", "recherche.html", root="",
        active_page="recherche",
        og_title="Recherche — Spectre",
        og_description="Recherche dans les événements couverts, côté navigateur"
                       " uniquement — aucune requête ne quitte votre machine.",
    )

    # SEO artifacts: sitemap from every written page, robots.txt, favicon.
    sitemap_entries = "\n".join(
        f"  <url><loc>{SITE_BASE_URL}{rel}</loc></url>" for rel in sorted(written_pages)
    )
    (out_dir / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{sitemap_entries}\n</urlset>\n",
        encoding="utf-8",
    )
    (out_dir / "robots.txt").write_text(
        f"User-agent: *\nAllow: /\nSitemap: {SITE_BASE_URL}sitemap.xml\n",
        encoding="utf-8",
    )
    shutil.copy(TEMPLATES_DIR / "favicon.svg", out_dir / "favicon.svg")

    stats = {"feed": len(feed_cards), "blindspots": len(blind_cards), "details": len(detail_cards)}
    logger.info("site built in %s: %s", out_dir, stats)
    return stats

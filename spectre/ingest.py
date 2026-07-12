"""RSS ingestion: fetch feeds, normalize entries, deduplicate, store.

Deliberate constraint: we only keep what the feed itself exposes (title +
description). No article scraping — cleaner legally (droits voisins) and
sufficient for framing analysis.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser
import httpx
import yaml
from bs4 import BeautifulSoup

from . import db
from .models import Article, Source

logger = logging.getLogger(__name__)

# ASCII only: HTTP header values cannot carry accented characters.
USER_AGENT = "Spectre/0.1 (projet personnel d'analyse media)"
FETCH_TIMEOUT = 20.0

# Query parameters that only exist for tracking; stripped during URL
# canonicalization so the same article shared through two channels dedups.
_TRACKING_PREFIXES = ("utm_", "at_", "mtm_", "pk_")
_TRACKING_PARAMS = {"xtor", "fbclid", "gclid", "igshid", "yclid", "mc_cid", "mc_eid"}


def load_sources(config_path: str | Path) -> list[Source]:
    """Parse config/sources.yaml into Source objects.

    Documentation-only YAML fields (classification_note, …) are ignored: the
    dataclass only receives the fields it declares.
    """
    import dataclasses

    known = {f.name for f in dataclasses.fields(Source)}
    with open(config_path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return [
        Source(**{k: v for k, v in {**entry, "id": str(entry["id"])}.items() if k in known})
        for entry in raw["sources"]
    ]


def canonicalize_url(url: str) -> str:
    """Strip tracking query parameters and the fragment from a URL."""
    parts = urlsplit(url.strip())
    kept = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k not in _TRACKING_PARAMS and not k.lower().startswith(_TRACKING_PREFIXES)
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), ""))


def strip_html(text: str | None) -> str | None:
    """HTML fragment -> plain text with collapsed whitespace."""
    if not text:
        return None
    plain = BeautifulSoup(text, "html.parser").get_text(separator=" ")
    plain = " ".join(plain.split())
    return plain or None


def _parse_published(entry: feedparser.FeedParserDict) -> str | None:
    """RSS date -> ISO 8601 UTC, or None (feedparser parses to UTC struct_time)."""
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed:
            return datetime(*parsed[:6], tzinfo=timezone.utc).isoformat(timespec="seconds")
    return None


def normalize_entry(
    source_id: str, entry: feedparser.FeedParserDict, fetched_at: str
) -> Article | None:
    """RSS entry -> Article, or None if it lacks a title or link."""
    title = strip_html(entry.get("title"))
    link = entry.get("link")
    if not title or not link:
        return None
    return Article(
        source_id=source_id,
        title=title,
        url=canonicalize_url(link),
        guid=entry.get("id") or None,
        summary=strip_html(entry.get("summary") or entry.get("description")),
        published_at=_parse_published(entry),
        fetched_at=fetched_at,
    )


def ingest_feed(
    conn: sqlite3.Connection, client: httpx.Client, source: Source, feed_url: str
) -> int:
    """Fetch one feed and store its new entries. Returns the new-article count.

    Never raises: any failure is logged to fetch_log and swallowed so one
    dead feed cannot abort the run.
    """
    fetched_at = db.utcnow_iso()
    try:
        resp = client.get(feed_url)
    except httpx.HTTPError as exc:
        logger.warning("%s: fetch failed: %s", feed_url, exc)
        db.log_fetch(conn, source.id, feed_url, "http_error", error=str(exc))
        return 0
    if resp.status_code != 200:
        logger.warning("%s: HTTP %d", feed_url, resp.status_code)
        db.log_fetch(conn, source.id, feed_url, "http_error", http_code=resp.status_code)
        return 0

    parsed = feedparser.parse(resp.content)
    if parsed.bozo and not parsed.entries:
        logger.warning("%s: parse error: %s", feed_url, parsed.bozo_exception)
        db.log_fetch(
            conn, source.id, feed_url, "parse_error",
            http_code=resp.status_code, error=str(parsed.bozo_exception),
        )
        return 0

    n_new = 0
    for entry in parsed.entries:
        article = normalize_entry(source.id, entry, fetched_at)
        if article and db.insert_article(conn, article):
            n_new += 1
    conn.commit()
    db.log_fetch(
        conn, source.id, feed_url, "ok",
        http_code=resp.status_code, n_entries=len(parsed.entries), n_new=n_new,
    )
    logger.info("%s: %d entries, %d new", feed_url, len(parsed.entries), n_new)
    return n_new


def ingest_all(conn: sqlite3.Connection, sources: list[Source]) -> int:
    """Ingest every active source; returns the total new-article count."""
    active = [s for s in sources if s.active]
    total_new = 0
    with httpx.Client(
        timeout=FETCH_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        for source in active:
            for feed_url in source.rss:
                total_new += ingest_feed(conn, client, source, feed_url)
    logger.info("ingest done: %d new articles across %d sources", total_new, len(active))
    return total_new


def pipeline_health_check(fetch_rows: list) -> str | None:
    """Free-tier alerting: return an error message when the run looks broken.

    The CI step fails on it, and GitHub e-mails the failure. Signal: more than
    25% of feeds failing to fetch/parse — a reliable sign of real breakage.

    We deliberately do NOT alert on "0 new articles": the DB is committed after
    every run, so a fresh runner routinely re-ingests only duplicates and sees
    zero new — that is normal, not a failure.
    """
    if not fetch_rows:
        return "ALERTE : aucun flux tenté — pipeline cassé."
    ko = sum(1 for r in fetch_rows if r["status"] != "ok")
    if ko / len(fetch_rows) > 0.25:
        return (
            f"ALERTE : {ko}/{len(fetch_rows)} flux en échec (> 25 %) — "
            "vérifier fetch_log."
        )
    return None

"""Weekly archive snapshots — the project's long-term memory.

The DB purges articles after 30 days and cluster pages die with the feed, so
patterns ("what did each side ignore this month?") would be lost. Every
pipeline run (re)writes a small JSON snapshot of the CURRENT ISO week into
data/archive/; past weeks stay frozen. The JSON files are committed to the
repo (titles, links to originals and our own metrics only — no press content)
and rendered as static archive pages.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db

logger = logging.getLogger(__name__)

ARCHIVE_DIR = Path("data/archive")
MAX_CLUSTERS_PER_WEEK = 40
SNAPSHOT_MIN_MEMBERS = 3


def current_week_id(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    year, week, _ = now.isocalendar()
    return f"{year}-W{week:02d}"


def week_start(now: datetime | None = None) -> str:
    """ISO timestamp of Monday 00:00 UTC of the current ISO week."""
    now = now or datetime.now(timezone.utc)
    monday = now - timedelta(days=now.isocalendar().weekday - 1)
    return monday.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(
        timespec="seconds"
    )


def write_snapshot(conn: sqlite3.Connection, archive_dir: Path = ARCHIVE_DIR) -> Path:
    """(Re)write the snapshot of the current ISO week. Returns the file path."""
    from .render import build_cards  # late import: render also imports archive

    cards = build_cards(conn, week_start(), SNAPSHOT_MIN_MEMBERS)[:MAX_CLUSTERS_PER_WEEK]
    entries = []
    for card in cards:
        top = db.cluster_top_article(conn, card["id"])
        entries.append({
            "title": card["title"],
            "url": top["url"] if top else None,  # representative original article
            "n_members": card["n_members"],
            "n_sources": card["n_sources"],
            "counts": card["counts"],
            "style_counts": card["style_counts"],
            "blindspot_score": card["blindspot_score"],
            "blindspot_for": card["blindspot_for"],
            "divergence": card["divergence"],
            "category": card["category"],
        })
    snapshot = {
        "week": current_week_id(),
        "generated_at": db.utcnow_iso(),
        "clusters": entries,
    }
    archive_dir.mkdir(parents=True, exist_ok=True)
    path = archive_dir / f"{snapshot['week']}.json"
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=1), encoding="utf-8")
    logger.info("archive snapshot %s: %d clusters", snapshot["week"], len(entries))
    return path


def load_snapshots(archive_dir: Path = ARCHIVE_DIR) -> list[dict]:
    """All stored snapshots, most recent week first."""
    if not archive_dir.is_dir():
        return []
    snaps = [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted(archive_dir.glob("*.json"), reverse=True)
    ]
    return snaps

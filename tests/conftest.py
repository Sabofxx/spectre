"""Shared fixtures: in-memory DB with a small source referential."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from spectre import db as dbmod
from spectre.models import Article, Source

SOURCES = [
    Source(id="g1", name="Gauche 1", orientation="gauche", owner="-"),
    Source(id="g2", name="Gauche 2", orientation="gauche", owner="-"),
    Source(id="cg1", name="CGauche 1", orientation="centre-gauche", owner="-"),
    Source(id="cg2", name="CGauche 2", orientation="centre-gauche", owner="-"),
    Source(id="c1", name="Centre 1", orientation="centre", owner="-"),
    Source(id="cd1", name="CDroit 1", orientation="centre-droit", owner="-"),
    Source(id="cd2", name="CDroit 2", orientation="centre-droit", owner="-"),
    Source(id="d1", name="Droite 1", orientation="droite", owner="-"),
    Source(id="d2", name="Droite 2", orientation="droite", owner="-"),
    Source(id="d3", name="Droite 3", orientation="droite", owner="-"),
]
# Active bloc sizes with this referential: left = 4, right = 5, centre = 1.


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = dbmod.connect(":memory:")
    dbmod.sync_sources(connection, SOURCES)
    yield connection
    connection.close()


def iso_hours_ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(
        timespec="seconds"
    )


_counter = iter(range(10_000))


def make_article(
    source_id: str = "g1",
    title: str = "Un titre",
    summary: str | None = None,
    hours_ago: float = 1.0,
    url: str | None = None,
    guid: str | None = None,
) -> Article:
    n = next(_counter)
    return Article(
        source_id=source_id,
        title=title,
        url=url or f"https://example.org/{source_id}/{n}",
        guid=guid,
        summary=summary,
        published_at=iso_hours_ago(hours_ago),
        fetched_at=iso_hours_ago(0),
    )

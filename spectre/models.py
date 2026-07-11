"""Core data structures shared across modules."""

from __future__ import annotations

from dataclasses import dataclass, field

ORIENTATIONS = ("gauche", "centre-gauche", "centre", "centre-droit", "droite")

# Orientation groupings used by blindspot / vocabulary contrast analyses.
LEFT_BLOC = ("gauche", "centre-gauche")
RIGHT_BLOC = ("centre-droit", "droite")


@dataclass(slots=True)
class Source:
    """A media outlet as declared in config/sources.yaml."""

    id: str
    name: str
    orientation: str
    owner: str
    rss: list[str] = field(default_factory=list)
    active: bool = True

    def __post_init__(self) -> None:
        if self.orientation not in ORIENTATIONS:
            raise ValueError(f"unknown orientation {self.orientation!r} for source {self.id!r}")


@dataclass(slots=True)
class Article:
    """A normalized RSS entry, ready for insertion."""

    source_id: str
    title: str
    url: str
    guid: str | None
    summary: str | None
    published_at: str | None  # ISO 8601 UTC
    fetched_at: str  # ISO 8601 UTC

"""Quality gates for the source classification referential."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from .models import EDITORIAL_STYLES, ORIENTATIONS, PAYWALL_LEVELS

REQUIRED_SOURCE_FIELDS = (
    "id",
    "name",
    "orientation",
    "editorial_style",
    "paywall",
    "owner",
    "rss",
    "active",
)
REQUIRED_CLASSIFICATION_FIELDS = ("reviewed_at", "scope", "basis")


def audit_sources_config(config_path: str | Path) -> dict[str, Any]:
    """Validate source classifications and return errors/warnings/counts.

    This gate does not pretend to prove subjective labels are objectively true;
    it prevents mechanical mistakes: missing fields, implicit defaults,
    duplicate ids, invalid vocabulary, and active sources without feeds.
    """
    raw = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(raw, dict):
        return {"ok": False, "errors": ["config root must be a mapping"], "warnings": []}

    classification = raw.get("classification")
    if not isinstance(classification, dict):
        errors.append("missing top-level classification review metadata")
    else:
        for field in REQUIRED_CLASSIFICATION_FIELDS:
            if not str(classification.get(field, "")).strip():
                errors.append(f"classification.{field} is required")
        if classification.get("scope") != "source-level":
            errors.append("classification.scope must be 'source-level'")

    sources = raw.get("sources")
    if not isinstance(sources, list) or not sources:
        errors.append("sources must be a non-empty list")
        return {"ok": False, "errors": errors, "warnings": warnings}

    seen_ids: set[str] = set()
    orientation_counts: Counter[str] = Counter()
    style_counts: Counter[str] = Counter()
    seen_feeds: dict[str, str] = {}
    active_count = 0

    for idx, entry in enumerate(sources, start=1):
        prefix = f"sources[{idx}]"
        if not isinstance(entry, dict):
            errors.append(f"{prefix} must be a mapping")
            continue

        sid = str(entry.get("id", "")).strip()
        label = sid or prefix
        for field in REQUIRED_SOURCE_FIELDS:
            if field not in entry:
                errors.append(f"{label}: missing required field {field}")

        if not sid:
            errors.append(f"{prefix}: id is required")
        elif sid in seen_ids:
            errors.append(f"{label}: duplicate id")
        else:
            seen_ids.add(sid)

        orientation = entry.get("orientation")
        if orientation not in ORIENTATIONS:
            errors.append(f"{label}: invalid orientation {orientation!r}")
        else:
            orientation_counts[orientation] += 1

        style = entry.get("editorial_style")
        if style not in EDITORIAL_STYLES:
            errors.append(f"{label}: invalid editorial_style {style!r}")
        else:
            style_counts[style] += 1

        if entry.get("paywall") not in PAYWALL_LEVELS:
            errors.append(f"{label}: invalid paywall {entry.get('paywall')!r}")

        if not str(entry.get("name", "")).strip():
            errors.append(f"{label}: name is required")
        if not str(entry.get("owner", "")).strip():
            errors.append(f"{label}: owner is required")

        active = entry.get("active")
        if not isinstance(active, bool):
            errors.append(f"{label}: active must be a boolean")
        elif active:
            active_count += 1

        rss = entry.get("rss")
        if not isinstance(rss, list):
            errors.append(f"{label}: rss must be a list")
        else:
            for url in rss:
                if not isinstance(url, str) or not url.strip():
                    errors.append(f"{label}: rss entries must be non-empty strings")
                    continue
                parts = urlsplit(url)
                if parts.scheme not in {"http", "https"} or not parts.netloc:
                    errors.append(f"{label}: invalid RSS URL {url!r}")
                    continue
                previous = seen_feeds.get(url)
                if previous and previous != label:
                    errors.append(f"{label}: duplicate RSS feed already used by {previous}")
                else:
                    seen_feeds[url] = label
            if active is True and not rss:
                errors.append(f"{label}: active source must declare at least one RSS feed")
            elif active is False and rss:
                warnings.append(f"{label}: inactive source still declares RSS feeds")

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "n_sources": len(sources),
        "n_active": active_count,
        "orientation_counts": dict(orientation_counts),
        "style_counts": dict(style_counts),
    }

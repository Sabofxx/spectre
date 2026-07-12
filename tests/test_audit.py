"""Classification referential audit."""

from spectre.audit import audit_sources_config


def test_repository_sources_config_passes_audit():
    report = audit_sources_config("config/sources.yaml")

    assert report["ok"], report["errors"]
    assert report["n_active"] > 0
    assert set(report["orientation_counts"]) == {
        "gauche",
        "centre-gauche",
        "centre",
        "centre-droit",
        "droite",
    }
    assert set(report["style_counts"]) == {"factuel", "mixte", "opinion"}


def test_audit_rejects_missing_explicit_editorial_style(tmp_path):
    config = tmp_path / "sources.yaml"
    config.write_text(
        """
classification:
  reviewed_at: "2026-07-11"
  scope: source-level
  basis: "test"
sources:
  - id: test
    name: Test
    orientation: centre
    owner: Owner
    rss: ["https://example.org/feed.xml"]
    active: true
        """,
        encoding="utf-8",
    )

    report = audit_sources_config(config)

    assert not report["ok"]
    assert "test: missing required field editorial_style" in report["errors"]


def test_audit_rejects_active_source_without_feed(tmp_path):
    config = tmp_path / "sources.yaml"
    config.write_text(
        """
classification:
  reviewed_at: "2026-07-11"
  scope: source-level
  basis: "test"
sources:
  - id: test
    name: Test
    orientation: centre
    editorial_style: factuel
    owner: Owner
    rss: []
    active: true
        """,
        encoding="utf-8",
    )

    report = audit_sources_config(config)

    assert not report["ok"]
    assert "test: active source must declare at least one RSS feed" in report["errors"]


def test_audit_rejects_invalid_and_duplicate_feeds(tmp_path):
    config = tmp_path / "sources.yaml"
    config.write_text(
        """
classification:
  reviewed_at: "2026-07-11"
  scope: source-level
  basis: "test"
sources:
  - id: one
    name: One
    orientation: centre
    editorial_style: factuel
    owner: Owner
    rss: ["https://example.org/feed.xml"]
    active: true
  - id: two
    name: Two
    orientation: centre
    editorial_style: factuel
    owner: Owner
    rss: ["https://example.org/feed.xml", "not-a-url"]
    active: true
        """,
        encoding="utf-8",
    )

    report = audit_sources_config(config)

    assert not report["ok"]
    assert "two: duplicate RSS feed already used by one" in report["errors"]
    assert "two: invalid RSS URL 'not-a-url'" in report["errors"]

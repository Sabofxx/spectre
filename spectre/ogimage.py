"""Open Graph card images (1200x630), generated with Pillow.

Social shares are the growth channel: each cluster page gets a card showing
the title, the coverage bar and the per-side source counts. Pure-wheel
constraint: Pillow only (cairosvg needs a system libcairo). Fonts: bundled
DejaVu (free license, assets/fonts/LICENSE).

Legal note: the image carries the cluster TITLE and our own metrics — the
same content as the page itself, never RSS summaries.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

FONTS_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"
WIDTH, HEIGHT = 1200, 630
MARGIN = 72

# Dark-surface variants of the validated palette (bar on dark background).
BG = (23, 22, 20)
FG = (236, 233, 226)
MUTED = (167, 164, 155)
LEFT = (226, 102, 86)
CENTRE = (157, 163, 168)
RIGHT = (90, 153, 210)


def _wrap(draw, text: str, font, max_width: int, max_lines: int = 4) -> list[str]:
    """Greedy word wrap; the last line gets an ellipsis if text overflows."""
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=font) <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
            if len(lines) == max_lines:
                break
    if current and len(lines) < max_lines:
        lines.append(current)
    if len(lines) == max_lines and " ".join(lines).count(" ") + 1 < len(words):
        lines[-1] = lines[-1].rstrip(".,;: ") + "…"
    return lines


def generate_card(card: dict, out_path: Path) -> None:
    """Render one OG image for a cluster card dict (title/counts/n_*)."""
    from PIL import Image, ImageDraw, ImageFont

    bold = ImageFont.truetype(str(FONTS_DIR / "DejaVuSans-Bold.ttf"), 58)
    small = ImageFont.truetype(str(FONTS_DIR / "DejaVuSans.ttf"), 30)
    brand = ImageFont.truetype(str(FONTS_DIR / "DejaVuSans-Bold.ttf"), 34)

    img = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(img)

    draw.text((MARGIN, 56), "SPECTRE", font=brand, fill=FG)
    draw.text((MARGIN + 232, 62), "presse française — couverture comparée",
              font=ImageFont.truetype(str(FONTS_DIR / "DejaVuSans.ttf"), 24), fill=MUTED)

    title_lines = _wrap(draw, card["title"], bold, WIDTH - 2 * MARGIN)
    y = 150
    for line in title_lines:
        draw.text((MARGIN, y), line, font=bold, fill=FG)
        y += 74

    counts = card["counts"]
    total = max(counts["left"] + counts["centre"] + counts["right"], 1)
    bar_y, bar_h, gap = 480, 30, 6
    bar_w = WIDTH - 2 * MARGIN
    x = float(MARGIN)
    for value, color in ((counts["left"], LEFT), (counts["centre"], CENTRE),
                         (counts["right"], RIGHT)):
        if not value:
            continue
        seg = (bar_w - 2 * gap) * value / total
        draw.rounded_rectangle((x, bar_y, x + seg, bar_y + bar_h), radius=6, fill=color)
        x += seg + gap

    caption = (f"{counts['left']} gauche · {counts['centre']} centre · "
               f"{counts['right']} droite — {card['n_members']} articles, "
               f"{card['n_sources']} sources")
    draw.text((MARGIN, bar_y + bar_h + 22), caption, font=small, fill=MUTED)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, format="PNG", optimize=True)


def generate_all(cards: list[dict], out_dir: Path) -> int:
    """Generate one image per card into out_dir; returns the count."""
    for card in cards:
        generate_card(card, out_dir / f"{card['id']}.png")
    logger.info("og images: %d generated", len(cards))
    return len(cards)

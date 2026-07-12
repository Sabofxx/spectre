"""One-shot favicon fetcher — run LOCALLY, results are committed.

Downloads each active source's favicon once into static/favicons/{id}.png.
Visitors never hit any external service: the site serves the committed
copies. Standard aggregator practice: favicons are used purely to identify
the linked outlet.

Usage: .venv/bin/python scripts/fetch_favicons.py
"""

from __future__ import annotations

import io
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from spectre.ingest import load_sources  # noqa: E402

OUT_DIR = Path(__file__).resolve().parent.parent / "static" / "favicons"
SIZE = 32

# Feed hosts that differ from the public site host.
HOST_OVERRIDES = {
    "blast": "www.blast-info.fr",
    "lobs": "www.nouvelobs.com",
    "leparisien": "www.leparisien.fr",
}


def favicon_host(source) -> str:
    if source.id in HOST_OVERRIDES:
        return HOST_OVERRIDES[source.id]
    return urlsplit(source.rss[0]).netloc


def to_png(raw: bytes) -> bytes | None:
    """Normalize whatever we got (ico/png/jpg) to a small PNG."""
    from PIL import Image

    try:
        img = Image.open(io.BytesIO(raw))
        img = img.convert("RGBA")
        img.thumbnail((SIZE, SIZE))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return None


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    sources = [s for s in load_sources("config/sources.yaml") if s.active]
    ok, ko = 0, []
    with httpx.Client(timeout=15, follow_redirects=True,
                      headers={"User-Agent": "Spectre/0.1 (favicon fetch, one-time)"}) as client:
        for source in sources:
            host = favicon_host(source)
            png = None
            for url in (
                f"https://{host}/favicon.ico",
                f"https://www.google.com/s2/favicons?domain={host}&sz={SIZE}",
            ):
                try:
                    resp = client.get(url)
                    if resp.status_code == 200 and resp.content:
                        png = to_png(resp.content)
                        if png:
                            break
                except httpx.HTTPError:
                    continue
            if png:
                (OUT_DIR / f"{source.id}.png").write_bytes(png)
                ok += 1
            else:
                ko.append(source.id)
    print(f"{ok} favicons récupérés ; échecs : {ko or 'aucun'}")


if __name__ == "__main__":
    main()

"""Progressive Web App assets: manifest, service worker, and app icons.

The site stays a plain static bundle; this only adds an installable shell and
offline reading of already-visited pages. No personal data is stored — the
service worker caches responses, nothing else — so the zero-tracking pledge
holds. Icons reuse the site's signature: the political spectrum as three bars
(left / centre / right) on the dark surface, drawn with Pillow (pure-wheel,
same constraint as the OG cards).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from PIL import Image, ImageDraw

logger = logging.getLogger(__name__)

# Dark-surface palette (matches ogimage / the site's dark theme).
BG = (23, 22, 20)
LEFT = (226, 102, 86)
CENTRE = (157, 163, 168)
RIGHT = (90, 153, 210)


def _icon(size: int, safe: float = 1.0) -> Image.Image:
    """The spectrum mark: three vertical bars centred in a `safe` fraction of
    the canvas (safe < 1 leaves room for maskable icons' circular crop)."""
    img = Image.new("RGB", (size, size), BG)
    draw = ImageDraw.Draw(img)
    band = size * safe
    off = (size - band) / 2
    gap = band * 0.045
    bar_w = (band - 2 * gap) / 3
    top = off + band * 0.16
    bottom = off + band - band * 0.16
    radius = bar_w * 0.32
    for i, color in enumerate((LEFT, CENTRE, RIGHT)):
        x0 = off + i * (bar_w + gap)
        draw.rounded_rectangle(
            [x0, top, x0 + bar_w, bottom], radius=radius, fill=color
        )
    return img


def write_pwa(out_dir: Path, site_base_url: str) -> None:
    """Emit icons/, manifest.webmanifest and sw.js into the built site."""
    icons_dir = out_dir / "icons"
    icons_dir.mkdir(parents=True, exist_ok=True)
    _icon(192).save(icons_dir / "icon-192.png")
    _icon(512).save(icons_dir / "icon-512.png")
    _icon(512, safe=0.72).save(icons_dir / "icon-maskable-512.png")

    manifest = {
        "name": "Spectre — presse française",
        "short_name": "Spectre",
        "description": "Qui couvre quoi dans la presse française :"
                       " couverture par orientation politique des sources.",
        "start_url": "./index.html",
        "scope": "./",
        "display": "standalone",
        "orientation": "portrait-primary",
        "lang": "fr",
        "dir": "ltr",
        "background_color": "#171715",
        "theme_color": "#171715",
        "categories": ["news", "politics"],
        "icons": [
            {"src": "icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": "icons/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
    }
    (out_dir / "manifest.webmanifest").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    (out_dir / "sw.js").write_text(_SERVICE_WORKER, encoding="utf-8")
    logger.info("PWA assets written (manifest, service worker, 3 icons)")


# Bump CACHE on any change to the precached shell so clients refresh.
_SERVICE_WORKER = """\
'use strict';
const CACHE = 'spectre-v1';
const CORE = [
  './', './index.html', './style.css', './manifest.webmanifest',
  './favicon.svg', './icons/icon-192.png', './icons/icon-512.png',
  './data/index.json'
];

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE)
      .then((c) => c.addAll(CORE))
      .then(() => self.skipWaiting())
      .catch(() => {})
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // Pages: network-first (fresh feed), fall back to cache, then the shell.
  if (req.mode === 'navigate') {
    e.respondWith(
      fetch(req)
        .then((res) => {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy));
          return res;
        })
        .catch(() => caches.match(req).then((r) => r || caches.match('./index.html')))
    );
    return;
  }

  // Assets: cache-first, populate on miss.
  e.respondWith(
    caches.match(req).then((cached) =>
      cached || fetch(req).then((res) => {
        if (res && res.ok) {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy));
        }
        return res;
      })
    )
  );
});
"""

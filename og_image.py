"""Glassbox OG image generator.

Renders a 1200×630 PNG matching the cockpit aesthetic:
  - deep space-black background with starfield + warm gold accent
  - "GLASSBOX" wordmark with cockpit pulse-ring brand mark
  - tagline + 3 live KPI rectangles pulled from the dashboard summary

Result is cached in memory (one PNG, regenerated every 30min) so each
OG fetch is ~1ms. Output is a static URL: /og-image.png.
"""
from __future__ import annotations
import io
import os
import random
import time
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

# In-memory cache — single PNG, refreshed periodically.
_CACHE: dict = {"png": None, "fetched_at": 0.0}
_TTL_SEC = 30 * 60   # regenerate every 30min so KPIs stay fresh


def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    """Find a system font we can rely on. Falls back to PIL default."""
    candidates = [
        "/System/Library/Fonts/Helvetica.ttc",      # macOS
        "/System/Library/Fonts/SFNS.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for p in candidates:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _render(stats: Optional[dict] = None) -> bytes:
    """Render the OG image with optional live stats."""
    W, H = 1200, 630
    GOLD       = (255, 181, 71)
    GOLD_DIM   = (180, 130, 50)
    CYAN       = (127, 208, 255)
    TEXT       = (233, 236, 242)
    TEXT_DIM   = (122, 130, 144)
    SPACE      = (5, 8, 13)
    VOID       = (2, 4, 10)

    img = Image.new("RGB", (W, H), VOID)
    d = ImageDraw.Draw(img)

    # Subtle background gradient (vignette toward edges)
    for y in range(H):
        # interpolate VOID -> SPACE -> VOID by distance from center
        t = abs((y - H / 2) / (H / 2))
        r = int(VOID[0] + (SPACE[0] - VOID[0]) * (1 - t * 0.7))
        g = int(VOID[1] + (SPACE[1] - VOID[1]) * (1 - t * 0.7))
        b = int(VOID[2] + (SPACE[2] - VOID[2]) * (1 - t * 0.7))
        d.line([(0, y), (W, y)], fill=(r, g, b))

    # Starfield (deterministic seed so identical regenerates look identical)
    rng = random.Random(42)
    for _ in range(160):
        x, y = rng.randint(0, W), rng.randint(0, H)
        bright = rng.randint(80, 220)
        size = rng.choice([1, 1, 1, 2])
        d.ellipse((x, y, x + size, y + size), fill=(bright, bright, bright))

    # Top gold rule
    d.rectangle((0, 0, W, 4), fill=GOLD)

    # Brand mark — gold sphere + outer ring
    cx, cy = 110, 175
    d.ellipse((cx - 28, cy - 28, cx + 28, cy + 28), outline=GOLD_DIM, width=2)
    d.ellipse((cx - 18, cy - 18, cx + 18, cy + 18), fill=GOLD)
    d.ellipse((cx - 11, cy - 11, cx + 11, cy + 11), fill=SPACE)

    # GLASSBOX wordmark
    title_font = _font(108, bold=True)
    d.text((170, 105), "GLASS", fill=TEXT, font=title_font)
    # measure GLASS to position BOX
    bb = d.textbbox((170, 105), "GLASS", font=title_font)
    box_x = bb[2]
    d.text((box_x, 105), "BOX", fill=GOLD, font=title_font)

    # Subtitle
    sub_font = _font(28)
    d.text((175, 240),
           "Live OSINT cockpit · algorithm-derived spatial intelligence",
           fill=TEXT_DIM, font=sub_font)

    # KPI strip
    kpi_y = 340
    kpi_h = 130
    cards = []
    if stats:
        cards = [
            ("CRITICAL",     str(stats.get("critical", 0)), (255, 91, 91)),
            ("OPEN CASES",   str(stats.get("open_cases", 0)), GOLD),
            ("SIGNALS",      str(stats.get("signals", 0)), TEXT),
            ("INGESTERS",    f"{stats.get('ingesters_ok',0)}/{stats.get('ingesters_total',0)}", CYAN),
        ]
    else:
        cards = [
            ("CRITICAL", "—", (255, 91, 91)),
            ("OPEN CASES", "—", GOLD),
            ("SIGNALS", "—", TEXT),
            ("INGESTERS", "—", CYAN),
        ]
    card_w = (W - 175 * 2) // 4
    for i, (label, value, color) in enumerate(cards):
        x0 = 175 + i * card_w
        x1 = x0 + card_w - 12
        # outline
        d.rectangle((x0, kpi_y, x1, kpi_y + kpi_h),
                    outline=(40, 48, 60), width=1)
        # top accent line
        d.rectangle((x0, kpi_y, x1, kpi_y + 2), fill=GOLD_DIM)
        # label
        d.text((x0 + 16, kpi_y + 14), label, fill=TEXT_DIM, font=_font(14, bold=True))
        # value
        val_font = _font(56, bold=True)
        d.text((x0 + 16, kpi_y + 38), value, fill=color, font=val_font)

    # Bottom callouts
    bot_y = 530
    d.text((175, bot_y),
           "Sanctioned vessels · Restricted airspace · Shadow fleet · Country intel heat",
           fill=TEXT_DIM, font=_font(20))
    d.text((175, bot_y + 32),
           "mewrcreate.com",
           fill=GOLD, font=_font(22, bold=True))

    # Bottom gold rule
    d.rectangle((0, H - 4, W, H), fill=GOLD)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


async def get_og_png(stats: Optional[dict] = None) -> bytes:
    """Return PNG bytes; cached for 30 min."""
    now = time.time()
    if _CACHE["png"] and (now - _CACHE["fetched_at"]) < _TTL_SEC:
        return _CACHE["png"]
    png = _render(stats)
    _CACHE["png"] = png
    _CACHE["fetched_at"] = now
    return png


def reset_cache() -> None:
    _CACHE["png"] = None
    _CACHE["fetched_at"] = 0.0

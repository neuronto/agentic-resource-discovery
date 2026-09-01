#!/usr/bin/env python3
"""Render the contact address as a raster image.

Written as pixels rather than text on purpose. An address in markup, in an SVG
`<text>` element, or in a `mailto:` link is harvested by anything that reads the
page; a raster has to be looked at. This is friction, not protection: it stops
the scrapers that read HTML, and it does not stop anybody willing to run OCR.
That is the trade being made, deliberately, and it is why the address here is a
role account and never a person's.

    NEURONTO_CONTACT=... python3 scripts/make_contact.py    # writes web/img/contact.png
"""
from __future__ import annotations

import glob
import os
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# Read from the environment, never committed. Writing it here would put the
# address in a public repository as plain text, which is exactly what rendering
# it as pixels is meant to avoid: the image would be pointless while the source
# beside it is greppable.
ADDRESS = os.getenv("NEURONTO_CONTACT", "").strip()
OUT = Path(__file__).resolve().parent.parent / "web" / "img" / "contact.png"
SCALE = 3                       # drawn large and downsampled, so it stays crisp on any display
FG = (250, 250, 250)            # var(--fg), the footer sets its own opacity
SIZE_PT = 14


def _font(px: int):
    for pat in ("**/DejaVuSans.ttf", "**/Inter*.ttf", "**/LiberationSans-Regular.ttf",
                "**/NotoSans-Regular.ttf"):
        for root in ("/usr/share/fonts", "/System/Library/Fonts", "/Library/Fonts"):
            hits = sorted(glob.glob(f"{root}/{pat}", recursive=True))
            if hits:
                return ImageFont.truetype(hits[0], px)
    return ImageFont.load_default()


def build() -> Path:
    if not ADDRESS:
        raise SystemExit("set NEURONTO_CONTACT to the address to render")
    f = _font(SIZE_PT * SCALE)
    tmp = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    box = tmp.textbbox((0, 0), ADDRESS, font=f)
    w, h = box[2] - box[0], box[3] - box[1]
    pad = 2 * SCALE
    img = Image.new("RGBA", (w + pad * 2, h + pad * 2), (0, 0, 0, 0))
    ImageDraw.Draw(img).text((pad - box[0], pad - box[1]), ADDRESS, font=f, fill=FG + (255,))
    img = img.resize((img.width // SCALE, img.height // SCALE), Image.LANCZOS)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    img.save(OUT, "PNG", optimize=True)
    return OUT


if __name__ == "__main__":
    p = build()
    from PIL import Image as I
    im = I.open(p)
    print(f"wrote {p} {im.width}x{im.height} {p.stat().st_size}b")
    sys.exit(0)

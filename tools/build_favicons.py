"""Build the favicon set from the TEN Capital icon master.

Source:  app/web/static/ten-capital-icon.png (square, transparent background)
Output (same directory, served at /static/):
  favicon.ico (16, 32, 48)       favicon-16x16.png  favicon-32x32.png  favicon-48x48.png
  apple-touch-icon.png (180, white background)
  icon-192.png  icon-512.png     icon-maskable-512.png (safe-zone padding)  site.webmanifest

Run after replacing the master:  python tools/build_favicons.py
"""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

STATIC = Path(__file__).resolve().parents[1] / "app" / "web" / "static"
MASTER = STATIC / "ten-capital-icon.png"
THEME = "#0B1526"


def trimmed_square(image: Image.Image, margin: float) -> Image.Image:
    """Crop transparent borders, then centre on a transparent square with ``margin`` padding per side."""
    content = image.crop(image.getbbox() or (0, 0, *image.size))
    side = round(max(content.size) * (1 + 2 * margin))
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(content, ((side - content.width) // 2, (side - content.height) // 2), content)
    return canvas


def sized(image: Image.Image, size: int) -> Image.Image:
    return image.resize((size, size), Image.Resampling.LANCZOS)


def on_background(image: Image.Image, size: int, colour: tuple[int, int, int], margin: float) -> Image.Image:
    icon = sized(trimmed_square(image, margin), size)
    canvas = Image.new("RGBA", (size, size), (*colour, 255))
    canvas.alpha_composite(icon)
    return canvas.convert("RGB")


def main() -> None:
    master = Image.open(MASTER).convert("RGBA")
    tight = trimmed_square(master, 0.02)  # small sizes: use every pixel for legibility
    roomy = trimmed_square(master, 0.06)

    for size in (16, 32, 48):
        sized(tight, size).save(STATIC / f"favicon-{size}x{size}.png", optimize=True)
    sized(tight, 48).save(STATIC / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])
    on_background(master, 180, (255, 255, 255), 0.12).save(STATIC / "apple-touch-icon.png", optimize=True)
    for size in (192, 512):
        sized(roomy, size).save(STATIC / f"icon-{size}.png", optimize=True)
    on_background(master, 512, (255, 255, 255), 0.2).save(STATIC / "icon-maskable-512.png", optimize=True)

    manifest = {
        "name": "TEN Capital Fundraising Interview Simulator",
        "short_name": "TEN Interview",
        "start_url": "/",
        "display": "standalone",
        "theme_color": THEME,
        "background_color": THEME,
        "icons": [
            {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"},
            {"src": "/static/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
    }
    (STATIC / "site.webmanifest").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    for name in ("favicon.ico", "favicon-16x16.png", "favicon-32x32.png", "favicon-48x48.png", "apple-touch-icon.png",
                 "icon-192.png", "icon-512.png", "icon-maskable-512.png", "site.webmanifest"):
        print(f"wrote {name} ({(STATIC / name).stat().st_size} bytes)")


if __name__ == "__main__":
    main()

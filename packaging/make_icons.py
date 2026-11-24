"""Render the LookHop eye logo to assets/lookhop.ico, assets/logo.png and
assets/lookhop.icns (macOS app icon; generated, not committed).

    python packaging/make_icons.py
"""
import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
BLUE = (77, 163, 255)
GREEN = (76, 209, 122)


def render(size: int = 1024) -> Image.Image:
    """Draw the eye logo at `size` pixels."""
    s = size / 64  # designed on a 64px grid, rendered large and downsampled
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # rounded dark tile
    d.rounded_rectangle((0, 0, size - 1, size - 1), radius=int(14 * s), fill=(22, 24, 28, 255))
    # two "screens" behind the eye: laptop (left, blue) and monitor (right, green)
    d.rounded_rectangle((7 * s, 40 * s, 25 * s, 52 * s), radius=int(2 * s), fill=BLUE + (255,))
    d.rounded_rectangle((29 * s, 36 * s, 57 * s, 52 * s), radius=int(2 * s), fill=GREEN + (255,))
    # eye
    d.ellipse((6 * s, 10 * s, 58 * s, 38 * s), fill=(250, 250, 250, 255),
              outline=(22, 24, 28, 255), width=int(3 * s))
    d.ellipse((34 * s, 15 * s, 50 * s, 33 * s), fill=GREEN + (255,))   # iris looking right
    d.ellipse((38.5 * s, 19.5 * s, 45.5 * s, 28.5 * s), fill=(20, 20, 25, 255))
    d.ellipse((40 * s, 20 * s, 42.5 * s, 22.5 * s), fill=(255, 255, 255, 230))
    return img


def main() -> None:
    """Write logo.png, lookhop.ico and lookhop.icns (or only the icns with --icns-only)."""
    ASSETS.mkdir(exist_ok=True)
    big = render()
    if "--icns-only" in sys.argv:  # macOS build: leave the committed png/ico alone
        big.save(ASSETS / "lookhop.icns", format="ICNS")
        print("wrote", ASSETS / "lookhop.icns")
        return
    big.resize((256, 256), Image.LANCZOS).save(ASSETS / "logo.png")
    big.resize((256, 256), Image.LANCZOS).save(
        ASSETS / "lookhop.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    big.save(ASSETS / "lookhop.icns", format="ICNS")
    print("wrote", ASSETS / "logo.png", ",", ASSETS / "lookhop.ico", "and", ASSETS / "lookhop.icns")


if __name__ == "__main__":
    main()

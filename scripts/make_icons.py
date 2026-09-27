"""Regenerate the icon set from the master artwork.

    python scripts/make_icons.py [assets/borochid.png]

Writes the freedesktop hicolor PNG sizes into packaging/icons/ and the copy
bundled with the GUI. Re-encoding through Qt drops ancillary PNG chunks, so
no embedded metadata is carried into the shipped icons.
"""

import sys
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QImage

ROOT = Path(__file__).resolve().parent.parent
SIZES = (16, 22, 24, 32, 48, 64, 128, 256, 512)


def main() -> None:
    master = QImage(str(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "assets" / "borochid.png"))
    if master.isNull():
        sys.exit("cannot read master image")
    for size in SIZES:
        out = ROOT / "packaging" / "icons" / "hicolor" / f"{size}x{size}" / "apps" / "borochid.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        img = master.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        img.save(str(out), "PNG")
    bundled = ROOT / "packages" / "gui" / "src" / "borochid" / "gui" / "resources" / "borochid.png"
    master.scaled(256, 256, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation).save(str(bundled), "PNG")
    print(f"wrote {len(SIZES)} hicolor sizes and {bundled.relative_to(ROOT)}")


if __name__ == "__main__":
    main()

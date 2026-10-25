"""Recreate the multiresolution Windows icon from the repository SVG."""

import struct
from pathlib import Path


def main():
    from PySide6.QtCore import QByteArray, QBuffer, QIODevice, Qt
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer
    assets = Path(__file__).resolve().parents[1] / "assets"
    renderer = QSvgRenderer(str(assets / "ngs.svg"))
    if not renderer.isValid():
        raise ValueError("Invalid icon SVG")
    frames = []
    for size in (16, 20, 24, 32, 40, 48, 64, 96, 128, 256):
        picture = QImage(size, size, QImage.Format.Format_ARGB32)
        picture.fill(Qt.GlobalColor.transparent)
        painter = QPainter(picture)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        renderer.render(painter)
        painter.end()
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if not picture.save(buffer, "PNG"):
            raise RuntimeError("Could not encode icon")
        frames.append((size, bytes(data)))
    header = struct.pack("<HHH", 0, 1, len(frames))
    offset = 6 + 16 * len(frames)
    for size, data in frames:
        header += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    (assets / "ngs.ico").write_bytes(header + b"".join(data for size, data in frames))


if __name__ == "__main__":
    main()

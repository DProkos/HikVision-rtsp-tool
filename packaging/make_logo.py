#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Φτιάχνει το λογότυπο και το εικονίδιο της εφαρμογής (δικό μας σχέδιο).

    python make_logo.py

Γράφει:
    assets/logo.png   512x512, διαφανές φόντο
    assets/icon.ico   εικονίδιο Windows με πολλά μεγέθη (16 ... 256)

Χρειάζεται μόνο PyQt5. Αν θες άλλο σχέδιο, άλλαξε τη συνάρτηση draw_logo().
"""
import os
import struct
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")      # δεν χρειάζεται οθόνη

from PyQt5.QtCore import QBuffer, QByteArray, QIODevice, QPointF, Qt
from PyQt5.QtGui import (QBrush, QColor, QGuiApplication, QImage, QLinearGradient,
                         QPainter, QPen, QRadialGradient)

APP_DIR = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(APP_DIR, "assets")
RED = QColor("#d32f2f")


def draw_logo(size=512):
    """Κάμερα με φακό μέσα σε στρογγυλεμένο τετράγωνο + κόκκινο σημείο REC."""
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
    k = size / 512.0
    p.scale(k, k)

    # φόντο: σκούρο στρογγυλεμένο τετράγωνο με διακριτική διαβάθμιση
    bg = QLinearGradient(0, 0, 0, 512)
    bg.setColorAt(0.0, QColor("#33363c"))
    bg.setColorAt(1.0, QColor("#17181b"))
    p.setPen(Qt.NoPen)
    p.setBrush(QBrush(bg))
    p.drawRoundedRect(16, 16, 480, 480, 112, 112)

    cx, cy = 256, 268
    # εξωτερικός δακτύλιος (ασημί)
    ring = QLinearGradient(0, cy - 160, 0, cy + 160)
    ring.setColorAt(0.0, QColor("#ffffff"))
    ring.setColorAt(1.0, QColor("#aeb4bb"))
    p.setBrush(QBrush(ring))
    p.drawEllipse(QPointF(cx, cy), 160, 160)
    # κόκκινος δακτύλιος
    p.setBrush(RED)
    p.drawEllipse(QPointF(cx, cy), 132, 132)
    # σκούρος φακός
    lens = QRadialGradient(QPointF(cx - 20, cy - 25), 120)
    lens.setColorAt(0.0, QColor("#4a5160"))
    lens.setColorAt(1.0, QColor("#101114"))
    p.setBrush(QBrush(lens))
    p.drawEllipse(QPointF(cx, cy), 102, 102)
    # εσωτερικός κύκλος
    p.setBrush(QColor("#0b0c0e"))
    p.drawEllipse(QPointF(cx, cy), 56, 56)
    p.setPen(QPen(QColor(255, 255, 255, 40), 3))
    p.setBrush(Qt.NoBrush)
    p.drawEllipse(QPointF(cx, cy), 78, 78)
    # ανταύγεια
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(255, 255, 255, 200))
    p.drawEllipse(QPointF(cx - 38, cy - 42), 20, 14)
    p.setBrush(QColor(255, 255, 255, 90))
    p.drawEllipse(QPointF(cx + 30, cy + 36), 9, 9)

    # ένδειξη REC (κόκκινο σημείο με λευκό περίγραμμα) πάνω δεξιά
    p.setBrush(QColor("#ffffff"))
    p.drawEllipse(QPointF(412, 104), 34, 34)
    p.setBrush(RED)
    p.drawEllipse(QPointF(412, 104), 26, 26)
    p.end()
    return img


def png_bytes(img):
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    return bytes(ba)


def write_ico(path, sizes=(16, 24, 32, 48, 64, 128, 256)):
    """ICO με ενσωματωμένα PNG (υποστηρίζεται από Windows Vista και νεότερα)."""
    images = [(s, png_bytes(draw_logo(s))) for s in sizes]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, blobs = b"", b""
    for s, data in images:
        w = 0 if s >= 256 else s                  # 0 = 256 στο format του ICO
        entries += struct.pack("<BBBBHHII", w, w, 0, 0, 1, 32, len(data), offset)
        blobs += data
        offset += len(data)
    with open(path, "wb") as f:
        f.write(header + entries + blobs)


def main():
    app = QGuiApplication.instance() or QGuiApplication(sys.argv)   # noqa: F841
    os.makedirs(ASSETS, exist_ok=True)
    logo = draw_logo(512)
    logo.save(os.path.join(ASSETS, "logo.png"), "PNG")
    write_ico(os.path.join(ASSETS, "icon.ico"))
    print("Έτοιμα:", os.path.join(ASSETS, "logo.png"), "και", os.path.join(ASSETS, "icon.ico"))


if __name__ == "__main__":
    main()

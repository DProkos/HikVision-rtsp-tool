#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Μετατρέπει το Hik RTSP Viewer σε εκτελέσιμο (.exe) με PyInstaller.

    python packaging/build_exe.py              # ένα αρχείο .exe (onefile)
    python packaging/build_exe.py --onedir     # φάκελος με exe (ξεκινά πιο γρήγορα)
    python packaging/build_exe.py --console    # με παράθυρο κονσόλας (για διάγνωση σφαλμάτων)

Αποτέλεσμα:  packaging/dist/HikRTSPViewer.exe   (onedir: packaging/dist/HikRTSPViewer/)

Σημειώσεις
  * Το PyInstaller ΔΕΝ κάνει cross-compile: για .exe τρέξε το script στα Windows.
  * Χρειάζονται: pip install -r requirements.txt   (PyQt5, av)
    Το PyInstaller εγκαθίσταται αυτόματα αν λείπει.
  * Οι ρυθμίσεις (hik_viewer_config.json), τα στιγμιότυπα και το discover_debug.txt
    αποθηκεύονται ΔΙΠΛΑ στο exe.
"""
import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))          # .../packaging
ROOT = os.path.dirname(HERE)                               # ρίζα του repo
ENTRY = os.path.join(ROOT, "hik_rtsp_viewer.py")
ASSETS = os.path.join(HERE, "assets")
NAME = "HikRTSPViewer"


def run(cmd, **kw):
    print("\n>", " ".join(f'"{c}"' if " " in c else c for c in cmd))
    return subprocess.call(cmd, **kw)


def ensure_assets():
    """Φτιάχνει λογότυπο/εικονίδιο αν λείπουν."""
    if not (os.path.exists(os.path.join(ASSETS, "logo.png"))
            and os.path.exists(os.path.join(ASSETS, "icon.ico"))):
        print("Λείπουν το λογότυπο/εικονίδιο - τα φτιάχνω...")
        if run([sys.executable, os.path.join(HERE, "make_logo.py")]) != 0:
            sys.exit("Αποτυχία δημιουργίας λογότυπου (χρειάζεται PyQt5).")


def ensure_pyinstaller():
    try:
        import PyInstaller  # noqa: F401
        return
    except ImportError:
        print("Το PyInstaller δεν είναι εγκατεστημένο - το εγκαθιστώ...")
    if run([sys.executable, "-m", "pip", "install", "pyinstaller"]) != 0:
        sys.exit("Δεν μπόρεσα να εγκαταστήσω το PyInstaller (pip install pyinstaller).")


def main():
    ap = argparse.ArgumentParser(description="Δημιουργία exe με PyInstaller")
    ap.add_argument("--onedir", action="store_true", help="φάκελος αντί για ένα αρχείο")
    ap.add_argument("--console", action="store_true", help="με παράθυρο κονσόλας")
    ap.add_argument("--name", default=NAME, help=f"όνομα εκτελέσιμου (προεπιλογή {NAME})")
    a = ap.parse_args()

    for mod in ("PyQt5", "av"):
        try:
            __import__(mod)
        except ImportError:
            sys.exit(f"Λείπει το «{mod}». Τρέξε πρώτα:  pip install -r requirements.txt")

    ensure_assets()
    ensure_pyinstaller()

    cmd = [sys.executable, "-m", "PyInstaller",
           "--noconfirm", "--clean",
           "--name", a.name,
           "--onedir" if a.onedir else "--onefile",
           "--distpath", os.path.join(HERE, "dist"),
           "--workpath", os.path.join(HERE, "build_tmp"),
           "--specpath", HERE,
           "--add-data", f"{ASSETS}{os.pathsep}assets",       # λογότυπο μέσα στο exe
           "--collect-all", "av",                              # PyAV + οι βιβλιοθήκες ffmpeg
           "--exclude-module", "cv2",                          # προαιρετικό fallback: δεν χρειάζεται
           "--exclude-module", "tkinter",
           "--exclude-module", "matplotlib",
           "--exclude-module", "numpy.tests"]
    if not a.console:
        cmd.append("--windowed")                               # χωρίς μαύρο παράθυρο κονσόλας
    if sys.platform == "win32":
        cmd += ["--icon", os.path.join(ASSETS, "icon.ico")]
    cmd.append(ENTRY)

    if run(cmd, cwd=ROOT) != 0:
        sys.exit("\nΗ δημιουργία απέτυχε. Δες το μήνυμα σφάλματος παραπάνω.")

    ext = ".exe" if sys.platform == "win32" else ""
    out = (os.path.join(HERE, "dist", a.name, a.name + ext) if a.onedir
           else os.path.join(HERE, "dist", a.name + ext))
    size = os.path.getsize(out) / 1024 / 1024 if os.path.exists(out) else 0
    print("\n" + "=" * 60)
    print(f"Έτοιμο: {out}  ({size:.0f} MB)")
    print("Οι ρυθμίσεις θα αποθηκεύονται δίπλα στο εκτελέσιμο.")
    if sys.platform != "win32":
        print("ΠΡΟΣΟΧΗ: χτίστηκε για αυτό το λειτουργικό. Για .exe τρέξε το στα Windows.")
    print("=" * 60)


if __name__ == "__main__":
    main()

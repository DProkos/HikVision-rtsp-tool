#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Διαγνωστικό για χαλασμένη (πράσινη) εικόνα RTSP σε καταγραφικό Hikvision.

Χρήση:   python hik_diag.py
Δοκιμάζει ΜΙΑ κάμερα με διάφορους τρόπους μεταφοράς (TCP / UDP / HTTP) και
διευθύνσεις, ~6" τον καθένα, και μετρά πόσα καρέ αποκωδικοποιούνται καθαρά.
Γράφει επίσης στοιχεία συσκευής (firmware) και ψάχνει ρυθμίσεις κρυπτογράφησης.
Όλα αποθηκεύονται και στο diag_report.txt. ΔΕΝ αλλάζει καμία ρύθμιση.
"""
import json
import os
import re
import sys
import time
from urllib.parse import quote

import av

from hik_encryption import make_opener, request, ask

av.logging.set_level(None)
APP_DIR = os.path.dirname(os.path.abspath(__file__))
REPORT = os.path.join(APP_DIR, "diag_report.txt")
SNAP_DIR = os.path.join(APP_DIR, "diag_snapshots")
_lines = []


def out(s=""):
    print(s)
    _lines.append(s)


def hide_pw(text):
    return re.sub(r"//[^/@\s]+@", "//***@", text)


def test_stream(url, transport, seconds=6.0):
    """Επιστρέφει dict με στατιστικά αποκωδικοποίησης."""
    r = dict(ok=0, corrupt=0, err=0, pkts=0, keys=0, codec="-", size="-", note="")
    try:
        c = av.open(url, options={"rtsp_transport": transport, "max_delay": "500000"},
                    timeout=(8, 8))
    except Exception as e:
        r["note"] = "δεν άνοιξε: " + hide_pw(str(e))[:60]
        return r, None
    snap = None
    try:
        s = c.streams.video[0]
        ctx = s.codec_context
        r["codec"] = ctx.name
        r["size"] = f"{ctx.width}x{ctx.height}"
        t0 = time.time()
        for p in c.demux(s):
            if time.time() - t0 > seconds:
                break
            if p.size == 0:
                continue
            r["pkts"] += 1
            r["keys"] += 1 if p.is_keyframe else 0
            try:
                frames = p.decode()
            except Exception:
                r["err"] += 1
                continue
            for f in frames:
                if f.is_corrupt:
                    r["corrupt"] += 1
                else:
                    r["ok"] += 1
                    if snap is None or r["ok"] == 10:
                        try:
                            snap = f.to_image()      # θέλει Pillow
                        except Exception:
                            pass
    except Exception as e:
        r["note"] = "διακοπή: " + hide_pw(str(e))[:60]
    finally:
        c.close()
    return r, snap


def main():
    cfg = {}
    try:
        with open(os.path.join(APP_DIR, "hik_viewer_config.json"), encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        pass
    ip = ask("IP καταγραφικού", cfg.get("ip", ""))
    user = ask("Χρήστης", cfg.get("user", "admin"))
    pwd = cfg.get("password") or __import__("getpass").getpass("Κωδικός: ")
    ch = int(ask("Κανάλι για δοκιμή (1-16)", "1"))
    http_port = ask("Θύρα HTTP", "80")

    # ---- στοιχεία συσκευής
    base = f"http://{ip}:{http_port}"
    op = make_opener(base, user, pwd)
    out("=== Συσκευή ===")
    st, xml = request(op, base + "/ISAPI/System/deviceInfo")
    for tag in ("model", "firmwareVersion", "firmwareReleasedDate", "encoderVersion"):
        m = re.search(rf"<{tag}>(.*?)</{tag}>", xml)
        out(f"  {tag}: {m.group(1) if m else '?'}")
    out("\n=== Ρυθμίσεις ροής καναλιού (ISAPI) ===")
    for sid in (f"{ch}01", f"{ch}02"):
        st, xml = request(op, base + f"/ISAPI/Streaming/channels/{sid}")
        if st != 200:
            out(f"  {sid}: HTTP {st}")
            continue
        vals = {t: (re.search(rf"<{t}>(.*?)</{t}>", xml) or [None, "?"])[1]
                for t in ("videoCodecType", "videoResolutionWidth", "videoResolutionHeight",
                          "videoQualityControlType", "maxFrameRate", "GovLength", "SmartCodec")}
        out(f"  {sid}: " + ", ".join(f"{k}={v}" for k, v in vals.items()))
        for m in re.finditer(r"<(\w*[Ee]ncrypt\w*)>(.*?)</\1>", xml):
            out(f"     !! {m.group(1)} = {m.group(2)}")
    out("\n=== Αναζήτηση ρυθμίσεων κρυπτογράφησης ===")
    found = False
    for path in ("/ISAPI/System/Network/EZVIZ/capabilities", "/ISAPI/Streaming/channels/capabilities",
                 f"/ISAPI/Streaming/channels/{ch}02/capabilities", "/ISAPI/Security/capabilities",
                 "/ISAPI/System/capabilities"):
        st, xml = request(op, base + path)
        hits = sorted(set(re.findall(r"<(\w*[Ee]ncrypt\w*)", xml)))
        if hits:
            found = True
            out(f"  {path}: {', '.join(hits)}")
    if not found:
        out("  καμία αναφορά σε κρυπτογράφηση")

    # ---- δοκιμές RTSP
    u, p_ = quote(user, safe=""), quote(pwd, safe="")
    urls = [
        ("sub  /Streaming/Channels", f"rtsp://{u}:{p_}@{ip}:554/Streaming/Channels/{ch}02"),
        ("sub  /h264/chN/sub", f"rtsp://{u}:{p_}@{ip}:554/h264/ch{ch}/sub/av_stream"),
        ("main /Streaming/Channels", f"rtsp://{u}:{p_}@{ip}:554/Streaming/Channels/{ch}01"),
    ]
    out(f"\n=== Δοκιμές RTSP (κανάλι {ch}, ~6\" η καθεμία) ===")
    out(f"{'διεύθυνση':26} {'μεταφορά':8} {'codec':6} {'ανάλυση':10} {'πακέτα':>6} "
        f"{'key':>4} {'καθαρά':>6} {'χαλασμ.':>7} {'σφάλμ.':>6}  σημείωση")
    os.makedirs(SNAP_DIR, exist_ok=True)
    best = None
    for name, url in urls:
        for tr in ("tcp", "udp", "http"):
            print(f"  δοκιμή: {name} / {tr} ...", end="\r")
            # RTSP-over-HTTP: Hikvision το σερβίρει στη θύρα web
            r, snap = test_stream(url.replace(":554/", f":{http_port}/") if tr == "http" else url, tr)
            out(f"{name:26} {tr:8} {r['codec']:6} {r['size']:10} {r['pkts']:6} {r['keys']:4} "
                f"{r['ok']:6} {r['corrupt']:7} {r['err']:6}  {r['note']}")
            if snap is not None:
                fn = os.path.join(SNAP_DIR, f"ch{ch}_{name.split()[0]}_{name.split('/')[1]}_{tr}.jpg")
                snap.save(fn)
            tot = r["ok"] + r["corrupt"] + r["err"]
            if tot and (best is None or r["ok"] / tot > best[0]):
                best = (r["ok"] / tot, name, tr)

    out("\n=== Συμπέρασμα ===")
    if best and best[0] > 0.9:
        out(f"Καθαρή εικόνα με: {best[1]} / {best[2]}  ({best[0]:.0%} καθαρά καρέ)")
    elif best:
        out(f"Καλύτερο: {best[1]} / {best[2]} με μόνο {best[0]:.0%} καθαρά καρέ - "
            "η ροή φτάνει χαλασμένη με όλους τους τρόπους.")
    else:
        out("Καμία δοκιμή δεν έδωσε εικόνα.")
    out(f"Εικόνες δοκιμών: {SNAP_DIR}")
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(_lines))
    print(f"\nΗ αναφορά αποθηκεύτηκε στο {REPORT} - στείλε μου το περιεχόμενό της.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    input("\nEnter για έξοδο...")

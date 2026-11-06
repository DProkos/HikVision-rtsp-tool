#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Έλεγχος αν η ροή RTSP ενός καταγραφικού Hikvision είναι κρυπτογραφημένη.

Χρήση:
    python hik_stream_check.py              # σύνδεση στο καταγραφικό
    python hik_stream_check.py raw.h264     # ανάλυση αρχείου που γράφτηκε πριν

Βήματα:
  1. Στιγμιότυπο JPEG από το ίδιο το καταγραφικό (ISAPI). Αν είναι καθαρό, η κάμερα
     και η εγγραφή είναι εντάξει - το πρόβλημα είναι μόνο στη ροή.
  2. Γράφει ~8" της δευτερεύουσας ροής H.264 (raw_chN.h264) και ελέγχει τις
     κεφαλίδες κάθε καρέ (slice header). Σε κανονική ροή είναι ~100% έγκυρες,
     σε κρυπτογραφημένη μοιάζουν τυχαίες (~5%).
  3. Δοκιμάζει αποκρυπτογράφηση AES με τον κωδικό επαλήθευσης Hik-Connect σε
     διάφορους συνδυασμούς και μετρά αν βγαίνουν καθαρά καρέ.
Γράφει τα πάντα στο stream_check.txt. ΔΕΝ αλλάζει καμία ρύθμιση.
"""
import hashlib
import json
import os
import re
import sys
import time
from urllib.parse import quote

import av

av.logging.set_level(None)
APP_DIR = os.path.dirname(os.path.abspath(__file__))
REPORT = os.path.join(APP_DIR, "stream_check.txt")
_lines = []


def out(s=""):
    print(s)
    _lines.append(s)


# --------------------------------------------------------------------------- #
# H.264: διάσπαση NAL και ανάγνωση κεφαλίδας slice
def split_nals(data):
    """Annex-B -> λίστα NAL (χωρίς start code)."""
    idx = [m.end() for m in re.finditer(b"\x00\x00\x01", data)]
    nals = []
    for n, start in enumerate(idx):
        end = idx[n + 1] - 3 if n + 1 < len(idx) else len(data)
        nal = data[start:end]
        while nal.endswith(b"\x00"):          # μηδενικό του 4-byte start code
            nal = nal[:-1]
        if nal:
            nals.append(nal)
    return nals


def rbsp(nal):
    """Αφαίρεση emulation-prevention bytes (00 00 03 -> 00 00)."""
    return re.sub(b"\x00\x00\x03", b"\x00\x00", nal)


class Bits:
    def __init__(self, b):
        self.b, self.p = b, 0

    def bit(self):
        if self.p >= len(self.b) * 8:
            raise EOFError
        v = (self.b[self.p >> 3] >> (7 - (self.p & 7))) & 1
        self.p += 1
        return v

    def ue(self):
        z = 0
        while self.bit() == 0:
            z += 1
            if z > 31:
                raise ValueError
        v = 0
        for _ in range(z):
            v = (v << 1) | self.bit()
        return (1 << z) - 1 + v


def pps_id_of(nal):
    try:
        return Bits(rbsp(nal[1:])).ue()
    except Exception:
        return None


def slice_ok(nal, pps_ids, max_mb):
    """True αν η κεφαλίδα slice είναι έγκυρη."""
    t = nal[0] & 0x1F
    try:
        b = Bits(rbsp(nal[1:]))
        first_mb, stype, pps = b.ue(), b.ue(), b.ue()
    except Exception:
        return False
    if stype > 9 or first_mb >= max_mb or (pps_ids and pps not in pps_ids):
        return False
    if t == 5 and stype % 5 not in (2, 4):     # IDR -> μόνο I/SI
        return False
    return True


def sps_mbs(sps):
    """Πλήθος macroblocks από το SPS (απλή ανάγνωση, baseline/main/high)."""
    try:
        b = Bits(rbsp(sps[1:]))
        profile = b.b[0]
        b.p = 24
        b.ue()
        if profile in (100, 110, 122, 244, 44, 83, 86, 118, 128):
            if b.ue() == 3:
                b.bit()
            b.ue(); b.ue(); b.bit()
            if b.bit():
                return None
        b.ue()
        poc = b.ue()
        if poc == 0:
            b.ue()
        elif poc == 1:
            return None
        b.ue(); b.bit()
        w, h = b.ue() + 1, b.ue() + 1
        frame_mbs_only = b.bit()
        return w * h * (2 - frame_mbs_only)
    except Exception:
        return None


def header_rate(nals, pps_ids, max_mb):
    sl = [n for n in nals if n[0] & 0x1F in (1, 5)]
    if not sl:
        return 0.0, 0
    return sum(slice_ok(n, pps_ids, max_mb) for n in sl) / len(sl), len(sl)


def decode_rate(params, nals):
    """Αποκωδικοποίηση με καθαρό decoder: (καθαρά, σύνολο καρέ)."""
    data = b"".join(b"\x00\x00\x00\x01" + n for n in params + nals)
    ctx = av.CodecContext.create("h264", "r")
    ctx.thread_count = 1
    ok = total = 0
    try:
        for pkt in ctx.parse(data) + ctx.parse(None):
            try:
                for f in ctx.decode(pkt):
                    total += 1
                    ok += 0 if f.is_corrupt else 1
            except Exception:
                total += 1
    except Exception:
        pass
    return ok, total


# --------------------------------------------------------------------------- #
# Αποκρυπτογράφηση (πειραματικά: ο αλγόριθμος της Hikvision δεν είναι δημόσιος)
def aes(key, mode):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    m = {"ecb": modes.ECB(), "cbc": modes.CBC(b"\x00" * 16),
         "ctr": modes.CTR(b"\x00" * 16)}[mode]
    return Cipher(algorithms.AES(key), m).decryptor()


def decrypt_nal(nal, key, mode, offset, blocks):
    head, body = nal[:offset], nal[offset:]
    if mode == "ctr":
        n = len(body) if blocks is None else min(len(body), 16 * blocks)
    else:
        n = (len(body) // 16) * 16 if blocks is None else min(16 * blocks, (len(body) // 16) * 16)
    if n == 0:
        return nal
    d = aes(key, mode)
    return head + d.update(body[:n]) + d.finalize() + body[n:]


def key_candidates(code):
    c = code.encode()
    md5 = hashlib.md5(c)
    keys = {
        "κωδικός+0x00": c.ljust(16, b"\x00")[:16],
        "κωδικός+'0'": c.ljust(16, b"0")[:16],
        "κωδικός x2": (c * 4)[:16],
        "MD5(κωδικός)": md5.digest(),
        "MD5 hex[:16]": md5.hexdigest()[:16].encode(),
        "MD5 HEX[:16]": md5.hexdigest()[:16].upper().encode(),
    }
    return keys


def try_decrypt(params, nals, code, pps_ids, max_mb):
    try:
        import cryptography  # noqa: F401
    except ImportError:
        out("  (λείπει η βιβλιοθήκη: pip install cryptography - παράλειψη)")
        return None
    slices = [n for n in nals if n[0] & 0x1F in (1, 5)][:120]
    other = [n for n in nals if n[0] & 0x1F not in (1, 5, 7, 8)]
    results = []
    for kname, key in key_candidates(code).items():
        for mode in ("ecb", "cbc", "ctr"):
            for offset in (1, 2, 4):
                for blocks in (None, 1):
                    dec = [decrypt_nal(n, key, mode, offset, blocks) for n in slices]
                    rate, _ = header_rate(dec, pps_ids, max_mb)
                    results.append((rate, kname, mode, offset, blocks, dec))
    results.sort(key=lambda r: -r[0])
    out(f"{'κλειδί':16} {'mode':5} {'offset':6} {'μπλοκ':6} {'κεφαλίδες OK':>12}  καρέ")
    best = None
    for rate, kname, mode, offset, blocks, dec in results[:6]:
        ok, tot = decode_rate(params, other + dec) if rate > 0.3 else (0, 0)
        out(f"{kname:16} {mode:5} {offset:6} {'όλα' if blocks is None else blocks:>6} "
            f"{rate:12.0%}  {ok}/{tot}")
        if tot and ok / tot > 0.8 and best is None:
            best = (kname, mode, offset, blocks)
    return best


# --------------------------------------------------------------------------- #
def capture(url, seconds=8.0):
    c = av.open(url, options={"rtsp_transport": "tcp"}, timeout=(8, 8))
    try:
        s = c.streams.video[0]
        extradata = bytes(s.codec_context.extradata or b"")
        name = s.codec_context.name
        chunks, t0 = [], time.time()
        for p in c.demux(s):
            if time.time() - t0 > seconds:
                break
            if p.size:
                chunks.append(bytes(p))
        return name, extradata, b"".join(chunks)
    finally:
        c.close()


def analyse(params_sdp, data, code):
    nals = split_nals(data)
    types = {}
    for n in nals:
        types[n[0] & 0x1F] = types.get(n[0] & 0x1F, 0) + 1
    out("\n=== Τύποι NAL στη ροή ===")
    names = {1: "P/B slice", 5: "IDR (κεντρικό)", 6: "SEI", 7: "SPS", 8: "PPS", 9: "AUD"}
    for t in sorted(types):
        out(f"  τύπος {t:2} {names.get(t, '(ασυνήθιστος)'):16} x{types[t]}")
    if any(n[0] & 0x80 for n in nals):
        out("  !! forbidden_zero_bit=1 σε κάποια NAL - χαλασμένες κεφαλίδες NAL")

    inband = [n for n in nals if n[0] & 0x1F in (7, 8)]
    sps = [n for n in params_sdp + inband if n[0] & 0x1F == 7]
    pps = [n for n in params_sdp + inband if n[0] & 0x1F == 8]
    params = (params_sdp or []) + [n for n in inband if n not in params_sdp][:2]
    max_mb = next((m for m in (sps_mbs(s) for s in sps) if m), 8160)
    pps_ids = {i for i in (pps_id_of(p) for p in pps) if i is not None}
    out(f"  macroblocks/καρέ: {max_mb}, PPS id: {sorted(pps_ids) or '?'}")
    if params_sdp:
        sdp_sps = [n for n in params_sdp if n[0] & 0x1F == 7]
        ib_sps = [n for n in inband if n[0] & 0x1F == 7]
        if ib_sps and sdp_sps:
            same = all(n == sdp_sps[0] for n in ib_sps)
            out(f"  SPS μέσα στη ροή {'ίδιο' if same else 'ΔΙΑΦΟΡΕΤΙΚΟ'} με αυτό της σύνδεσης (SDP)")

    out("\n=== Δείγμα αρχής καρέ (hex) ===")
    shown = {}
    for n in nals:
        t = n[0] & 0x1F
        if t in (1, 5) and shown.get(t, 0) < 4:
            shown[t] = shown.get(t, 0) + 1
            out(f"  τύπος {t}: {n[:12].hex(' ')}")
    out("  (κανονικά: IDR ξεκινά συνήθως '65 88 ..', P-slice '41 9a ..' ή '41 9b ..')")

    rate, n_sl = header_rate(nals, pps_ids, max_mb)
    ok, tot = decode_rate(params, [n for n in nals if n[0] & 0x1F not in (7, 8)])
    out("\n=== Αποτέλεσμα ελέγχου ===")
    out(f"  Έγκυρες κεφαλίδες καρέ: {rate:.0%} από {n_sl}")
    out(f"  Καθαρά καρέ στην αποκωδικοποίηση: {ok}/{tot}")
    clean = ok / tot if tot else 0.0
    if n_sl == 0:
        verdict = "Δεν βρέθηκαν καρέ H.264 - δεν μπορώ να κρίνω."
    elif clean > 0.8:
        verdict = "Η ροή είναι ΚΑΝΟΝΙΚΗ (όχι κρυπτογραφημένη)."
    elif rate >= 0.95:
        verdict = ("Οι κεφαλίδες είναι σωστές αλλά τα δεδομένα εικόνας χαλάνε: "
                   "πιθανή ΜΕΡΙΚΗ κρυπτογράφηση (μόνο τα δεδομένα μετά την κεφαλίδα) ή απώλειες.")
    elif rate < 0.7 and clean < 0.1:
        verdict = ("Οι κεφαλίδες μοιάζουν ΤΥΧΑΙΕΣ και κανένα καρέ δεν βγαίνει καθαρό: "
                   "η ροή είναι σχεδόν σίγουρα ΚΡΥΠΤΟΓΡΑΦΗΜΕΝΗ.")
    else:
        verdict = "Ανάμεικτο αποτέλεσμα: μάλλον απώλειες δεδομένων ή μερική κρυπτογράφηση."
    out("  >> " + verdict)

    if code and (rate < 0.9 or (tot and ok / tot < 0.8)):
        out("\n=== Δοκιμές αποκρυπτογράφησης (πειραματικό) ===")
        best = try_decrypt(params, nals, code, pps_ids, max_mb)
        if best:
            out(f"  >> ΒΡΕΘΗΚΕ: κλειδί {best[0]}, AES-{best[1].upper()}, offset {best[2]}, "
                f"μπλοκ {best[3] or 'όλα'}")
        elif best is None:
            out("  >> Κανένας από τους δοκιμασμένους συνδυασμούς δεν έδωσε καθαρή εικόνα.")


def isapi_snapshot(ip, port, user, pwd, ch):
    from hik_encryption import make_opener
    import urllib.request
    base = f"http://{ip}:{port}"
    op = make_opener(base, user, pwd)
    code = ""
    try:
        with op.open(base + "/ISAPI/System/Network/EZVIZ", timeout=10) as r:
            m = re.search(r"<verificationCode>(.*?)</verificationCode>", r.read().decode())
            code = m.group(1) if m else ""
    except Exception:
        pass
    for h in op.handlers:
        if hasattr(h, "reset_retry_count"):
            h.reset_retry_count()
    try:
        with op.open(urllib.request.Request(base + f"/ISAPI/Streaming/channels/{ch}01/picture"),
                     timeout=15) as r:
            img = r.read()
        fn = os.path.join(APP_DIR, f"snapshot_ch{ch}.jpg")
        with open(fn, "wb") as f:
            f.write(img)
        ok = img[:2] == b"\xff\xd8"
        out(f"  Στιγμιότυπο καταγραφικού: {fn} ({len(img)//1024} KB, "
            f"{'JPEG' if ok else 'όχι JPEG!'}) - άνοιξέ το: αν είναι καθαρό, η κάμερα είναι εντάξει")
    except Exception as e:
        out(f"  Στιγμιότυπο: απέτυχε ({e})")
    return code


def main():
    if len(sys.argv) > 1:                         # ανάλυση αρχείου
        data = open(sys.argv[1], "rb").read()
        code = sys.argv[2] if len(sys.argv) > 2 else ""
        out(f"=== Αρχείο {sys.argv[1]} ({len(data)//1024} KB) ===")
        analyse([], data, code)
    else:
        from hik_encryption import ask
        cfg = {}
        try:
            with open(os.path.join(APP_DIR, "hik_viewer_config.json"), encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            pass
        ip = ask("IP καταγραφικού", cfg.get("ip", ""))
        user = ask("Χρήστης", cfg.get("user", "admin"))
        pwd = cfg.get("password") or __import__("getpass").getpass("Κωδικός: ")
        ch = int(ask("Κανάλι (1-16)", "1"))
        http_port = ask("Θύρα HTTP", "80")
        out("=== Καταγραφικό ===")
        code = isapi_snapshot(ip, http_port, user, pwd, ch)
        out(f"  Κωδικός επαλήθευσης Hik-Connect: {'βρέθηκε' if code else 'δεν βρέθηκε'}")
        url = f"rtsp://{quote(user, safe='')}:{quote(pwd, safe='')}@{ip}:554/Streaming/Channels/{ch}02"
        out("\n=== Λήψη 8\" δευτερεύουσας ροής ===")
        try:
            name, extradata, data = capture(url)
        except Exception as e:
            out("  Αποτυχία σύνδεσης: " + re.sub(r"//[^/@\s]+@", "//***@", str(e)))
            return
        raw = os.path.join(APP_DIR, f"raw_ch{ch}.h264")
        with open(raw, "wb") as f:
            f.write(extradata + data)
        out(f"  codec {name}, {len(data)//1024} KB -> {raw}")
        if name != "h264":
            out("  Η δευτερεύουσα ροή δεν είναι H.264 - γύρισέ τη σε H.264 για τον έλεγχο.")
            return
        analyse(split_nals(extradata), data, code)
    print(f"\nΑναφορά: {REPORT} - στείλε μου το περιεχόμενό της.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    finally:
        if _lines:
            with open(REPORT, "w", encoding="utf-8") as f:
                f.write("\n".join(_lines))
    if len(sys.argv) == 1:
        input("\nEnter για έξοδο...")

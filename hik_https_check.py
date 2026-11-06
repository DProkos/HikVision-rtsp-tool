#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Διαγνωστικό HTTPS για καταγραφικό Hikvision: δείχνει σε ποιο στάδιο αποτυγχάνει.

    python hik_https_check.py
    python hik_https_check.py --ip 192.168.35.18 --port 443 --user admin

Στάδια:
  1. Σύνδεση TCP στη θύρα (ανοιχτή / κλειστή / δεν απαντά)
  2. TLS handshake με διάφορες εκδόσεις και ρυθμίσεις (ποια δουλεύει)
  3. Στοιχεία του πιστοποιητικού (αποτύπωμα, αλγόριθμος, μέγεθος κλειδιού)
  4. Αίτημα GET /ISAPI/System/deviceInfo με Digest

ΔΕΝ αλλάζει καμία ρύθμιση. Ο κωδικός δεν γράφεται πουθενά.
Γράφει την αναφορά και στο https_check.txt.
"""
import argparse
import getpass
import hashlib
import os
import socket
import ssl
import sys
import urllib.error
import urllib.request
import warnings

APP_DIR = os.path.dirname(os.path.abspath(__file__))
REPORT = os.path.join(APP_DIR, "https_check.txt")
_lines = []


def out(s=""):
    print(s)
    _lines.append(s)


def make_ctx(min_v=None, max_v=None, ciphers=None):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        try:
            if min_v:
                ctx.minimum_version = getattr(ssl.TLSVersion, min_v)
            if max_v:
                ctx.maximum_version = getattr(ssl.TLSVersion, max_v)
        except (AttributeError, ValueError) as e:
            raise RuntimeError(f"δεν υποστηρίζεται από αυτή την Python: {e}")
    if ciphers:
        ctx.set_ciphers(ciphers)
    return ctx


def tcp_check(ip, port):
    try:
        s = socket.create_connection((ip, port), timeout=5)
        s.close()
        return True, "ανοιχτή (δέχεται συνδέσεις)"
    except ConnectionRefusedError:
        return False, "ΚΛΕΙΣΤΗ (άρνηση σύνδεσης) - το HTTPS μάλλον δεν είναι ενεργό στο καταγραφικό"
    except (socket.timeout, TimeoutError):
        return False, "δεν απαντά (timeout) - firewall, λάθος IP ή το καταγραφικό δεν φτάνεται"
    except OSError as e:
        return False, f"σφάλμα: {e}"


def tls_try(ip, port, ctx):
    """Επιστρέφει (ok, κείμενο, der_cert)."""
    try:
        with socket.create_connection((ip, port), timeout=6) as raw:
            with ctx.wrap_socket(raw, server_hostname=ip) as tls:
                der = tls.getpeercert(binary_form=True)
                return True, f"{tls.version()} / {tls.cipher()[0]}", der
    except ssl.SSLError as e:
        return False, f"SSLError: {getattr(e, 'reason', '') or e}", None
    except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError) as e:
        return False, f"το καταγραφικό έκλεισε τη σύνδεση ({type(e).__name__})", None
    except (socket.timeout, TimeoutError):
        return False, "timeout στο handshake", None
    except OSError as e:
        return False, f"{type(e).__name__}: {e}", None


def cert_info(der):
    out(f"  μέγεθος πιστοποιητικού: {len(der)} bytes")
    out(f"  αποτύπωμα SHA-256: {hashlib.sha256(der).hexdigest()}")
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives.asymmetric import rsa
        c = x509.load_der_x509_certificate(der)
        pub = c.public_key()
        size = f"{pub.key_size} bit" if hasattr(pub, "key_size") else type(pub).__name__
        kind = "RSA" if isinstance(pub, rsa.RSAPublicKey) else type(pub).__name__
        out(f"  υποκείμενο: {c.subject.rfc4514_string() or '(κενό)'}")
        out(f"  κλειδί: {kind} {size}")
        out(f"  υπογραφή: {c.signature_algorithm_oid._name}")
        try:
            out(f"  ισχύει: {c.not_valid_before_utc:%Y-%m-%d} έως {c.not_valid_after_utc:%Y-%m-%d}")
        except AttributeError:
            out(f"  ισχύει: {c.not_valid_before:%Y-%m-%d} έως {c.not_valid_after:%Y-%m-%d}")
    except ImportError:
        out("  (για περισσότερα στοιχεία: pip install cryptography)")
    except Exception as e:
        out(f"  (δεν διαβάστηκαν τα πεδία του πιστοποιητικού: {e})")


def http_get(ip, port, user, pw, ctx):
    base = f"https://{ip}:{port}"
    mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    mgr.add_password(None, base, user, pw)
    op = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx),
                                     urllib.request.HTTPDigestAuthHandler(mgr),
                                     urllib.request.HTTPBasicAuthHandler(mgr))
    try:
        with op.open(base + "/ISAPI/System/deviceInfo", timeout=10) as r:
            body = r.read().decode("utf-8", "replace")
            return r.status, body
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ip")
    ap.add_argument("--port", type=int, default=443)
    ap.add_argument("--user", default="admin")
    a = ap.parse_args()
    ip = a.ip or input("IP καταγραφικού [192.168.35.18]: ").strip() or "192.168.35.18"
    pw = os.environ.get("HIK_PASSWORD") or getpass.getpass("Κωδικός admin (δεν εμφανίζεται): ")

    out(f"Python {sys.version.split()[0]} | {ssl.OPENSSL_VERSION}")
    out(f"\n=== 1. Σύνδεση TCP στο {ip}:{a.port} ===")
    ok, msg = tcp_check(ip, a.port)
    out("  " + msg)
    if not ok:
        out("\nΣυμπέρασμα: δεν υπάρχει υπηρεσία HTTPS σε αυτή τη θύρα.")
        out("  -> Ενεργοποίησε το HTTPS στο καταγραφικό (Δίκτυο → Υπηρεσία δικτύου / Προηγμένες)"
            " και έλεγξε τη θύρα HTTPS (Δίκτυο → Θύρα).")
        return

    out(f"\n=== 2. TLS handshake (δοκιμή εκδόσεων/ρυθμίσεων) ===")
    configs = [
        ("προεπιλογή Python (TLS 1.2+)", dict()),
        ("TLS 1.3 μόνο", dict(min_v="TLSv1_3")),
        ("TLS 1.2 μόνο", dict(min_v="TLSv1_2", max_v="TLSv1_2")),
        ("TLS 1.1 μόνο, χαλαρή ασφάλεια", dict(min_v="TLSv1_1", max_v="TLSv1_1", ciphers="ALL:@SECLEVEL=0")),
        ("TLS 1.0 μόνο, χαλαρή ασφάλεια", dict(min_v="TLSv1", max_v="TLSv1", ciphers="ALL:@SECLEVEL=0")),
        ("TLS 1.0+ με ALL, SECLEVEL=0 (όπως το viewer)", dict(min_v="TLSv1", ciphers="ALL:@SECLEVEL=0")),
    ]
    first_ok, der, good_ctx = None, None, None
    for name, kw in configs:
        try:
            ctx = make_ctx(**kw)
        except (RuntimeError, ssl.SSLError) as e:
            out(f"  [ ---- ] {name}: δεν δοκιμάστηκε ({e})")
            continue
        ok, text, d = tls_try(ip, a.port, ctx)
        out(f"  [{'  OK ' if ok else 'ΑΠΟΤΥΧΙΑ'}] {name}: {text}")
        if ok and first_ok is None:
            first_ok, der, good_ctx = name, d, ctx
    if first_ok is None:
        out("\nΣυμπέρασμα: το καταγραφικό ΔΕΝ ολοκληρώνει TLS handshake με καμία ρύθμιση.")
        out("  -> Στείλε μου αυτή την αναφορά. Πιθανό: πολύ παλιό TLS/κρυπτοσύστημα που η Python σου δεν")
        out("     υποστηρίζει, ή η θύρα δεν είναι όντως HTTPS (π.χ. απλό HTTP στη θύρα 443).")
        return

    out(f"\n=== 3. Πιστοποιητικό (πρώτη επιτυχία: {first_ok}) ===")
    cert_info(der)

    out("\n=== 4. Αίτημα GET /ISAPI/System/deviceInfo (Digest) ===")
    status, body = http_get(ip, a.port, a.user, pw, good_ctx)
    if status == 200:
        import re
        for tag in ("model", "firmwareVersion"):
            m = re.search(rf"<{tag}>(.*?)</{tag}>", body)
            out(f"  {tag}: {m.group(1) if m else '?'}")
        out("\nΣυμπέρασμα: το HTTPS ΔΟΥΛΕΥΕΙ. Στο viewer τσέκαρε HTTPS με θύρα "
            f"{a.port}.")
    elif status in (401, 403):
        out(f"  HTTP {status} - το HTTPS δουλεύει αλλά ο χρήστης/κωδικός δεν έγινε δεκτός.")
    else:
        out(f"  αποτυχία: {status} {body}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    finally:
        if _lines:
            with open(REPORT, "w", encoding="utf-8") as f:
                f.write("\n".join(_lines))
            print(f"\nΑναφορά: {REPORT}")
    if len(sys.argv) == 1:
        input("\nEnter για έξοδο...")

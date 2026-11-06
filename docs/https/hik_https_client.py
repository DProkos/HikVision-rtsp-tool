#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Αυτόνομος πελάτης HTTPS για καταγραφικά Hikvision (ISAPI).  Μόνο standard library, Python 3.8+.

Τι κάνει:
  * μόνιμη σύνδεση TLS (keep-alive) + HTTP Digest, όπως ο πελάτης HTTP του docs/http
  * δέχεται το ΑΥΤΟ-ΥΠΟΓΕΓΡΑΜΜΕΝΟ πιστοποιητικό του καταγραφικού, αλλά το ΑΠΟΜΝΗΜΟΝΕΥΕΙ στην
    πρώτη σύνδεση (trust on first use) και ΑΡΝΕΙΤΑΙ αν αργότερα αλλάξει (CertChangedError)
  * δουλεύει με παλιά firmware (TLS 1.0+, ασθενή κρυπτοσυστήματα/πιστοποιητικά)

Χρήση ως βιβλιοθήκη:
    from hik_https_client import DigestHttpsClient, PinStore
    pins = PinStore("pins.json")
    c = DigestHttpsClient("https://192.168.1.64:443/ISAPI/System/deviceInfo", "admin", "pass", pins)
    xml = c.get()                       # bytes

Γραμμή εντολών (ο κωδικός από τη μεταβλητή HIK_PASSWORD ή --password):
    python hik_https_client.py --ip 192.168.1.64                    # model/firmware + κατάσταση pin
    python hik_https_client.py --ip 192.168.1.64 --picture 1 --save a.jpg
    python hik_https_client.py --ip 192.168.1.64 --bench --seconds 10
    python hik_https_client.py --ip 192.168.1.64 --reset-pin        # ξεχνά το αποθηκευμένο πιστοποιητικό
"""
import argparse
import hashlib
import http.client
import json
import os
import re
import ssl
import sys
import threading
import time
import warnings
from urllib.parse import urlsplit

__all__ = ["CertChangedError", "PinStore", "tls_context", "DigestHttpsClient"]


class CertChangedError(OSError):
    """Το πιστοποιητικό του καταγραφικού διαφέρει από αυτό που είχαμε απομνημονεύσει."""


def tls_context(legacy=True):
    """Context TLS χωρίς επαλήθευση αλυσίδας/ονόματος (το πιστοποιητικό είναι αυτο-υπογεγραμμένο).
    Την αυθεντικότητα την εξασφαλίζει το PinStore. legacy=True: επιτρέπει TLS 1.0+ και
    ασθενή κρυπτοσυστήματα για παλιά firmware (ΧΑΛΑΡΩΝΕΙ την ασφάλεια - χρησιμοποίησέ το μόνο
    μέσα σε αξιόπιστο δίκτυο)."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    if legacy:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)      # «TLSv1 is deprecated»
            try:
                ctx.minimum_version = ssl.TLSVersion.TLSv1
            except (AttributeError, ValueError):
                pass
        for ciphers in ("ALL:@SECLEVEL=0", "DEFAULT:@SECLEVEL=0"):  # OpenSSL 3: SECLEVEL=0
            try:
                ctx.set_ciphers(ciphers)
                break
            except ssl.SSLError:
                continue
    return ctx


class PinStore:
    """Αποτυπώματα SHA-256 πιστοποιητικών, ανά "host:port". Προαιρετικά αποθηκεύονται σε JSON."""

    def __init__(self, path=None):
        self.path = path
        self._lock = threading.Lock()
        self._pins = {}
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    self._pins = dict(json.load(f))
            except (OSError, ValueError):
                self._pins = {}

    def check(self, key, der):
        """Πρώτη φορά: το αποθηκεύει ("new"). Ίδιο: "ok". Διαφορετικό: CertChangedError."""
        fp = hashlib.sha256(der).hexdigest()
        with self._lock:
            known = self._pins.get(key)
            if known is None:
                self._pins[key] = fp
                self._save()
                return "new"
            if known != fp:
                raise CertChangedError(
                    f"ΑΛΛΑΞΕ το πιστοποιητικό του {key} (ήταν {known[:16]}…, τώρα {fp[:16]}…). "
                    "Αν το άλλαξες εσύ, κάνε reset του pin· αλλιώς ΜΗΝ συνεχίσεις.")
            return "ok"

    def forget(self, key):
        with self._lock:
            had = self._pins.pop(key, None) is not None
            self._save()
            return had

    def get(self, key):
        return self._pins.get(key)

    def _save(self):
        if not self.path:
            return
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._pins, f, indent=2)
        os.replace(tmp, self.path)                  # ατομική αντικατάσταση


class DigestHttpsClient:
    """GET σε ένα URL https:// με μόνιμη σύνδεση TLS και HTTP Digest (RFC 2617, qop=auth)."""

    def __init__(self, url, user, password, pins, timeout=6.0, legacy_tls=True):
        p = urlsplit(url)
        if p.scheme != "https":
            raise ValueError("το URL πρέπει να είναι https://")
        self.host, self.port = p.hostname, p.port or 443
        self.path = p.path + (("?" + p.query) if p.query else "")
        self.user, self.password, self.timeout = user, password, timeout
        self.pins, self.legacy_tls = pins, legacy_tls
        self.conn = None
        self.challenge = None
        self.nc = 0
        self.cnonce = hashlib.md5(os.urandom(8)).hexdigest()[:16]
        self.pin_status = None                        # "new" | "ok" μετά τη σύνδεση

    # -- σύνδεση TLS + έλεγχος pin
    def _connect(self):
        self.close()
        conn = http.client.HTTPSConnection(self.host, self.port, timeout=self.timeout,
                                           context=tls_context(self.legacy_tls))
        conn.connect()
        try:
            self.pin_status = self.pins.check(f"{self.host}:{self.port}",
                                              conn.sock.getpeercert(binary_form=True))
        except Exception:
            conn.close()
            raise
        self.conn = conn

    def close(self):
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None

    # -- digest
    @staticmethod
    def _md5(s):
        return hashlib.md5(s.encode("utf-8")).hexdigest()

    def _authorization(self):
        c = self.challenge
        self.nc += 1
        nc = f"{self.nc:08x}"
        ha1 = self._md5(f"{self.user}:{c['realm']}:{self.password}")
        ha2 = self._md5(f"GET:{self.path}")
        qop = "auth" in (c.get("qop") or "")
        resp = (self._md5(f"{ha1}:{c['nonce']}:{nc}:{self.cnonce}:auth:{ha2}") if qop
                else self._md5(f"{ha1}:{c['nonce']}:{ha2}"))
        h = (f'Digest username="{self.user}", realm="{c["realm"]}", nonce="{c["nonce"]}", '
             f'uri="{self.path}", response="{resp}"')
        if qop:
            h += f', qop=auth, nc={nc}, cnonce="{self.cnonce}"'
        if c.get("opaque"):
            h += f', opaque="{c["opaque"]}"'
        return h

    def _send(self):
        headers = {"Connection": "keep-alive"}
        if self.challenge:
            headers["Authorization"] = self._authorization()
        self.conn.request("GET", self.path, headers=headers)
        r = self.conn.getresponse()
        return r, r.read()

    def get(self):
        """Επιστρέφει το σώμα (bytes) ή σηκώνει OSError (HTTP σφάλμα, δίκτυο, CertChangedError)."""
        if self.conn is None:
            self._connect()
        try:
            r, body = self._send()
        except CertChangedError:
            raise
        except (http.client.HTTPException, OSError):
            self._connect()                           # η σύνδεση έκλεισε: ξαναδοκίμασε μία φορά
            r, body = self._send()
        if r.status == 401:
            www = r.getheader("WWW-Authenticate", "")
            self.challenge = {k: (a or b) for k, a, b in
                              re.findall(r'(\w+)=(?:"([^"]*)"|([^,\s]+))', www)}
            r, body = self._send()
        if r.status != 200:
            raise OSError(f"HTTP {r.status}")
        return body


def main():
    ap = argparse.ArgumentParser(description="Πελάτης HTTPS για Hikvision (με pinning πιστοποιητικού)")
    ap.add_argument("--ip", required=True)
    ap.add_argument("--port", type=int, default=443)
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", default=os.environ.get("HIK_PASSWORD", ""))
    ap.add_argument("--pin-file", default="pins.json", help="πού αποθηκεύονται τα αποτυπώματα")
    ap.add_argument("--reset-pin", action="store_true", help="ξεχνά το αποθηκευμένο πιστοποιητικό")
    ap.add_argument("--picture", type=int, metavar="CH", help="κατεβάζει στιγμιότυπο του καναλιού")
    ap.add_argument("--save", help="αρχείο για το στιγμιότυπο")
    ap.add_argument("--bench", action="store_true", help="μέτρηση fps (κανάλι --picture ή 1)")
    ap.add_argument("--seconds", type=float, default=8)
    ap.add_argument("--no-legacy-tls", action="store_true", help="μόνο σύγχρονο TLS (χωρίς χαλάρωση)")
    a = ap.parse_args()

    pins = PinStore(a.pin_file)
    key = f"{a.ip}:{a.port}"
    if a.reset_pin:
        print("το pin ξεχάστηκε" if pins.forget(key) else "δεν υπήρχε αποθηκευμένο pin")
        return
    base = f"https://{a.ip}:{a.port}"
    legacy = not a.no_legacy_tls
    try:
        if a.bench or a.picture:
            ch = a.picture or 1
            c = DigestHttpsClient(f"{base}/ISAPI/Streaming/channels/{ch}01/picture",
                                  a.user, a.password, pins, legacy_tls=legacy)
            if a.bench:
                n, size, t0 = 0, 0, time.time()
                while time.time() - t0 < a.seconds:
                    size += len(c.get())
                    n += 1
                dt = time.time() - t0
                print(f"{n} καρέ σε {dt:.1f}s = {n / dt:.1f} fps, μέσο μέγεθος {size / n / 1024:.1f} KB "
                      f"| pin: {c.pin_status}")
            else:
                data = c.get()
                out = a.save or f"snapshot_ch{ch}.jpg"
                with open(out, "wb") as f:
                    f.write(data)
                print(f"αποθηκεύτηκε {out} ({len(data) // 1024} KB) | pin: {c.pin_status}")
            c.close()
        else:
            c = DigestHttpsClient(f"{base}/ISAPI/System/deviceInfo", a.user, a.password, pins,
                                  legacy_tls=legacy)
            xml = c.get().decode("utf-8", "replace")
            for tag in ("model", "firmwareVersion"):
                m = re.search(rf"<{tag}>(.*?)</{tag}>", xml)
                print(f"{tag}: {m.group(1) if m else '?'}")
            print(f"pin: {c.pin_status} ({'πρώτη σύνδεση - το πιστοποιητικό απομνημονεύτηκε' if c.pin_status == 'new' else 'ίδιο με το αποθηκευμένο'})")
            c.close()
    except CertChangedError as e:
        sys.exit(f"ΠΡΟΣΟΧΗ: {e}")
    except OSError as e:
        sys.exit(f"Σφάλμα: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()

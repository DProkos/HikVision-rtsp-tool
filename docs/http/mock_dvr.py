#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ψεύτικο καταγραφικό για δοκιμές χωρίς συσκευή.

Μιμείται τα endpoints εικόνας του ISAPI με HTTP Digest (qop=auth) και
keep-alive, και ελέγχει σωστά το digest response.

    python mock_dvr.py --port 8080 --jpeg εικόνα.jpg [--mjpeg] [--delay 0.02]

  --jpeg    το JPEG που θα σερβίρει (χωρίς αυτό: ψεύτικα bytes FFD8..FFD9,
            αρκετά για μέτρηση fps, όχι για προβολή)
  --mjpeg   ενεργοποιεί το /httpPreview (ροή multipart)
  --delay   χρόνος «δημιουργίας» κάθε εικόνας σε δευτερόλεπτα
  --serial  ένα αίτημα τη φορά (μιμείται DVR που δεν δουλεύει παράλληλα)
  --tls     HTTPS με αυτο-υπογεγραμμένο πιστοποιητικό (χρειάζεται: pip install cryptography)
Χρήστης/κωδικός: admin / test
"""
import argparse
import hashlib
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

USER, PASSWORD, REALM, NONCE = "admin", "test", "DS", "mocknonce0001"
STATS = {"connections": 0, "requests": 0, "rejected": 0}


def md5(s):
    return hashlib.md5(s.encode()).hexdigest()


def make_handler(jpeg, delay, mjpeg, serial=False):
    lock = threading.Lock()                    # --serial: ένα αίτημα τη φορά

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"          # keep-alive, όπως τα πραγματικά DVR

        def log_message(self, *a):
            pass

        def setup(self):
            super().setup()
            STATS["connections"] += 1

        def _empty(self, code, extra=None):
            self.send_response(code)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _authorized(self):
            a = self.headers.get("Authorization", "")
            if not a.startswith("Digest"):
                self._empty(401, {"WWW-Authenticate":
                                  f'Digest realm="{REALM}", qop="auth", nonce="{NONCE}", opaque="op"'})
                return False
            d = {k: (x or y) for k, x, y in re.findall(r'(\w+)=(?:"([^"]*)"|([^,\s]+))', a)}
            ha1 = md5(f"{USER}:{REALM}:{PASSWORD}")
            ha2 = md5(f"GET:{d.get('uri', '')}")
            if d.get("response") != md5(f"{ha1}:{NONCE}:{d.get('nc')}:{d.get('cnonce')}:auth:{ha2}"):
                STATS["rejected"] += 1
                self._empty(403)
                return False
            return True

        def do_GET(self):
            STATS["requests"] += 1
            if not self._authorized():
                return
            path = self.path.split("?")[0]
            if path.endswith("/picture"):
                if serial:
                    with lock:
                        time.sleep(delay)
                else:
                    time.sleep(delay)
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(jpeg)))
                self.end_headers()
                self.wfile.write(jpeg)
            elif path.endswith("/httpPreview") and mjpeg:
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.end_headers()
                try:
                    while True:
                        time.sleep(delay or 0.04)
                        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n"
                                         + f"Content-Length: {len(jpeg)}\r\n\r\n".encode()
                                         + jpeg + b"\r\n")
                        self.wfile.flush()
                except OSError:
                    pass
                self.close_connection = True
            else:
                self._empty(404)
    return Handler


def make_self_signed(tmpdir):
    """Αυτο-υπογεγραμμένο πιστοποιητικό (όπως τα πραγματικά καταγραφικά). Θέλει: pip install cryptography"""
    import datetime
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "mock-dvr")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .sign(key, hashes.SHA256()))
    cpath, kpath = os.path.join(tmpdir, "cert.pem"), os.path.join(tmpdir, "key.pem")
    with open(cpath, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(kpath, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM,
                                  serialization.PrivateFormat.TraditionalOpenSSL,
                                  serialization.NoEncryption()))
    return cpath, kpath


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--jpeg")
    ap.add_argument("--mjpeg", action="store_true")
    ap.add_argument("--delay", type=float, default=0.02)
    ap.add_argument("--tls", action="store_true",
                    help="HTTPS με αυτο-υπογεγραμμένο πιστοποιητικό (νέο σε κάθε εκκίνηση)")
    ap.add_argument("--serial", action="store_true",
                    help="εξυπηρέτηση ενός αιτήματος τη φορά (όπως αργό DVR)")
    a = ap.parse_args()
    jpeg = open(a.jpeg, "rb").read() if a.jpeg else b"\xff\xd8" + b"\x00" * 20000 + b"\xff\xd9"
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(jpeg, a.delay, a.mjpeg, a.serial))
    scheme = "http"
    if a.tls:
        import ssl
        import tempfile
        cpath, kpath = make_self_signed(tempfile.mkdtemp())
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cpath, kpath)
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
        scheme = "https"
    print(f"mock DVR στο {scheme}://127.0.0.1:{a.port}  (admin/test)  Ctrl+C για τέλος")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    print("στατιστικά:", STATS)


if __name__ == "__main__":
    main()

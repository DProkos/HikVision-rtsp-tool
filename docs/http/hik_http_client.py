#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Αυτόνομος πελάτης εικόνας HTTP για καταγραφικά Hikvision (ISAPI).

Χωρίς εξαρτήσεις πέρα από την standard library της Python 3.8+.

Δύο τρόποι:
  * MJPEG  : μία σύνδεση, το καταγραφικό στέλνει συνεχόμενα JPEG
             (GET /ISAPI/Streaming/channels/<id>/httpPreview)
  * Polling: μόνιμη σύνδεση keep-alive, επαναλαμβανόμενο
             GET /ISAPI/Streaming/channels/<id>/picture

Χρήση από γραμμή εντολών (μέτρηση fps και αποθήκευση καρέ):
    python hik_http_client.py --ip 192.168.1.64 --user admin --password ... \\
           --channel 1 --seconds 10 --fps 15 --save frame.jpg

Χρήση ως βιβλιοθήκη:
    from hik_http_client import HikHttpCamera
    cam = HikHttpCamera("192.168.1.64", "admin", "pass", channel=1)
    for jpeg in cam.frames(fps=15):
        ...  # bytes JPEG
"""
import argparse
import hashlib
import http.client
import os
import re
import time
from urllib.parse import urlsplit

__all__ = ["DigestKeepAliveClient", "HikHttpCamera"]


class DigestKeepAliveClient:
    """GET σε ένα URL με ΜΟΝΙΜΗ σύνδεση και HTTP Digest (RFC 2617, qop=auth).

    Το challenge αποθηκεύεται μετά το πρώτο 401 και κάθε επόμενο αίτημα
    στέλνει έτοιμο Authorization με αυξανόμενο nc. Έτσι κάθε καρέ κοστίζει
    ένα round-trip, χωρίς νέο TCP handshake και χωρίς δεύτερο αίτημα για login.
    """

    def __init__(self, url, user, password, timeout=5.0):
        p = urlsplit(url)
        self.host, self.port = p.hostname, p.port or 80
        self.path = p.path + (("?" + p.query) if p.query else "")
        self.user, self.password, self.timeout = user, password, timeout
        self.conn = None
        self.challenge = None
        self.nc = 0
        self.cnonce = hashlib.md5(os.urandom(8)).hexdigest()[:16]

    # -- σύνδεση
    def _connect(self):
        self.close()
        self.conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)

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

    @staticmethod
    def parse_challenge(header):
        return {k: (a or b) for k, a, b in re.findall(r'(\w+)=(?:"([^"]*)"|([^,\s]+))', header)}

    def _authorization(self, method="GET"):
        c = self.challenge
        self.nc += 1
        nc = f"{self.nc:08x}"
        ha1 = self._md5(f"{self.user}:{c['realm']}:{self.password}")
        ha2 = self._md5(f"{method}:{self.path}")
        if "auth" in (c.get("qop") or ""):
            resp = self._md5(f"{ha1}:{c['nonce']}:{nc}:{self.cnonce}:auth:{ha2}")
        else:
            resp = self._md5(f"{ha1}:{c['nonce']}:{ha2}")
        h = (f'Digest username="{self.user}", realm="{c["realm"]}", nonce="{c["nonce"]}", '
             f'uri="{self.path}", response="{resp}"')
        if "auth" in (c.get("qop") or ""):
            h += f', qop=auth, nc={nc}, cnonce="{self.cnonce}"'
        if c.get("opaque"):
            h += f', opaque="{c["opaque"]}"'
        return h

    # -- αιτήματα
    def _send(self, stream=False):
        headers = {"Connection": "keep-alive"}
        if self.challenge:
            headers["Authorization"] = self._authorization()
        self.conn.request("GET", self.path, headers=headers)
        r = self.conn.getresponse()
        if stream and r.status == 200:
            return r, None
        return r, r.read()

    def open(self, stream=False):
        """Στέλνει GET, χειρίζεται το 401 και την επανασύνδεση.
        stream=True: επιστρέφει την απάντηση χωρίς να τη διαβάσει (για MJPEG)."""
        if self.conn is None:
            self._connect()
        try:
            r, body = self._send(stream)
        except (http.client.HTTPException, OSError):
            self._connect()
            r, body = self._send(stream)
        if r.status == 401:
            self.challenge = self.parse_challenge(r.getheader("WWW-Authenticate", ""))
            r, body = self._send(stream)
        return r, body

    def get(self):
        r, body = self.open()
        if r.status != 200:
            raise OSError(f"HTTP {r.status}")
        return body


def iter_jpegs(response, chunk=16384, max_buffer=4_000_000):
    """Βγάζει ολόκληρα JPEG από ροή multipart (MJPEG) ψάχνοντας τα markers
    FFD8 (αρχή) και FFD9 (τέλος). Δεν βασίζεται στο boundary, που διαφέρει
    ανά firmware."""
    buf = b""
    while True:
        data = response.read(chunk)
        if not data:
            return
        buf += data
        while True:
            s = buf.find(b"\xff\xd8")
            e = buf.find(b"\xff\xd9", s + 2) if s >= 0 else -1
            if s < 0 or e < 0:
                break
            yield buf[s:e + 2]
            buf = buf[e + 2:]
        if len(buf) > max_buffer:
            buf = buf[-1_000_000:]


class HikHttpCamera:
    """Ένα κανάλι καταγραφικού Hikvision μέσω HTTP.

    channel: αριθμός καναλιού (1..16 αναλογικά, ή ip_base+N για IP κάμερες).
    """

    def __init__(self, ip, user, password, channel=1, port=80, timeout=5.0):
        self.base = f"http://{ip}:{port}"
        self.user, self.password = user, password
        self.channel, self.timeout = channel, timeout
        self.mode = None                 # "MJPEG" ή "JPEG" μετά την πρώτη επιτυχία

    def picture_url(self):
        return f"{self.base}/ISAPI/Streaming/channels/{self.channel}01/picture"

    def mjpeg_urls(self):
        return [f"{self.base}/ISAPI/Streaming/channels/{self.channel}02/httpPreview",
                f"{self.base}/ISAPI/Streaming/channels/{self.channel}01/httpPreview"]

    def _mjpeg(self):
        for url in self.mjpeg_urls():
            c = DigestKeepAliveClient(url, self.user, self.password, self.timeout)
            try:
                r, _ = c.open(stream=True)
            except Exception:
                c.close()
                continue
            if r.status == 200 and "multipart" in (r.getheader("Content-Type") or "").lower():
                return c, r
            try:
                r.read()
            except Exception:
                pass
            c.close()
        return None, None

    def probe(self):
        """Ελέγχει τι απαντά το καταγραφικό σε κάθε endpoint εικόνας.
        Επιστρέφει λίστα από dict: {url, status, content_type, jpeg, note}."""
        report = []
        for url in self.mjpeg_urls() + [self.picture_url()]:
            row = {"url": url, "status": None, "content_type": "", "jpeg": False, "note": ""}
            c = DigestKeepAliveClient(url, self.user, self.password, self.timeout)
            try:
                r, body = c.open(stream=True)      # body=None μόνο αν 200 + stream
                row["status"] = r.status
                ctype = (r.getheader("Content-Type") or "")
                row["content_type"] = ctype.split(";")[0]
                if r.status == 200 and "multipart" in ctype.lower():
                    chunk = r.read(65536)          # διάβασε λίγο: έρχονται JPEG;
                    row["jpeg"] = b"\xff\xd8" in chunk
                    row["note"] = "ροή MJPEG" if row["jpeg"] else "multipart χωρίς JPEG (ίσως H.264)"
                elif r.status == 200:
                    if body is None:               # stream mode: το σώμα δεν διαβάστηκε
                        body = r.read(65536)
                    row["jpeg"] = (body or b"")[:2] == b"\xff\xd8"
                    row["note"] = "ένα JPEG" if row["jpeg"] else "όχι JPEG"
                else:
                    txt = (body or b"").decode("utf-8", "replace")
                    detail = " · ".join(m.group(1).strip() for m in
                                        re.finditer(r"<(?:statusString|subStatusCode|errorMsg)>"
                                                    r"([^<]+)</", txt))
                    if r.status == 404:
                        row["note"] = "δεν υποστηρίζεται (404)"
                    else:
                        row["note"] = f"HTTP {r.status}" + (f" - {detail}" if detail else "")
            except Exception as e:
                row["note"] = f"σφάλμα: {str(e)[:50]}"
            finally:
                c.close()
            report.append(row)
        return report

    def frames(self, fps=15.0, try_mjpeg=True):
        """Γεννήτρια με bytes JPEG. Τρέχει μέχρι να τη σταματήσεις (break)."""
        if try_mjpeg:
            client, resp = self._mjpeg()
            if resp is not None:
                self.mode = "MJPEG"
                try:
                    yield from iter_jpegs(resp)
                finally:
                    client.close()
                return
        self.mode = "JPEG"
        client = DigestKeepAliveClient(self.picture_url(), self.user, self.password, self.timeout)
        interval = 1.0 / max(0.1, fps)
        try:
            while True:
                t0 = time.time()
                data = client.get()
                if data[:2] == b"\xff\xd8":
                    yield data
                rest = interval - (time.time() - t0)
                if rest > 0:
                    time.sleep(rest)
        finally:
            client.close()


def main():
    ap = argparse.ArgumentParser(description="Μέτρηση fps εικόνας HTTP από καταγραφικό Hikvision")
    ap.add_argument("--ip", required=True)
    ap.add_argument("--port", type=int, default=80)
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", default=os.environ.get("HIK_PASSWORD", ""))
    ap.add_argument("--channel", type=int, default=1)
    ap.add_argument("--fps", type=float, default=15)
    ap.add_argument("--seconds", type=float, default=10)
    ap.add_argument("--no-mjpeg", action="store_true", help="μόνο polling στιγμιοτύπων")
    ap.add_argument("--probe", action="store_true",
                    help="μόνο έλεγχος: τι υποστηρίζει το καταγραφικό (χωρίς μέτρηση)")
    ap.add_argument("--save", help="αποθήκευση του τελευταίου καρέ σε αρχείο")
    a = ap.parse_args()

    cam = HikHttpCamera(a.ip, a.user, a.password, a.channel, a.port)
    if a.probe:
        print(f"Έλεγχος endpoints εικόνας (κανάλι {a.channel}):")
        for row in cam.probe():
            tail = row["url"].split("/channels/")[-1]
            print(f"  {str(row['status']):>4}  {row['content_type']:<28} "
                  f"{tail:<22} {row['note']}")
        return
    n, size, last, t0 = 0, 0, None, time.time()
    for jpeg in cam.frames(fps=a.fps, try_mjpeg=not a.no_mjpeg):
        n += 1
        size += len(jpeg)
        last = jpeg
        if time.time() - t0 >= a.seconds:
            break
    dt = time.time() - t0
    print(f"τρόπος: {cam.mode}  καρέ: {n}  χρόνος: {dt:.1f}s  ->  {n / dt:.1f} fps, "
          f"μέσο μέγεθος {size / max(n, 1) / 1024:.1f} KB")
    if a.save and last:
        with open(a.save, "wb") as f:
            f.write(last)
        print("αποθηκεύτηκε:", a.save)


if __name__ == "__main__":
    main()

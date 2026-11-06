#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Hik RTSP Viewer - μικρό εργαλείο προβολής RTSP για καταγραφικό Hikvision DS-7216HUHI-K2.

RTSP URL Hikvision:
    rtsp://USER:PASS@IP:554/Streaming/Channels/<κανάλι><01|02>
        01 = κύρια ροή, 02 = δευτερεύουσα (sub) ροή
    π.χ. κάμερα A1 κύρια  -> /Streaming/Channels/101
         κάμερα A16 sub   -> /Streaming/Channels/1602
         IP κάμερα D4     -> /Streaming/Channels/(ΑΡΧΗ_IP+3)01  (π.χ. 2001 ή 3601)

Απαιτήσεις:  pip install PyQt5 av
            (προαιρετικά opencv-python ως εναλλακτικό, αν δεν υπάρχει το av)
"""
import json
import re
import os
import sys
import threading
import time
from datetime import datetime
import urllib.request
from urllib.parse import quote

# Σίγαση μηνυμάτων ffmpeg/OpenCV στην κονσόλα
os.environ.setdefault("OPENCV_FFMPEG_LOGLEVEL", "-8")
os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

try:
    import av  # PyAV: πιο σταθερό RTSP από το OpenCV
    try:
        # None = καμία καταγραφή, χωρίς Python callback (το callback μπορεί
        # να κλειδώσει με τα νήματα του αποκωδικοποιητή)
        av.logging.set_level(None)
    except Exception:
        pass
except ImportError:
    av = None
try:
    import cv2
except ImportError:
    cv2 = None
if av is None and cv2 is None:
    sys.exit("Λείπει βιβλιοθήκη βίντεο:  pip install av")

from PyQt5.QtCore import Qt, QEvent, QPoint, QRect, QSize, QThread, QTimer, pyqtSignal  # noqa: E402
from PyQt5.QtGui import QImage, QPixmap, QFont, QFontMetrics, QPainter, QColor, QKeySequence  # noqa: E402
from PyQt5.QtWidgets import (  # noqa: E402
    QApplication, QComboBox, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPushButton,
    QSpinBox, QSplitter, QVBoxLayout, QWidget, QSizePolicy, QShortcut, QLayout,
)

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(APP_DIR, "hik_viewer_config.json")
SNAP_DIR = os.path.join(APP_DIR, "snapshots")

# Κάμερες από το καταγραφικό (Αναλογικές A1-A16 + IP D4, D5)
DEFAULT_CAMERAS = [
    ("A1", "Camera 01"), ("A2", "APOTHIKI 2"), ("A3", "MESAIOS DIADROMOS 1"),
    ("A4", "FACE CONTROL"), ("A5", "EISODOS TAMEIA"), ("A6", "TAMEIA"),
    ("A7", "KENTRIKH OUT 1"), ("A8", "KENTRIKH OUT 2"), ("A9", "TSIGARA"),
    ("A10", "MANABIKH GONIA"), ("A11", "APOOTHIKI PISO"), ("A12", "KALINTIKA"),
    ("A13", "KAVA PSIGEIA"), ("A14", "MESAIOS DIADROMOS 2"),
    ("A15", "KATAPSIKTIS DELIVERY"), ("A16", "Camera 16"),
    ("D4", "IPCamera 04"), ("D5", "AKALYPTOS"),
]

DEFAULT_CONFIG = {
    "ip": "192.168.35.",
    "port": 554,
    "user": "admin",
    "password": "",
    "stream": "sub",        # sub | main | snapshot
    "layout": 4,            # 1, 4, 9, 16
    "ip_base": 16,          # D1 = ip_base+1  (δοκίμασε 32 αν δεν παίζουν οι IP)
    "http_port": 80,        # θύρα web, για τη λειτουργία Στιγμιότυπα HTTP
    "fps": 2,               # ρυθμός καρέ στη λειτουργία HTTP (στιγμιότυπα)
    "mjpeg": True,          # δοκίμασε ζωντανή ροή MJPEG (πιο real-time)
    "conns": 3,             # παράλληλες συνδέσεις ανά κάμερα (λειτουργία HTTP)
    "snap_res": "352x288",  # ανάλυση στιγμιοτύπου: "full" ή π.χ. "352x288" (πιο γρήγορο)
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("Αποτυχία αποθήκευσης ρυθμίσεων:", e)


CHANNEL_OVERRIDE = {}      # id κάμερας -> πραγματικός αριθμός καναλιού (από την ανίχνευση)
OFFLINE_IDS = set()        # IP κάμερες που το καταγραφικό δηλώνει εκτός (offline)


def apply_cameras(cams):
    """Αντικαθιστά τη λίστα καμερών (στη θέση της, ώστε να ισχύει παντού).
    cams: λίστα από dict {id, name, channel}."""
    DEFAULT_CAMERAS[:] = [(c["id"], c["name"]) for c in cams]
    CHANNEL_OVERRIDE.clear()
    CHANNEL_OVERRIDE.update({c["id"]: int(c["channel"]) for c in cams if c.get("channel")})
    OFFLINE_IDS.clear()
    OFFLINE_IDS.update(c["id"] for c in cams if c.get("online") is False)


def channel_number(cam_id, ip_base):
    """A5 -> 5,  D4 -> ip_base + 4  (εκτός αν η ανίχνευση ξέρει το πραγματικό κανάλι)"""
    if cam_id in CHANNEL_OVERRIDE:
        return CHANNEL_OVERRIDE[cam_id]
    n = int(cam_id[1:])
    return n if cam_id[0] == "A" else ip_base + n


def build_url(cfg, cam_id, stream):
    ch = channel_number(cam_id, cfg["ip_base"])
    st = "01" if stream == "main" else "02"
    user = quote(cfg["user"], safe="")
    pwd = quote(cfg["password"], safe="")
    return f"rtsp://{user}:{pwd}@{cfg['ip']}:{cfg['port']}/Streaming/Channels/{ch}{st}"


def snapshot_url(cfg, cam_id):
    ch = channel_number(cam_id, cfg["ip_base"])
    url = f"http://{cfg['ip']}:{cfg.get('http_port', 80)}/ISAPI/Streaming/channels/{ch}01/picture"
    res = cfg.get("snap_res", "full")
    if res and res != "full":                 # π.χ. "352x288": πιο γρήγορη εικόνα από το DVR
        w, h = res.split("x")
        url += f"?videoResolutionWidth={w}&videoResolutionHeight={h}"
    return url


def mjpeg_urls(cfg, cam_id):
    """Πιθανές διευθύνσεις ζωντανής ροής MJPEG (διαφέρουν ανά firmware)."""
    ch = channel_number(cam_id, cfg["ip_base"])
    host = f"http://{cfg['ip']}:{cfg.get('http_port', 80)}"
    return [
        f"{host}/ISAPI/Streaming/channels/{ch}02/httpPreview",
        f"{host}/ISAPI/Streaming/channels/{ch}01/httpPreview",
    ]


def make_http_opener(cfg):
    """Digest/Basic opener για τα στιγμιότυπα ISAPI."""
    base = f"http://{cfg['ip']}:{cfg.get('http_port', 80)}"
    mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    mgr.add_password(None, base, cfg["user"], cfg["password"])
    return urllib.request.build_opener(
        urllib.request.HTTPDigestAuthHandler(mgr),
        urllib.request.HTTPBasicAuthHandler(mgr),
    )


import hashlib        # noqa: E402
import http.client    # noqa: E402
from urllib.parse import urlsplit  # noqa: E402


class FastHttpClient:
    """Γρήγορος πελάτης HTTP με ΜΟΝΙΜΗ σύνδεση (keep-alive) και χειροκίνητο
    digest. Αποφεύγει το κόστος «νέα σύνδεση + login» σε κάθε καρέ, ώστε να
    πιάνουμε πολύ μεγαλύτερο ρυθμό από το απλό urllib."""

    def __init__(self, url, user, password, timeout=5):
        p = urlsplit(url)
        self.host, self.port = p.hostname, p.port or 80
        self.path = p.path + (("?" + p.query) if p.query else "")
        self.user, self.password, self.timeout = user, password, timeout
        self.conn = None
        self.ch = None                     # αποθηκευμένο digest challenge
        self.nc = 0
        self.cnonce = hashlib.md5(os.urandom(8)).hexdigest()[:16]

    def _connect(self):
        self.close()
        self.conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)

    def close(self):
        if self.conn:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None

    @staticmethod
    def _h(s):
        return hashlib.md5(s.encode()).hexdigest()

    def _auth_header(self):
        c = self.ch
        self.nc += 1
        ha1 = self._h(f"{self.user}:{c['realm']}:{self.password}")
        ha2 = self._h(f"GET:{self.path}")
        nc = f"{self.nc:08x}"
        if c.get("qop"):
            resp = self._h(f"{ha1}:{c['nonce']}:{nc}:{self.cnonce}:auth:{ha2}")
        else:
            resp = self._h(f"{ha1}:{c['nonce']}:{ha2}")
        a = (f'Digest username="{self.user}", realm="{c["realm"]}", nonce="{c["nonce"]}", '
             f'uri="{self.path}", response="{resp}"')
        if c.get("qop"):
            a += f', qop=auth, nc={nc}, cnonce="{self.cnonce}"'
        if c.get("opaque"):
            a += f', opaque="{c["opaque"]}"'
        return a

    def _request(self):
        headers = {"Connection": "keep-alive"}
        if self.ch:
            headers["Authorization"] = self._auth_header()
        self.conn.request("GET", self.path, headers=headers)
        r = self.conn.getresponse()
        body = r.read()
        return r, body

    def get(self):
        """Επιστρέφει bytes (JPEG) ή σηκώνει εξαίρεση."""
        if self.conn is None:
            self._connect()
        try:
            r, body = self._request()
        except Exception:
            self._connect()                # ξαναστήσε τη σύνδεση και ξαναδοκίμασε
            r, body = self._request()
        if r.status == 401:                # πρώτη φορά ή παλιό nonce: πάρε challenge
            www = r.getheader("WWW-Authenticate", "")
            self.ch = {k: (v1 or v2) for k, v1, v2 in
                       re.findall(r'(\w+)=(?:"([^"]+)"|([^,\s]+))', www)}
            r, body = self._request()
        if r.status != 200:
            raise OSError(f"HTTP {r.status}")
        return body


# --------------------------------------------------------------------------- #
# Ανίχνευση καμερών από το ίδιο το καταγραφικό (ISAPI)
def _strip_ns(xml):
    return re.sub(r'\sxmlns(?::\w+)?="[^"]*"', "", xml)


def _isapi_get(cfg, path, timeout=6):
    """(status, κείμενο). Δεν σηκώνει εξαίρεση για HTTP σφάλματα."""
    url = f"http://{cfg['ip']}:{cfg.get('http_port', 80)}{path}"
    c = FastHttpClient(url, cfg["user"], cfg["password"], timeout=timeout)
    try:
        return 200, c.get().decode("utf-8", "replace")
    except OSError as e:
        m = re.search(r"HTTP (\d+)", str(e))
        return (int(m.group(1)) if m else 0), ""
    except Exception:
        return 0, ""
    finally:
        c.close()


def _elements(xml, tag):
    """Όλα τα στοιχεία <tag> του XML (αγνοεί namespaces)."""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(_strip_ns(xml))
    except ET.ParseError:
        return []
    return list(root.iter(tag))


def _text(el, tag, default=""):
    f = el.find(tag)
    return (f.text or "").strip() if f is not None and f.text else default


def discover_cameras(cfg):
    """Ρωτά το καταγραφικό ποιες κάμερες έχει. Επιστρέφει
    {"cameras": [{id, name, channel, kind, online}], "analog_count": n,
     "log": [(path, status)], "raw": {path: κείμενο}}.  Δεν αλλάζει τίποτα στη συσκευή."""
    log, raw = [], {}

    def get(path):
        st, txt = _isapi_get(cfg, path)
        log.append((path, st))
        raw[path] = txt
        return txt if st == 200 else ""

    dev = get("/ISAPI/System/deviceInfo")
    m = re.search(r"<analogChannelNum>(\d+)</analogChannelNum>", dev)
    analog_count = int(m.group(1)) if m else 0

    # αναλογικές εισόδοι (id + όνομα)
    inputs = {}
    for el in _elements(get("/ISAPI/System/Video/inputs/channels"), "VideoInputChannel"):
        try:
            inputs[int(_text(el, "id"))] = _text(el, "name")
        except ValueError:
            pass

    # IP κάμερες (id + όνομα + διεύθυνση) και κατάσταση online
    proxies = {}
    for el in _elements(get("/ISAPI/ContentMgmt/InputProxy/channels"), "InputProxyChannel"):
        try:
            cid = int(_text(el, "id"))
        except ValueError:
            continue
        ip = ""
        d = el.find("sourceInputPortDescriptor")
        if d is not None:
            ip = _text(d, "ipAddress")
        proxies[cid] = (_text(el, "name"), ip)
    online = {}
    for el in _elements(get("/ISAPI/ContentMgmt/InputProxy/channels/status"), "InputProxyChannelStatus"):
        try:
            online[int(_text(el, "id"))] = _text(el, "online").lower() == "true"
        except ValueError:
            pass

    # κανάλια που ξέρει το σύστημα ροής (101, 102, 1701 ...) -> πραγματικοί αριθμοί καναλιών
    stream_channels = set()
    for el in _elements(get("/ISAPI/Streaming/channels"), "StreamingChannel"):
        v = _text(el, "id")
        if v.isdigit() and len(v) >= 3:
            stream_channels.add(int(v) // 100)

    if not analog_count:                     # fallback: όσα ανέφερε η λίστα εισόδων / ρύθμιση
        analog_count = (min(len(inputs), 16) if inputs and not proxies
                        else cfg.get("ip_base", 16))
    cams = []
    for n in sorted(inputs):
        if n <= analog_count:
            cams.append({"id": f"A{n}", "name": inputs[n] or f"Camera {n:02d}",
                         "channel": n, "kind": "analog", "online": None})
    if not inputs:                           # δεν δόθηκαν ονόματα: φτιάξε από τα κανάλια ροής
        for n in sorted(c for c in stream_channels if c <= analog_count):
            cams.append({"id": f"A{n}", "name": f"Camera {n:02d}", "channel": n,
                         "kind": "analog", "online": None})
    # αντιστοίχιση IP καμερών -> κανάλι ροής
    ip_streams = sorted(c for c in stream_channels if c > analog_count)
    ids = sorted(proxies)
    for k, cid in enumerate(ids):
        ch = ip_streams[k] if len(ip_streams) == len(ids) else analog_count + cid
        name, ipaddr = proxies[cid]
        cams.append({"id": f"D{cid}", "name": name or f"IPCamera {cid:02d}",
                     "channel": ch, "kind": "ip", "ip": ipaddr,
                     "online": online.get(cid)})
    return {"cameras": cams, "analog_count": analog_count, "log": log, "raw": raw}


class DiscoverWorker(QThread):
    done = pyqtSignal(object)

    def __init__(self, cfg):
        super().__init__()
        self.cfg = dict(cfg)

    def run(self):
        try:
            res = discover_cameras(self.cfg)
        except Exception as e:                       # δίκτυο κλπ.
            res = {"cameras": [], "analog_count": 0, "log": [], "raw": {}, "error": str(e)}
        self.done.emit(res)


# --------------------------------------------------------------------------- #
class HttpSnapshotWorker(QThread):
    """Ζωντανή εικόνα χωρίς RTSP, μέσω του δρόμου HTTP που ΔΕΝ κρυπτογραφείται.
    Δοκιμάζει πρώτα ζωντανή ροή εικόνων MJPEG (πιο real-time) και, αν δεν την
    υποστηρίζει το καταγραφικό, πέφτει σε γρήγορα στιγμιότυπα JPEG."""

    def __init__(self, cfg, cam_id, fps=2.0, want_mjpeg=True, conns=1):
        super().__init__()
        self.conns = max(1, int(conns))       # παράλληλες συνδέσεις στην ίδια κάμερα
        self._lock = threading.Lock()
        self._last_start = 0.0                # ώστε να μην εμφανίζεται παλιότερο καρέ
        self.pic_url = snapshot_url(cfg, cam_id)
        self.mjpeg_urls = mjpeg_urls(cfg, cam_id)
        self.opener = make_http_opener(cfg)
        self.user, self.password = cfg["user"], cfg["password"]
        self.interval = 1.0 / max(0.2, fps)
        self.want_mjpeg = want_mjpeg
        self._run = True
        self.latest = None
        self.seq = 0
        self.status = "Σύνδεση..."
        self.info = ""
        self.loss = 0
        self.target = (640, 360)

    def stop(self):
        self._run = False

    def _reset_auth(self):
        for h in self.opener.handlers:
            if hasattr(h, "reset_retry_count"):
                h.reset_retry_count()

    def _emit_jpeg(self, data, tag, started=None):
        img = QImage()
        if data[:2] == b"\xff\xd8" and img.loadFromData(data, "JPG") and not img.isNull():
            rgb = img.convertToFormat(QImage.Format_RGB888)
            with self._lock:
                if started is not None:
                    if started < self._last_start:     # ήρθε μετά από νεότερο καρέ: πέταξέ το
                        return True
                    self._last_start = started
                self.latest = rgb
                self.seq += 1
            self.status = ""
            now = time.time()
            if not hasattr(self, "_fps_t"):
                self._fps_t, self._fps_n, self.real_fps = now, self.seq, 0.0
            elif now - self._fps_t >= 2:
                self.real_fps = (self.seq - self._fps_n) / (now - self._fps_t)
                self._fps_t, self._fps_n = now, self.seq
            fps_txt = f"  {self.real_fps:.1f} fps" if self.real_fps else ""
            lat = getattr(self, "lat_ms", 0)
            lat_txt = f"  ({lat:.0f} ms/καρέ)" if lat else ""
            self.info = f"{tag} {img.width()}x{img.height()}{fps_txt}{lat_txt}"
            return True
        return False

    def run(self):
        # 1) Προσπάθεια για ζωντανή ροή MJPEG (πιο real-time)
        if self.want_mjpeg:
            for url in self.mjpeg_urls:
                if not self._run:
                    return
                if self._run_mjpeg(url):
                    return                       # η ροή έτρεξε και τελείωσε/διακόπηκε
        # 2) Εναλλακτικά: γρήγορα στιγμιότυπα
        self._run_polling()

    def _run_mjpeg(self, url):
        """Επιστρέφει True αν ήταν όντως ροή MJPEG (ώστε να μη δοκιμαστεί polling)."""
        try:
            self._reset_auth()
            r = self.opener.open(url, timeout=8)
        except Exception:
            return False
        ctype = (r.headers.get("Content-Type") or "").lower()
        if "multipart" not in ctype:
            r.close()
            return False
        self.status = ""
        buf = b""
        got = 0
        try:
            while self._run:
                chunk = r.read(16384)
                if not chunk:
                    break
                buf += chunk
                # βγάλε κάθε ολόκληρο JPEG (ffd8 ... ffd9) από τον buffer
                while True:
                    s = buf.find(b"\xff\xd8")
                    e = buf.find(b"\xff\xd9", s + 2) if s >= 0 else -1
                    if s < 0 or e < 0:
                        break
                    if self._emit_jpeg(buf[s:e + 2], "MJPEG"):
                        got += 1
                    buf = buf[e + 2:]
                if len(buf) > 4_000_000:         # προστασία, αν δεν βρίσκει όρια
                    buf = buf[-1_000_000:]
        except Exception:
            pass
        finally:
            r.close()
        return got > 0

    def _run_polling(self):
        """Μία ή περισσότερες παράλληλες συνδέσεις στην ίδια κάμερα. Το καταγραφικό
        εξυπηρετεί αιτήματα παράλληλα, οπότε N συνδέσεις δίνουν ~N φορές τα καρέ.
        Οι συνδέσεις ξεκινούν μετατοπισμένες ώστε τα καρέ να έρχονται ισόποσα."""
        helpers = []
        for k in range(1, self.conns):
            t = threading.Thread(target=self._poll_loop, args=(k,), daemon=True)
            helpers.append(t)
        self.lat_ms = 0
        for t in helpers:
            t.start()
        self._poll_loop(0)
        for t in helpers:
            t.join(timeout=2)

    def _poll_loop(self, index):
        fails = 0
        client = FastHttpClient(self.pic_url, self.user, self.password)
        per_conn_interval = self.interval * self.conns     # ρυθμός ανά σύνδεση
        if index:
            time.sleep(0.12 * index)                       # μετατόπιση εκκίνησης
        while self._run:
            t0 = time.time()
            try:
                data = client.get()
                dt = (time.time() - t0) * 1000
                self.lat_ms = dt if not getattr(self, "lat_ms", 0) else 0.8 * self.lat_ms + 0.2 * dt
                if self._emit_jpeg(data, "JPEG", started=t0):
                    fails = 0
                else:
                    fails += 1
                    self.status = "Μη έγκυρη εικόνα"
            except Exception as e:
                fails += 1
                msg = str(e)
                if "401" in msg or "403" in msg:
                    self.status = "Λάθος κωδικός/χρήστης"
                elif "404" in msg:
                    self.status = "Η κάμερα δεν δίνει στιγμιότυπο"
                else:
                    self.status = "Σφάλμα σύνδεσης"
            wait = max(per_conn_interval, 2.0) if fails else per_conn_interval
            end = time.time() + max(0.0, wait - (time.time() - t0))
            while self._run and time.time() < end:
                time.sleep(0.005)
        client.close()


# --------------------------------------------------------------------------- #
class StreamWorker(QThread):
    """Διαβάζει τη ροή σε δικό του thread και κρατά ΜΟΝΟ το τελευταίο καρέ.
    Η οθόνη το παίρνει με timer, οπότε ο αποκωδικοποιητής δεν μένει ποτέ πίσω
    (αυτό χαλούσε την εικόνα: 'non-existing PPS', 'bad cseq' κλπ)."""

    def __init__(self, url):
        super().__init__()
        self.url = url
        self._run = True
        self.latest = None        # QImage
        self.seq = 0              # αυξάνει σε κάθε νέο καρέ
        self.status = "Σύνδεση..."
        self.target = (640, 360)  # μέγεθος κελιού -> κλίμακα στην αποκωδικοποίηση
        self.info = ""            # κωδικοποίηση / ανάλυση ροής
        self.loss = 0             # % χαλασμένων πακέτων/καρέ

    def stop(self):
        self._run = False

    def run(self):
        while self._run:
            self.status = "Σύνδεση..."
            try:
                if av is not None:
                    self._run_av()
                else:
                    self._run_cv()
            except Exception as e:
                self.status = f"Σφάλμα: {str(e)[:60]}"
            if self._run:
                if self.latest is not None:
                    self.status = "Χάθηκε η ροή - επανασύνδεση"
                self._sleep(3)

    def _fit(self, w, h):
        tw, th = self.target
        if w <= 0 or h <= 0:
            return w, h
        s = min(tw / w, th / h, 1.0)
        return max(2, int(w * s) // 2 * 2), max(2, int(h * s) // 2 * 2)

    def _publish(self, rgb_bytes, w, h):
        self.latest = QImage(rgb_bytes, w, h, 3 * w, QImage.Format_RGB888).copy()
        self.seq += 1
        self.status = ""

    def _run_av(self):
        container = av.open(self.url, options={
            "rtsp_transport": "tcp",
            "max_delay": "500000",
            "buffer_size": "4194304",
            "reorder_queue_size": "64",
        }, timeout=(10, 10))
        try:
            stream = container.streams.video[0]
            stream.thread_type = "AUTO"
            ctx = stream.codec_context
            self.info = f"{ctx.name.upper()} {ctx.width}x{ctx.height}"
            need_key = True          # περιμένουμε καθαρό κεντρικό καρέ
            good = bad = 0
            t0 = time.time()
            for packet in container.demux(stream):
                if not self._run:
                    break
                if packet.size == 0:
                    continue
                if packet.is_corrupt:
                    bad += 1
                    need_key = True
                    continue
                if need_key:
                    if not packet.is_keyframe:
                        bad += 1
                        continue
                    need_key = False             # από εδώ και πέρα ξανά κανονικά
                try:
                    frames = packet.decode()
                except Exception:
                    bad += 1
                    need_key = True
                    continue
                for frame in frames:
                    if frame.is_corrupt:
                        bad += 1
                        need_key = True          # κρατάμε την τελευταία καλή εικόνα
                        continue
                    good += 1
                    w, h = self._fit(frame.width, frame.height)
                    arr = frame.reformat(width=w, height=h, format="rgb24").to_ndarray()
                    self._publish(arr.tobytes(), w, h)
                # στατιστικά κάθε 5"
                if time.time() - t0 > 5:
                    tot = good + bad
                    self.loss = int(100 * bad / tot) if tot else 0
                    good = bad = 0
                    t0 = time.time()
                    if self.latest is None:
                        self.status = "Αναμονή καθαρού καρέ..."
        finally:
            container.close()

    def _run_cv(self):
        cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
        try:
            if not cap.isOpened():
                self.status = "Αποτυχία σύνδεσης"
                return
            fails = 0
            while self._run and fails < 50:
                ok, img = cap.read()
                if not ok or img is None:
                    fails += 1
                    time.sleep(0.02)
                    continue
                fails = 0
                h, w = img.shape[:2]
                nw, nh = self._fit(w, h)
                if (nw, nh) != (w, h):
                    img = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA)
                rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
                self._publish(rgb.tobytes(), nw, nh)
        finally:
            cap.release()

    def _sleep(self, sec):
        end = time.time() + sec
        while self._run and time.time() < end:
            time.sleep(0.1)


class FlowLayout(QLayout):
    """Διάταξη που ΣΠΑΕΙ σε πολλές γραμμές όταν δεν χωράνε τα στοιχεία (σαν κείμενο)."""

    def __init__(self, parent=None, hspacing=8, vspacing=4):
        super().__init__(parent)
        self._items = []
        self._h, self._v = hspacing, vspacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientations(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, w):
        return self._layout(QRect(0, 0, w, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._layout(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for it in self._items:
            if not it.isEmpty():
                size = size.expandedTo(it.minimumSize())
        m = self.contentsMargins()
        return size + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _layout(self, rect, test_only):
        m = self.contentsMargins()
        r = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y, line_h = r.x(), r.y(), 0
        for it in self._items:
            if it.isEmpty():                      # κρυμμένο widget
                continue
            hint = it.sizeHint()
            if x + hint.width() > r.right() + 1 and line_h > 0:
                x = r.x()
                y += line_h + self._v
                line_h = 0
            if not test_only:
                it.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self._h
            line_h = max(line_h, hint.height())
        return y + line_h - rect.y() + m.bottom()


# --------------------------------------------------------------------------- #
class VideoTile(QLabel):
    doubleClicked = pyqtSignal(object)
    clicked = pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumSize(160, 90)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.setStyleSheet("background:#111;color:#bbb;border:1px solid #333;")
        self.worker = None
        self.cam = None          # (id, name)
        self.last_image = None
        self._seq = -1
        self._status = None
        self.selected = False
        self.show_text("—")

    # ---- παρουσίαση
    def show_text(self, txt):
        self.setPixmap(QPixmap())
        self.setText(txt)

    def caption(self):
        return f"{self.cam[0]}  {self.cam[1]}" if self.cam else ""

    def set_selected(self, sel):
        self.selected = sel
        color = "#d32f2f" if sel else "#333"
        self.setStyleSheet(f"background:#111;color:#bbb;border:2px solid {color};")

    # ---- ροή
    def play(self, cam, worker):
        self.stop()
        self.cam = cam
        self.show_text(self.caption() + "\nΣύνδεση...")
        self.worker = worker
        self.worker.target = (max(160, self.width()), max(90, self.height()))
        self.worker.start()

    def stop(self):
        if self.worker:
            w = self.worker
            w.stop()
            # μην μπλοκάρουμε το UI: το thread τερματίζει μόνο του (timeout 10s)
            w.finished.connect(w.deleteLater)
            _orphans.add(w)
            w.finished.connect(lambda w=w: _orphans.discard(w))
            self.worker = None
        self.cam = None
        self.last_image = None
        self._seq = -1
        self._status = None
        self.show_text("—")

    def poll(self):
        """Καλείται από timer του κεντρικού παραθύρου."""
        w = self.worker
        if not w:
            return
        if w.seq != self._seq and w.latest is not None:
            self._seq = w.seq
            self.last_image = w.latest
            self._status = w.status
            self._render()
        elif w.status != self._status:
            self._status = w.status
            if self.last_image is None:
                self.show_text(self.caption() + "\n" + w.status)
            else:
                self._render()

    def _render(self):
        if self.last_image is None:
            return
        pm = QPixmap.fromImage(self.last_image)
        avail = self.size() - QSize(4, 4)                  # μικρό περιθώριο για το πλαίσιο
        if avail.width() > 8 and avail.height() > 8 and pm.size() != avail:
            # γεμίζει το κελί διατηρώντας τις αναλογίες (και μεγαλώνει, π.χ. σε full screen)
            upscale = pm.width() < avail.width() and pm.height() < avail.height()
            pm = pm.scaled(avail, Qt.KeepAspectRatio,
                           Qt.SmoothTransformation if upscale else Qt.FastTransformation)
        p = QPainter(pm)
        fs = max(9, min(28, pm.height() // 34))             # μέγεθος γραμμάτων ~ μέγεθος κελιού
        txt = self.caption()
        p.setPen(QColor("white"))
        w = self.worker
        if w and w.info:
            txt += f"   {w.info}"
        if w and w.loss:
            txt += f"   σφάλματα {w.loss}%"
            p.setPen(QColor("#ff6b6b") if w.loss > 5 else QColor("white"))
        if self._status:
            txt += f"  [{self._status}]"
        # μίκρυνε τα γράμματα μέχρι να χωρέσει όλο το κείμενο στο πλάτος της εικόνας
        font = QFont("Arial", fs, QFont.Bold)
        while fs > 7 and QFontMetrics(font).horizontalAdvance(txt) > pm.width() - 12:
            fs -= 1
            font = QFont("Arial", fs, QFont.Bold)
        txt = QFontMetrics(font).elidedText(txt, Qt.ElideRight, pm.width() - 12)   # «…» αν δεν χωράει
        bar_h = int(fs * 2.1)
        p.fillRect(0, 0, pm.width(), bar_h, QColor(0, 0, 0, 140))
        p.setFont(font)
        p.drawText(6, int(bar_h * 0.72), txt)
        p.end()
        self.setPixmap(pm)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self.worker:
            self.worker.target = (max(160, self.width()), max(90, self.height()))
        self._render()

    def mousePressEvent(self, e):
        self.clicked.emit(self)

    def mouseDoubleClickEvent(self, e):
        self.doubleClicked.emit(self)


_orphans = set()   # threads που σταματούν στο παρασκήνιο

# --------------------------------------------------------------------------- #
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Hik RTSP Viewer - DS-7216HUHI-K2")
        self.resize(1400, 820)
        self.cfg = load_config()
        if self.cfg.get("cameras"):                 # λίστα από προηγούμενη ανίχνευση
            try:
                apply_cameras(self.cfg["cameras"])
            except Exception:
                pass
        self.tiles = []
        self.page = 0
        self.selected_tile = None
        self.single_mode = None   # (cam, προηγούμενο layout, σελίδα) όταν είμαστε σε μονή προβολή

        # ---------- πάνω μπάρα
        self.ed_ip = QLineEdit(self.cfg["ip"]); self.ed_ip.setFixedWidth(130)
        self.sp_port = QSpinBox(); self.sp_port.setRange(1, 65535); self.sp_port.setValue(self.cfg["port"])
        self.ed_user = QLineEdit(self.cfg["user"]); self.ed_user.setFixedWidth(90)
        self.ed_pass = QLineEdit(self.cfg["password"]); self.ed_pass.setEchoMode(QLineEdit.Password)
        self.ed_pass.setFixedWidth(110)
        self.cb_stream = QComboBox()
        self.cb_stream.addItems(["Δευτερεύουσα (sub)", "Κύρια (main)", "Στιγμιότυπα HTTP"])
        self.cb_stream.setCurrentIndex(
            {"sub": 0, "main": 1, "snapshot": 2}.get(self.cfg["stream"], 0))
        self.cb_stream.setToolTip("Στιγμιότυπα HTTP: ζωντανή εικόνα ~1 fps μέσω της θύρας web,\n"
                                  "για όταν η ροή RTSP είναι κρυπτογραφημένη.")
        self.sp_http = QSpinBox(); self.sp_http.setRange(1, 65535)
        self.sp_http.setValue(self.cfg.get("http_port", 80))
        self.sp_http.setToolTip("Θύρα web (HTTP) του καταγραφικού - για τα Στιγμιότυπα HTTP.")
        self.sp_conns = QSpinBox(); self.sp_conns.setRange(1, 8)
        self.sp_conns.setValue(self.cfg.get("conns", 3))
        self.sp_conns.setToolTip("Παράλληλες συνδέσεις ανά κάμερα (λειτουργία HTTP).\n"
                                 "Το καταγραφικό τις εξυπηρετεί παράλληλα, άρα περισσότερα fps.\n"
                                 "Πάνω από ~4-6 το κέρδος μικραίνει. Σε πλέγμα 9/16 χρησιμοποιείται 1.")
        self.cb_res = QComboBox(); self.cb_res.addItems(["Πλήρης", "352x288"])
        self.cb_res.setCurrentIndex(0 if self.cfg.get("snap_res", "352x288") == "full" else 1)
        self.cb_res.setToolTip("Ανάλυση στιγμιοτύπου. Η μικρότερη φτιάχνεται πιο γρήγορα από το\n"
                               "καταγραφικό (περισσότερα fps) αλλά είναι πιο θολή.")
        self.sp_fps = QSpinBox(); self.sp_fps.setRange(1, 30)
        self.sp_fps.setValue(self.cfg.get("fps", 2))
        self.sp_fps.setToolTip("Ρυθμός καρέ για τη λειτουργία Στιγμιότυπα HTTP.\n"
                               "Μεγαλύτερος = πιο real-time, αλλά φορτώνει το καταγραφικό.")
        self.cb_layout = QComboBox(); self.cb_layout.addItems(["1 κάμερα", "4", "9", "16"])
        self.cb_layout.setCurrentIndex([1, 4, 9, 16].index(self.cfg["layout"])
                                       if self.cfg["layout"] in (1, 4, 9, 16) else 1)
        self.cb_layout.setToolTip("«1 κάμερα»: μία κάμερα σε όλο το παράθυρο, με τη μέγιστη\n"
                                  "ταχύτητα. Άλλαξε κάμερα με ◀ ▶ ή διπλό κλικ στη λίστα.")
        self.sp_ipbase = QSpinBox(); self.sp_ipbase.setRange(0, 64); self.sp_ipbase.setValue(self.cfg["ip_base"])
        self.sp_ipbase.setToolTip("Αρίθμηση RTSP IP καναλιών: D1 = τιμή+1.\n"
                                  "Αν οι D4/D5 δεν παίζουν με 16, δοκίμασε 32.")

        btn_connect = QPushButton("▶ Προβολή"); btn_connect.clicked.connect(self.start_all)
        btn_stop = QPushButton("■ Στοπ"); btn_stop.clicked.connect(self.stop_all)
        btn_prev = QPushButton("◀"); btn_prev.clicked.connect(lambda: self.change_page(-1))
        btn_next = QPushButton("▶"); btn_next.clicked.connect(lambda: self.change_page(1))
        btn_snap = QPushButton("📷 Στιγμιότυπο"); btn_snap.clicked.connect(self.snapshot)
        btn_url = QPushButton("URL"); btn_url.clicked.connect(self.show_url)
        btn_full = QPushButton("⛶ Πλήρης οθόνη"); btn_full.clicked.connect(self.toggle_fullscreen)
        btn_full.setToolTip("F11: το πρόγραμμα γεμίζει όλη την οθόνη (μένουν όλα τα στοιχεία).\n"
                            "Esc ή F11: έξοδος.  PageUp / PageDown: προηγούμενη / επόμενη κάμερα ή σελίδα.")
        btn_clean = QPushButton("▣ Μόνο εικόνα"); btn_clean.clicked.connect(self.toggle_clean)
        btn_clean.setToolTip("F10: κρύβει μπάρα, λίστα και μπάρα κατάστασης, μένουν μόνο οι κάμερες.\n"
                             "Ξανά F10 ή Esc για επιστροφή.")
        self.lbl_page = QLabel()
        btn_connect.setStyleSheet("background:#d32f2f;color:white;font-weight:bold;padding:4px 12px;")
        self.btn_connect = btn_connect

        top = FlowLayout()
        self._field_boxes = {}               # πεδίο -> κουτί (ετικέτα+πεδίο) για απόκρυψη/εμφάνιση
        for lbl, w in [("IP:", self.ed_ip), ("RTSP:", self.sp_port), ("Χρήστης:", self.ed_user),
                       ("Κωδικός:", self.ed_pass), ("Ροή:", self.cb_stream),
                       ("HTTP:", self.sp_http), ("fps:", self.sp_fps),
                       ("συνδ.:", self.sp_conns), ("Ανάλ.:", self.cb_res),
                       ("Πλέγμα:", self.cb_layout), ("Αρ.Κάμερον:", self.sp_ipbase)]:
            box = QWidget(); hb = QHBoxLayout(box); hb.setContentsMargins(0, 0, 0, 0); hb.setSpacing(3)
            hb.addWidget(QLabel(lbl)); hb.addWidget(w)
            self._field_boxes[w] = box
            top.addWidget(box)
        for w in (btn_connect, btn_stop, btn_prev, self.lbl_page, btn_next, btn_snap,
                  btn_full, btn_clean, btn_url):
            top.addWidget(w)

        # ---------- λίστα καμερών
        self.cam_list = QListWidget()
        self.cam_list.setMaximumWidth(250)
        self.populate_camera_list()
        self.cam_list.itemDoubleClicked.connect(self.on_list_double)
        self.cam_list.setToolTip("Διπλό κλικ: παίζει την κάμερα στο επιλεγμένο κελί\n"
                                 "(ή σε μονή προβολή αν δεν έχει επιλεγεί κελί)")

        # ---------- πλέγμα
        self.grid_host = QWidget()
        self.grid_host.setStyleSheet("background:#000;")
        self.grid = QGridLayout(self.grid_host)
        self.grid.setSpacing(2); self.grid.setContentsMargins(2, 2, 2, 2)

        splitter = QSplitter()
        left = QWidget(); lv = QVBoxLayout(left); lv.setContentsMargins(0, 0, 0, 0)
        self.ed_search = QLineEdit()
        self.ed_search.setPlaceholderText("🔍 Αναζήτηση κάμερας (όνομα ή A6)...")
        self.ed_search.setClearButtonEnabled(True)
        self.ed_search.setToolTip("Γράψε μέρος του ονόματος ή του κωδικού (π.χ. tameia, A6, D5).\n"
                                  "Enter: άνοιγμα της πρώτης κάμερας που βρέθηκε.")
        self.ed_search.textChanged.connect(self.filter_cameras)
        self.ed_search.returnPressed.connect(self.open_first_match)
        self.lbl_found = QLabel("")
        lv.addWidget(QLabel("<b>Κάμερες</b>")); lv.addWidget(self.ed_search)
        self.btn_discover = QPushButton("🔍 Εύρεση καμερών από το καταγραφικό")
        self.btn_discover.setToolTip("Ρωτά το καταγραφικό ποιες κάμερες έχει (ονόματα, IP κάμερες,\n"
                                     "κατάσταση) και φτιάχνει τη λίστα μόνο του. Δεν αλλάζει τίποτα\n"
                                     "στη συσκευή. Χρησιμοποιεί τη θύρα HTTP (web).")
        self.btn_discover.clicked.connect(self.discover)
        lv.addWidget(self.cam_list); lv.addWidget(self.lbl_found); lv.addWidget(self.btn_discover)
        splitter.addWidget(left); splitter.addWidget(self.grid_host)
        splitter.setStretchFactor(1, 1)

        central = QWidget(); v = QVBoxLayout(central)
        self.top_bar = QWidget(); self.top_bar.setLayout(top)
        top.setContentsMargins(0, 0, 0, 0)
        self.left_panel = left
        v.addWidget(self.top_bar); v.addWidget(splitter, 1)
        self.setCentralWidget(central)
        self._fs_saved = None
        self._clean = False
        self._scale = 1.0
        self._base_pt = QApplication.font().pointSizeF()
        if self._base_pt <= 0:
            self._base_pt = 9.0
        self._scale_timer = QTimer(self); self._scale_timer.setSingleShot(True)
        self._scale_timer.timeout.connect(self.apply_scale)
        QShortcut(QKeySequence("F10"), self, activated=self.toggle_clean)
        QShortcut(QKeySequence("F11"), self, activated=self.toggle_fullscreen)
        QShortcut(QKeySequence("Esc"), self, activated=self.exit_fullscreen)
        QShortcut(QKeySequence("PgDown"), self, activated=lambda: self.change_page(1))
        QShortcut(QKeySequence("PgUp"), self, activated=lambda: self.change_page(-1))
        self.statusBar().showMessage("Συμπλήρωσε IP/κωδικό του καταγραφικού και πάτα Προβολή. "
                                     "Διπλό κλικ σε κελί = μεγέθυνση / επιστροφή.")

        self.cb_layout.currentTextChanged.connect(lambda _: self.relayout(reset_page=True))
        self.cb_stream.currentIndexChanged.connect(lambda _: self.restart_if_running())
        self.cb_stream.currentIndexChanged.connect(lambda _: self.apply_mode_fields())
        self.apply_mode_fields()
        self.running = False
        self.build_tiles(self.layout_n())

        # Ανανέωση οθόνης ~12 fps από το τελευταίο καρέ κάθε κάμερας
        self.timer = QTimer(self)
        self.timer.timeout.connect(lambda: [t.poll() for t in self.tiles])
        self.timer.start(80)

    # ---------- ρυθμίσεις
    def read_cfg(self):
        self.cfg.update({
            "ip": self.ed_ip.text().strip(),
            "port": self.sp_port.value(),
            "user": self.ed_user.text().strip(),
            "password": self.ed_pass.text(),
            "stream": ["sub", "main", "snapshot"][self.cb_stream.currentIndex()],
            "layout": self.layout_n(),
            "ip_base": self.sp_ipbase.value(),
            "http_port": self.sp_http.value(),
            "fps": self.sp_fps.value(),
            "mjpeg": self.cfg.get("mjpeg", True),
            "conns": self.sp_conns.value(),
            "snap_res": "full" if self.cb_res.currentIndex() == 0 else "352x288",
        })
        save_config(self.cfg)
        return self.cfg

    def make_worker(self, cfg, cam_id, stream, fps=None, conns=None):
        """Φτιάχνει τον σωστό worker ανάλογα με τη λειτουργία."""
        if stream == "snapshot":
            return HttpSnapshotWorker(cfg, cam_id,
                                      fps=fps if fps else cfg.get("fps", 2),
                                      want_mjpeg=cfg.get("mjpeg", True),
                                      conns=conns if conns else 1)
        return StreamWorker(build_url(cfg, cam_id, stream))

    # ---------- πλέγμα
    def build_tiles(self, n):
        for t in self.tiles:
            t.stop(); t.deleteLater()
        self.tiles = []
        self.selected_tile = None
        cols = {1: 1, 4: 2, 9: 3, 16: 4}[n]
        for i in range(n):
            t = VideoTile()
            t.doubleClicked.connect(self.on_tile_double)
            t.clicked.connect(self.on_tile_click)
            self.grid.addWidget(t, i // cols, i % cols)
            self.tiles.append(t)
        for c in range(4):
            self.grid.setColumnStretch(c, 1 if c < cols else 0)
            self.grid.setRowStretch(c, 1 if c < cols else 0)

    def pages(self):
        n = len(self.tiles)
        return max(1, (len(DEFAULT_CAMERAS) + n - 1) // n)

    def relayout(self, reset_page=False):
        self.single_mode = None
        if reset_page:
            self.page = 0
        self.build_tiles(self.layout_n())
        if self.running:
            self.play_page()
        self.update_page_label()

    def toggle_fullscreen(self):
        """F11: το παράθυρο γεμίζει την οθόνη. Όλα τα στοιχεία μένουν και προσαρμόζονται."""
        if self.isFullScreen():
            self.exit_fullscreen()
        else:
            self._fs_saved = {"max": self.isMaximized(), "geo": self.geometry()}
            self.showFullScreen()

    def exit_fullscreen(self):
        """Esc: πρώτα βγαίνει από το «Μόνο εικόνα», μετά από το full screen."""
        if self._clean:
            self.toggle_clean()
            return
        if self.isFullScreen():
            if self._fs_saved and self._fs_saved["max"]:
                self.showMaximized()
            else:
                self.showNormal()
                if self._fs_saved:
                    self.setGeometry(self._fs_saved["geo"])

    def toggle_clean(self):
        """F10: μόνο οι κάμερες (κρύβει μπάρα, λίστα, μπάρα κατάστασης)."""
        self._clean = not self._clean
        for wdg in (self.top_bar, self.left_panel):
            wdg.setVisible(not self._clean)
        self.statusBar().setVisible(not self._clean)
        m = 0 if self._clean else 9
        self.centralWidget().layout().setContentsMargins(m, m, m, m)
        self.grid.setSpacing(1 if self._clean else 2)

    # ---- κλιμάκωση του περιβάλλοντος με το μέγεθος του παραθύρου
    def apply_scale(self):
        scale = max(1.0, min(2.2, self.width() / 1500.0))
        if abs(scale - self._scale) < 0.06:
            return
        self._scale = scale
        f = QApplication.font()
        f.setPointSizeF(self._base_pt * scale)
        QApplication.setFont(f)
        # τα widgets με stylesheet δεν παίρνουν μόνα τους τη νέα γραμματοσειρά
        self.btn_connect.setFont(f)
        self.btn_connect.setStyleSheet(self.btn_connect.styleSheet())
        self.cam_list.setMaximumWidth(int(250 * scale))
        for t in self.tiles:
            t._render()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._scale_timer.start(120)            # μετά το τέλος του resize, όχι σε κάθε pixel

    def populate_camera_list(self):
        self.cam_list.clear()
        for cid, name in DEFAULT_CAMERAS:
            off = cid in OFFLINE_IDS
            it = QListWidgetItem(f"{cid}  {name}" + ("  ⚠ εκτός" if off else ""))
            it.setData(Qt.UserRole, (cid, name))
            if off:
                it.setToolTip("Το καταγραφικό δηλώνει ότι αυτή η IP κάμερα είναι εκτός (offline).")
            self.cam_list.addItem(it)

    def discover(self):
        cfg = self.read_cfg()
        if not cfg["ip"] or cfg["ip"].endswith("."):
            QMessageBox.warning(self, "IP", "Συμπλήρωσε τη διεύθυνση IP του καταγραφικού.")
            return
        self.btn_discover.setEnabled(False)
        self.statusBar().showMessage(f"Ρωτάω το καταγραφικό ({cfg['ip']}:{cfg.get('http_port', 80)}) "
                                     "ποιες κάμερες έχει...")
        self._disc = DiscoverWorker(cfg)
        self._disc.done.connect(self.on_discovered)
        self._disc.start()

    def on_discovered(self, res):
        self.btn_discover.setEnabled(True)
        # αναφορά για διάγνωση (χωρίς κωδικούς), π.χ. αν το firmware έχει άλλη μορφή
        try:
            with open(os.path.join(APP_DIR, "discover_debug.txt"), "w", encoding="utf-8") as f:
                f.write("Αποτέλεσμα ανίχνευσης καμερών\n")
                for path, st in res.get("log", []):
                    f.write(f"  {st}  {path}\n")
                if res.get("error"):
                    f.write("Σφάλμα: " + res["error"] + "\n")
                for path, txt in res.get("raw", {}).items():
                    f.write(f"\n===== {path}\n{txt}\n")
        except OSError:
            pass
        cams = res.get("cameras", [])
        if not cams:
            why = res.get("error") or "το καταγραφικό δεν επέστρεψε λίστα καμερών"
            QMessageBox.warning(
                self, "Εύρεση καμερών",
                f"Δεν βρέθηκαν κάμερες ({why}).\n\nΈλεγξε IP, χρήστη/κωδικό και τη θύρα HTTP. "
                "Η λίστα έμεινε όπως ήταν.\nΛεπτομέρειες στο discover_debug.txt (δίπλα στο πρόγραμμα).")
            self.statusBar().showMessage("Η εύρεση καμερών απέτυχε.")
            return
        self.stop_all()
        apply_cameras(cams)
        self.cfg["cameras"] = [{"id": c["id"], "name": c["name"], "channel": c["channel"],
                                "online": c.get("online")} for c in cams]
        save_config(self.cfg)
        self.populate_camera_list()
        self.ed_search.clear()
        self.page = 0
        self.relayout(reset_page=True)
        n_ip = sum(1 for c in cams if c["kind"] == "ip")
        lines = []
        for c in cams:
            extra = ""
            if c["kind"] == "ip":
                extra = f"  IP {c.get('ip', '')}  " + {True: "online", False: "ΕΚΤΟΣ",
                                                       None: ""}[c.get("online")]
            lines.append(f"{c['id']:4} κανάλι {c['channel']:<3} {c['name']}{extra}")
        box = QMessageBox(self)
        box.setWindowTitle("Εύρεση καμερών")
        box.setText(f"Βρέθηκαν {len(cams)} κάμερες ({len(cams) - n_ip} αναλογικές, {n_ip} IP) "
                    "και μπήκαν στη λίστα. Η λίστα αποθηκεύτηκε για την επόμενη φορά.")
        box.setDetailedText("\n".join(lines))
        box.exec_()
        self.statusBar().showMessage(f"Βρέθηκαν {len(cams)} κάμερες από το καταγραφικό.")

    @staticmethod
    def _norm(t):
        """Πεζά + χωρίς τόνους, ώστε το «ταμεια»/«TAMEIA»/«ταμεία» να ταιριάζουν ευέλικτα."""
        import unicodedata
        t = unicodedata.normalize("NFD", t.lower())
        return "".join(c for c in t if unicodedata.category(c) != "Mn")

    # Τα ονόματα καμερών είναι λατινικά (greeklish). Αν γράψεις ελληνικά, δοκιμάζουμε
    # δύο μετατροπές: αντιστοιχία πλήκτρων (η->H, θ->U) και φωνητική (η->I, θ->TH).
    _KEYS = dict(zip("αβγδεζηθικλμνξοπρσςτυφχψω", "abgdezhuiklmnjoprswtyfxcv"))
    _PHON = {"α": "a", "β": "v", "γ": "g", "δ": "d", "ε": "e", "ζ": "z", "η": "i",
             "θ": "th", "ι": "i", "κ": "k", "λ": "l", "μ": "m", "ν": "n", "ξ": "x",
             "ο": "o", "π": "p", "ρ": "r", "σ": "s", "ς": "s", "τ": "t", "υ": "y",
             "φ": "f", "χ": "ch", "ψ": "ps", "ω": "o"}

    def _variants(self, text):
        t = self._norm(text)
        phon = "".join(self._PHON.get(c, c) for c in t)
        return {t, "".join(self._KEYS.get(c, c) for c in t), phon,
                phon.replace("y", "i")}                # υ -> i (π.χ. KALINTIKA)

    def filter_cameras(self, text=""):
        variants = [v.split() for v in self._variants(text)]
        active = bool(text.strip())
        shown = 0
        for i in range(self.cam_list.count()):
            it = self.cam_list.item(i)
            hay = self._norm(it.text())
            # ταιριάζει αν κάποια εκδοχή έχει ΟΛΕΣ τις λέξεις μέσα στο όνομα
            ok = any(all(w in hay for w in words) for words in variants)
            it.setHidden(not ok)
            shown += ok
        self.lbl_found.setText(
            f"{shown} από {self.cam_list.count()} κάμερες" if active else "")

    def open_first_match(self):
        for i in range(self.cam_list.count()):
            it = self.cam_list.item(i)
            if not it.isHidden():
                self.cam_list.setCurrentItem(it)
                self.on_list_double(it)
                return

    def apply_mode_fields(self):
        """Δείχνει μόνο τα πεδία που αφορούν την επιλεγμένη ροή:
        RTSP (sub/main): θύρα RTSP.  Στιγμιότυπα HTTP: θύρα HTTP, fps, συνδ., ανάλυση."""
        http = self.cb_stream.currentIndex() == 2
        http_only = (self.sp_http, self.sp_fps, self.sp_conns, self.cb_res)
        for w in http_only:
            self._field_boxes[w].setVisible(http)
        self._field_boxes[self.sp_port].setVisible(not http)
        self.top_bar.updateGeometry()
        self.statusBar().showMessage(
            "Λειτουργία Στιγμιότυπα HTTP: εικόνα μέσω θύρας web (δουλεύει και με κρυπτογράφηση)."
            if http else
            "Λειτουργία RTSP: κανονικό βίντεο· αν η εικόνα είναι πράσινη/χαλασμένη, η ροή "
            "είναι κρυπτογραφημένη - χρησιμοποίησε «Στιγμιότυπα HTTP».")

    def layout_n(self):
        """Πλήθος κελιών από το combo ('1 κάμερα' -> 1)."""
        return int(self.cb_layout.currentText().split()[0])

    def update_page_label(self):
        if len(self.tiles) == 1 and not self.single_mode:
            self.lbl_page.setText(f"Κάμερα {self.page + 1}/{len(DEFAULT_CAMERAS)}")
        else:
            self.lbl_page.setText(f"Σελ. {self.page + 1}/{self.pages()}")

    def change_page(self, d):
        if self.single_mode:
            return
        self.page = (self.page + d) % self.pages()
        self.update_page_label()
        if self.running:
            self.play_page()

    def play_page(self):
        cfg = self.read_cfg()
        n = len(self.tiles)
        cams = DEFAULT_CAMERAS[self.page * n:(self.page + 1) * n]
        for i, t in enumerate(self.tiles):
            if i < len(cams):
                # πολλές συνδέσεις ανά κάμερα μόνο σε μικρό πλέγμα (αλλιώς φορτώνει το DVR)
                conns = cfg.get("conns", 3) if n <= 4 else 1
                # «1 κάμερα»: όσο πιο γρήγορα αντέχει το DVR (όπως η μονή προβολή)
                fps = max(25, cfg.get("fps", 2)) if n == 1 and cfg["stream"] == "snapshot" else None
                t.play(cams[i], self.make_worker(cfg, cams[i][0], cfg["stream"], fps=fps, conns=conns))
            else:
                t.stop()

    # ---------- ενέργειες
    def start_all(self):
        if not self.ed_ip.text().strip() or self.ed_ip.text().strip().endswith("."):
            QMessageBox.warning(self, "IP", "Συμπλήρωσε τη διεύθυνση IP του καταγραφικού.")
            return
        self.running = True
        self.single_mode = None
        if len(self.tiles) != self.layout_n():
            self.build_tiles(self.layout_n())
        self.play_page()
        self.update_page_label()

    def stop_all(self):
        self.running = False
        for t in self.tiles:
            t.stop()

    def restart_if_running(self):
        if self.running:
            if self.single_mode:
                self.play_single(self.single_mode[0])
            else:
                self.play_page()

    def on_tile_click(self, tile):
        if self.selected_tile:
            self.selected_tile.set_selected(False)
        self.selected_tile = None if self.selected_tile is tile else tile
        if self.selected_tile:
            tile.set_selected(True)
            if tile.cam:
                self.statusBar().showMessage(f"{tile.caption()}  ->  "
                                             f"{self.safe_url(tile.cam[0])}")

    def on_tile_double(self, tile):
        if self.single_mode:                       # επιστροφή στο πλέγμα
            _, layout, page = self.single_mode
            self.single_mode = None
            self.page = page
            self.build_tiles(layout)
            self.update_page_label()
            if self.running:
                self.play_page()
        elif tile.cam:
            self.play_single(tile.cam)

    def play_single(self, cam):
        prev_layout = self.single_mode[1] if self.single_mode else len(self.tiles)
        prev_page = self.single_mode[2] if self.single_mode else self.page
        self.build_tiles(1)
        self.single_mode = (cam, prev_layout, prev_page)
        cfg = self.read_cfg()
        # Στη μονή προβολή: στιγμιότυπα αν είναι σε αυτή τη λειτουργία,
        # αλλιώς κύρια ροή (καλύτερη ποιότητα)
        single = "snapshot" if cfg["stream"] == "snapshot" else "main"
        # στη μονή προβολή όσο πιο γρήγορα αντέχει το καταγραφικό (στόχος 25 fps)
        fps = max(25, cfg.get("fps", 2)) if single == "snapshot" else None
        w = self.make_worker(cfg, cam[0], single, fps=fps, conns=cfg.get("conns", 3))
        self.tiles[0].play(cam, w)
        self.running = True
        self.lbl_page.setText("Μονή")

    def on_list_double(self, item):
        cam = item.data(Qt.UserRole)
        cfg = self.read_cfg()
        if not cfg["ip"] or cfg["ip"].endswith("."):
            QMessageBox.warning(self, "IP", "Συμπλήρωσε τη διεύθυνση IP του καταγραφικού.")
            return
        if len(self.tiles) == 1 and not self.single_mode:      # λειτουργία «1 κάμερα»
            self.page = next(i for i, c in enumerate(DEFAULT_CAMERAS) if c[0] == cam[0])
            self.running = True
            self.play_page()
            self.update_page_label()
        elif self.selected_tile and not self.single_mode:
            self.running = True
            self.selected_tile.play(cam, self.make_worker(cfg, cam[0], cfg["stream"], conns=cfg.get("conns", 3)))
        else:
            self.play_single(cam)

    def snapshot(self):
        tile = self.selected_tile or (self.tiles[0] if self.single_mode else None)
        if not tile or tile.last_image is None:
            QMessageBox.information(self, "Στιγμιότυπο",
                                    "Κάνε κλικ σε ένα κελί που παίζει (ή μονή προβολή).")
            return
        os.makedirs(SNAP_DIR, exist_ok=True)
        fn = os.path.join(SNAP_DIR, f"{tile.cam[0]}_{datetime.now():%Y%m%d_%H%M%S}.jpg")
        tile.last_image.save(fn, "JPG", 95)
        self.statusBar().showMessage("Αποθηκεύτηκε: " + fn, 6000)

    def safe_url(self, cam_id):
        cfg = dict(self.read_cfg()); cfg["password"] = "****"
        stream = cfg["stream"]
        if self.single_mode and stream != "snapshot":
            stream = "main"
        if stream == "snapshot":
            return snapshot_url(cfg, cam_id)
        return build_url(cfg, cam_id, stream)

    def show_url(self):
        lines = [f"{cid:4} {name:22} {self.safe_url(cid)}" for cid, name in DEFAULT_CAMERAS]
        box = QMessageBox(self)
        box.setWindowTitle("RTSP URLs")
        box.setText("Για VLC κλπ. (βάλε τον κωδικό στη θέση των ****):")
        box.setDetailedText("\n".join(lines))
        box.exec_()

    def closeEvent(self, e):
        self.read_cfg()
        self.stop_all()
        deadline = time.time() + 1.0
        for w in list(_orphans):
            w.wait(max(1, int((deadline - time.time()) * 1000)))
        e.accept()


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    w = MainWindow()
    w.show()
    code = app.exec_()
    # τυχόν threads που περιμένουν ακόμη timeout δικτύου -> άμεση έξοδος
    os._exit(code)


if __name__ == "__main__":
    main()

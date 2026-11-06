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
import os
import sys
import time
from datetime import datetime
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

from PyQt5.QtCore import Qt, QThread, QTimer, pyqtSignal  # noqa: E402
from PyQt5.QtGui import QImage, QPixmap, QFont, QPainter, QColor  # noqa: E402
from PyQt5.QtWidgets import (  # noqa: E402
    QApplication, QComboBox, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPushButton,
    QSpinBox, QSplitter, QVBoxLayout, QWidget, QSizePolicy,
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
    "stream": "sub",        # sub | main
    "layout": 4,            # 1, 4, 9, 16
    "ip_base": 16,          # D1 = ip_base+1  (δοκίμασε 32 αν δεν παίζουν οι IP)
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


def channel_number(cam_id, ip_base):
    """A5 -> 5,  D4 -> ip_base + 4"""
    n = int(cam_id[1:])
    return n if cam_id[0] == "A" else ip_base + n


def build_url(cfg, cam_id, stream):
    ch = channel_number(cam_id, cfg["ip_base"])
    st = "01" if stream == "main" else "02"
    user = quote(cfg["user"], safe="")
    pwd = quote(cfg["password"], safe="")
    return f"rtsp://{user}:{pwd}@{cfg['ip']}:{cfg['port']}/Streaming/Channels/{ch}{st}"


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
    def play(self, cam, url):
        self.stop()
        self.cam = cam
        self.show_text(self.caption() + "\nΣύνδεση...")
        self.worker = StreamWorker(url)
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
        if pm.width() > self.width() or pm.height() > self.height():
            pm = pm.scaled(self.size(), Qt.KeepAspectRatio, Qt.FastTransformation)
        p = QPainter(pm)
        p.fillRect(0, 0, pm.width(), 20, QColor(0, 0, 0, 140))
        p.setPen(QColor("white"))
        p.setFont(QFont("Arial", 9, QFont.Bold))
        txt = self.caption()
        w = self.worker
        if w and w.info:
            txt += f"   {w.info}"
        if w and w.loss:
            txt += f"   σφάλματα {w.loss}%"
            p.setPen(QColor("#ff6b6b") if w.loss > 5 else QColor("white"))
        if self._status:
            txt += f"  [{self._status}]"
        p.drawText(6, 15, txt)
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
        self.cb_stream = QComboBox(); self.cb_stream.addItems(["Δευτερεύουσα (sub)", "Κύρια (main)"])
        self.cb_stream.setCurrentIndex(1 if self.cfg["stream"] == "main" else 0)
        self.cb_layout = QComboBox(); self.cb_layout.addItems(["1", "4", "9", "16"])
        self.cb_layout.setCurrentText(str(self.cfg["layout"]))
        self.sp_ipbase = QSpinBox(); self.sp_ipbase.setRange(0, 64); self.sp_ipbase.setValue(self.cfg["ip_base"])
        self.sp_ipbase.setToolTip("Αρίθμηση RTSP IP καναλιών: D1 = τιμή+1.\n"
                                  "Αν οι D4/D5 δεν παίζουν με 16, δοκίμασε 32.")

        btn_connect = QPushButton("▶ Προβολή"); btn_connect.clicked.connect(self.start_all)
        btn_stop = QPushButton("■ Στοπ"); btn_stop.clicked.connect(self.stop_all)
        btn_prev = QPushButton("◀"); btn_prev.clicked.connect(lambda: self.change_page(-1))
        btn_next = QPushButton("▶"); btn_next.clicked.connect(lambda: self.change_page(1))
        btn_snap = QPushButton("📷 Στιγμιότυπο"); btn_snap.clicked.connect(self.snapshot)
        btn_url = QPushButton("URL"); btn_url.clicked.connect(self.show_url)
        self.lbl_page = QLabel()
        btn_connect.setStyleSheet("background:#d32f2f;color:white;font-weight:bold;padding:4px 12px;")

        top = QHBoxLayout()
        for lbl, w in [("IP:", self.ed_ip), ("Θύρα:", self.sp_port), ("Χρήστης:", self.ed_user),
                       ("Κωδικός:", self.ed_pass), ("Ροή:", self.cb_stream),
                       ("Πλέγμα:", self.cb_layout), ("IP βάση:", self.sp_ipbase)]:
            top.addWidget(QLabel(lbl)); top.addWidget(w)
        top.addWidget(btn_connect); top.addWidget(btn_stop)
        top.addStretch()
        for w in (btn_prev, self.lbl_page, btn_next, btn_snap, btn_url):
            top.addWidget(w)

        # ---------- λίστα καμερών
        self.cam_list = QListWidget()
        self.cam_list.setMaximumWidth(250)
        for cid, name in DEFAULT_CAMERAS:
            it = QListWidgetItem(f"{cid}  {name}")
            it.setData(Qt.UserRole, (cid, name))
            self.cam_list.addItem(it)
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
        lv.addWidget(QLabel("<b>Κάμερες</b>")); lv.addWidget(self.cam_list)
        splitter.addWidget(left); splitter.addWidget(self.grid_host)
        splitter.setStretchFactor(1, 1)

        central = QWidget(); v = QVBoxLayout(central)
        v.addLayout(top); v.addWidget(splitter, 1)
        self.setCentralWidget(central)
        self.statusBar().showMessage("Συμπλήρωσε IP/κωδικό του καταγραφικού και πάτα Προβολή. "
                                     "Διπλό κλικ σε κελί = μεγέθυνση / επιστροφή.")

        self.cb_layout.currentTextChanged.connect(lambda _: self.relayout(reset_page=True))
        self.cb_stream.currentIndexChanged.connect(lambda _: self.restart_if_running())
        self.running = False
        self.build_tiles(int(self.cb_layout.currentText()))

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
            "stream": "main" if self.cb_stream.currentIndex() == 1 else "sub",
            "layout": int(self.cb_layout.currentText()),
            "ip_base": self.sp_ipbase.value(),
        })
        save_config(self.cfg)
        return self.cfg

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
        self.build_tiles(int(self.cb_layout.currentText()))
        if self.running:
            self.play_page()
        self.update_page_label()

    def update_page_label(self):
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
                t.play(cams[i], build_url(cfg, cams[i][0], cfg["stream"]))
            else:
                t.stop()

    # ---------- ενέργειες
    def start_all(self):
        if not self.ed_ip.text().strip() or self.ed_ip.text().strip().endswith("."):
            QMessageBox.warning(self, "IP", "Συμπλήρωσε τη διεύθυνση IP του καταγραφικού.")
            return
        self.running = True
        self.single_mode = None
        if len(self.tiles) != int(self.cb_layout.currentText()):
            self.build_tiles(int(self.cb_layout.currentText()))
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
        # Στη μονή προβολή χρησιμοποιούμε πάντα κύρια ροή (καλύτερη ποιότητα)
        self.tiles[0].play(cam, build_url(cfg, cam[0], "main"))
        self.running = True
        self.lbl_page.setText("Μονή")

    def on_list_double(self, item):
        cam = item.data(Qt.UserRole)
        cfg = self.read_cfg()
        if not cfg["ip"] or cfg["ip"].endswith("."):
            QMessageBox.warning(self, "IP", "Συμπλήρωσε τη διεύθυνση IP του καταγραφικού.")
            return
        if self.selected_tile and not self.single_mode:
            self.running = True
            self.selected_tile.play(cam, build_url(cfg, cam[0], cfg["stream"]))
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
        stream = "main" if self.single_mode else cfg["stream"]
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

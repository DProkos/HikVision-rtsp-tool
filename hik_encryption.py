#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Έλεγχος / απενεργοποίηση της κρυπτογράφησης ροής Hik-Connect σε καταγραφικό
Hikvision μέσω ISAPI (το ίδιο API που χρησιμοποιεί η σελίδα web).

Χρήση:   python hik_encryption.py
         (διαβάζει IP/χρήστη/κωδικό από hik_viewer_config.json αν υπάρχει)

Τι κάνει:
  1. Διαβάζει τις ρυθμίσεις Hik-Connect (/ISAPI/System/Network/EZVIZ) και τις εμφανίζει.
  2. Αν βρει πεδίο κρυπτογράφησης ενεργό (π.χ. <streamEncrypteEnabled>true),
     ρωτάει και το γυρίζει σε false - αλλάζει ΜΟΝΟ αυτό, τα υπόλοιπα μένουν ίδια.
  3. Ξαναδιαβάζει για επιβεβαίωση.
Δεν αλλάζει τίποτα χωρίς να απαντήσεις «ν».
"""
import getpass
import json
import os
import re
import sys
import urllib.error
import urllib.request

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(APP_DIR, "hik_viewer_config.json")
BACKUP_FILE = os.path.join(APP_DIR, "isapi_backup.xml")

PATHS = [
    "/ISAPI/System/Network/EZVIZ",
    "/ISAPI/System/Network/EZVIZ/capabilities",
]
# Πιθανά σημεία της ρύθμισης (διαφέρουν ανά firmware) - μόνο ανάγνωση (GET)
CANDIDATES = [
    "/ISAPI/System/Network/EZVIZ",
    "/ISAPI/System/Network/EZVIZ/capabilities",
    "/ISAPI/System/Network/EZVIZ/encrypt",
    "/ISAPI/System/Network/EZVIZ/streamEncrypt",
    "/ISAPI/System/Network/EZVIZ/secretKey",
    "/ISAPI/Streaming/encryption",
    "/ISAPI/Streaming/encryption/capabilities",
    "/ISAPI/Streaming/encrypt",
    "/ISAPI/Streaming/streamingEncrypt",
    "/ISAPI/Streaming/streamEncryption",
    "/ISAPI/Streaming/channels/102/encryption",
    "/ISAPI/Streaming/channels/101",
    "/ISAPI/Streaming/channels/102",
    "/ISAPI/Security/streamEncryption",
    "/ISAPI/Security/streamingEncrypt",
    "/ISAPI/Security/encryption",
    "/ISAPI/Security/capabilities",
    "/ISAPI/System/Network/platformAccess",
]
ENC_RE = re.compile(r"<(\w*[Ee]ncrypt\w*)>\s*(true|false)\s*</\1>")


def ask(prompt, default=""):
    v = input(f"{prompt} [{default}]: ").strip()
    return v or default


def make_opener(base, user, pwd):
    mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    mgr.add_password(None, base, user, pwd)
    return urllib.request.build_opener(
        urllib.request.HTTPDigestAuthHandler(mgr),
        urllib.request.HTTPBasicAuthHandler(mgr),
    )


def request(opener, url, method="GET", body=None):
    data = body.encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/xml; charset=UTF-8")
    # Το urllib μετράει τις προσπάθειες digest σε ΟΛΑ τα αιτήματα και μετά από
    # 5 αποτυχημένα (π.χ. 404) σταματά να στέλνει κωδικό -> ψεύτικα 401.
    for h in opener.handlers:
        if hasattr(h, "reset_retry_count"):
            h.reset_retry_count()
    try:
        with opener.open(req, timeout=10) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def find_flags(xml):
    return [(m.group(1), m.group(2)) for m in ENC_RE.finditer(xml)]


def disable_flags(xml):
    return ENC_RE.sub(lambda m: f"<{m.group(1)}>false</{m.group(1)}>", xml)


def main():
    cfg = {}
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        pass
    ip = ask("IP καταγραφικού", cfg.get("ip", ""))
    port = ask("Θύρα HTTP (web)", "80")
    user = ask("Χρήστης", cfg.get("user", "admin"))
    pwd = cfg.get("password") or getpass.getpass("Κωδικός admin: ")
    base = f"http://{ip}:{port}"
    op = make_opener(base, user, pwd)

    print("\n=== Αναζήτηση ρύθμισης κρυπτογράφησης ===")
    st, _ = request(op, base + PATHS[0])
    if st == 401:
        sys.exit("Λάθος χρήστης/κωδικός (401).")

    # Πού αναφέρεται η δυνατότητα στα capabilities (για διάγνωση)
    _, caps = request(op, base + "/ISAPI/System/capabilities")
    cl = caps.splitlines()
    for n, line in enumerate(cl):
        if re.search(r"[Ee]ncrypt", line):
            print("  capabilities:", " ".join(x.strip() for x in cl[max(0, n - 2):n + 3]))

    targets = []          # (path, xml, flags με true/false)
    backups = []
    for path in CANDIDATES:
        st, xml = request(op, base + path)
        flags = find_flags(xml) if st == 200 else []
        mark = "  <-- " + ", ".join(f"{t}={v}" for t, v in flags) if flags else ""
        print(f"  {st}  {path}{mark}")
        if st == 200:
            backups.append(f"===== {path}\n{xml}")
        if flags and not path.endswith("capabilities"):
            targets.append((path, xml, flags))
    with open(BACKUP_FILE, "w", encoding="utf-8") as f:
        f.write("\n\n".join(backups))
    print(f"(αντίγραφο όλων των απαντήσεων: {BACKUP_FILE})")

    print("\n=== Αποτέλεσμα ===")
    active = [(p, x, fl) for p, x, fl in targets if any(v == "true" for _, v in fl)]
    if not targets:
        print("Δεν βρέθηκε ρύθμιση κρυπτογράφησης που να αλλάζει μέσω ISAPI σε αυτό το firmware.")
        print("Δες τις εναλλακτικές λύσεις (iVMS-4200 / μενού καταγραφικού).")
        return
    if not active:
        print("Βρέθηκαν ρυθμίσεις αλλά είναι ήδη false:", [p for p, _, _ in targets])
        return
    for path, xml, flags in active:
        print(f"\n{path}:")
        for tag, val in flags:
            print(f"  {tag} = {val}")
        if ask("Να γίνει false; (ν/ο)", "ο").lower() not in ("ν", "n", "y", "yes", "ναι"):
            continue
        st, resp = request(op, base + path, "PUT", disable_flags(xml))
        short = re.sub(r"\s+", " ", resp)[:300]
        print(f"Απάντηση: {st} {short}")
        _, xml2 = request(op, base + path)
        print("Τώρα:", ", ".join(f"{t}={v}" for t, v in find_flags(xml2)))
    print("\nΠάτα Στοπ -> Προβολή στο viewer για να δεις αν καθάρισε η εικόνα.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print("Σφάλμα:", e)
    input("\nEnter για έξοδο...")

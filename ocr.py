"""ocr.py - OCR-Lauf fuer fehlende Dateien (Paket O1).

Wird von app.py als eigener Prozess gestartet (wie der Indexlauf). Liest von
stdin zuerst eine Zeile JSON mit dem Auftrag, danach ggf. die Zeile "CANCEL".
Schreibt auf einen eigenen Kanal (Kopie des alten stdout) Zeilen
    PROGRESS {json}   Stand fuer die Statusseite
    ZEILE <text>      eine Logzeile fuer die Live-Anzeige
    ENDE {json}       Abschluss
Alles andere (Meldungen von PyMuPDF usw.) geht nach stderr.

Grundsaetze (Entscheidungen vom 17.11.2026):
- Die Originale in der Sammlung werden nie angefasst. Die OCR-Fassung liegt im
  Schattenordner (config "ocr_ordner", Vorgabe ~/see_also_ocr) unter demselben
  relativen Pfad.
- OCR und Einbetten sind getrennt. Dieser Lauf macht nur OCR; eingebettet wird
  beim naechsten "Jetzt nachindizieren" (ab Paket O2).
- Sprachen eng+deu+fra, in dieser Reihenfolge.
- Keine Verwaltungsarbeit fuer den Nutzer: Der Lauf prueft alle Kandidaten
  selbst, nimmt kleine Dateien zuerst und fuehrt jede Datei, aus der OCR keinen
  Text gewinnt, mit Grund in der Liste "OCR ohne Ergebnis" (Tabelle
  ocr_dateien in see_also.db, dazu logs/ocr_nicht_erfasst.txt). Eine solche
  Datei wird erst wieder versucht, wenn sich das Original aendert.
- Abbrechen jederzeit; der naechste Lauf macht dort weiter.

Zustaende in ocr_dateien.status:
    offen          geprueft, wartet auf OCR
    in_arbeit      wird gerade bearbeitet (bleibt nur bei Absturz stehen)
    erfasst        OCR-Fassung liegt im Schattenordner und liefert Text
    kein_text      OCR lief, ergab aber keinen verwertbaren Text
    leer           0-Byte-Datei
    verschluesselt Passwortschutz
    kein_pdf       Inhalt ist kein PDF (Endung taeuscht; z. B. AppleDouble, Textdatei)
    abgeschnitten  PDF ohne Dateiende, keine Seiten (Kopie abgebrochen) - Inhalt verloren
    ausgenullt     PDF ohne Dateiende, ueberwiegend Nullbytes - Inhalt verloren
    unlesbar       PDF laesst sich aus anderem Grund nicht oeffnen
    fehler         ocrmypdf meldete einen Fehler
    zeitlimit      ocrmypdf brauchte zu lange
    absturz        der Lauf endete zweimal waehrend dieser Datei

Nur fuer Tests: Ist SEE_ALSO_TEST_ATTRAPPE gesetzt, ersetzen Attrappen das
Pruefen mit PyMuPDF und das Zaehlen der Chunks (siehe _attrappe()).
"""
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import speicher  # noqa: E402

TABELLE = """
CREATE TABLE IF NOT EXISTS ocr_dateien (
    path      TEXT PRIMARY KEY,
    groesse   INTEGER,
    mtime     REAL,
    seiten    INTEGER,
    zeichen   INTEGER,
    status    TEXT NOT NULL,
    grund     TEXT,
    chunks    INTEGER,
    sekunden  REAL,
    versuche  INTEGER NOT NULL DEFAULT 0,
    geaendert TEXT
)"""

NICHT_ERFASST = ("kein_text", "leer", "verschluesselt", "kein_pdf", "abgeschnitten", "ausgenullt",
                 "unlesbar", "fehler", "zeitlimit", "absturz")
GRUND_TEXT = {      # G1: englisch (Anzeige, Log, neue Eintraege in ocr_dateien.grund)
    "kein_text": "no text found",
    "leer": "empty file (0 bytes)",
    "verschluesselt": "password protected",
    "kein_pdf": "not a PDF",
    "abgeschnitten": "truncated (no end of file)",
    "ausgenullt": "zeroed (only zero bytes)",
    "unlesbar": "PDF unreadable",
    "fehler": "ocrmypdf error",
    "zeitlimit": "time limit",
    "absturz": "crash",
}
# G1: Anzeige englisch. Fruehere Laeufe haben die deutschen Standardtexte in
# ocr_dateien.grund geschrieben; beide gelten als "Standard" (keine Detailzeile).
GRUND_TEXT_DE = {
    "kein_text": "kein Text erkannt", "leer": "leere Datei (0 Byte)", "verschluesselt": "Passwortschutz",
    "kein_pdf": "kein PDF", "abgeschnitten": "abgeschnitten (kein Dateiende)",
    "ausgenullt": "ausgenullt (nur Nullbytes)", "unlesbar": "PDF nicht lesbar",
    "fehler": "Fehler von ocrmypdf", "zeitlimit": "Zeitlimit", "absturz": "Absturz",
}
STANDARD_GRUENDE = set(GRUND_TEXT_DE.values()) | set(GRUND_TEXT.values())
# G1b: Statuswerte (intern deutsch, in der Datenbank) fuer Log und Listen.
STATUS_EN = {
    "offen": "pending", "in_arbeit": "in progress", "erfasst": "recognized", "kein_text": "no text",
    "leer": "empty", "verschluesselt": "encrypted", "kein_pdf": "not a PDF", "abgeschnitten": "truncated",
    "ausgenullt": "zeroed", "unlesbar": "unreadable", "fehler": "error", "zeitlimit": "time limit",
    "absturz": "crash",
}
LISTE_NICHT_ERFASST = "ocr_not_recognized.txt"   # bis G1a: ocr_nicht_erfasst.txt

# G1b: Gruende aus frueheren Laeufen stehen deutsch in ocr_dateien.grund.
# Fuer Anzeige und Listen uebersetzen (die Datenbank bleibt, wie sie ist).
_GRUND_DE_EN = [
    (re.compile(r"^kein PDF: Mac-Begleitdatei \(AppleDouble\)"), "not a PDF: macOS metadata file (AppleDouble)"),
    (re.compile(r"^kein PDF: Textdatei"), "not a PDF: text file"),
    (re.compile(r"^kein PDF: unbekanntes Format"), "not a PDF: unknown format"),
    (re.compile(r"^kein PDF: HTML-Seite"), "not a PDF: HTML page"),
    (re.compile(r"^keine Seiten, Dateiende vorhanden"), "no pages, end of file present"),
    (re.compile(r"^keine Seiten$"), "no pages"),
    (re.compile(r"^Lauf endete zweimal waehrend dieser Datei"), "run ended twice during this file"),
    (re.compile(r"^nach (\d+) s \((\S+) Seiten\)"), r"after \1 s (\2 pages)"),
    (re.compile(r"^OCR-Fassung nicht lesbar: "), "OCR version unreadable: "),
    (re.compile(r"2\. Versuch: "), "2nd attempt: "),
]


def anzeige_pfad(pfad: str) -> str:
    """Pfad fuer Anzeige und Logs: das Benutzerverzeichnis als ~, damit
    Bildschirmfotos und Logs den Benutzernamen nicht zeigen (G3c)."""
    heim = os.path.expanduser("~").rstrip("/")
    p = str(pfad)
    if heim and (p == heim or p.startswith(heim + "/")):
        return "~" + p[len(heim):]
    return p


def grund_en(grund: str | None) -> str | None:
    """Gespeicherten Grund englisch anzeigen (alte deutsche Eintraege)."""
    if not grund:
        return grund
    for muster, ersatz in _GRUND_DE_EN:
        grund = muster.sub(ersatz, grund)
    return grund
TMP_ENDUNG = ".ocr-tmp.pdf"


def tabelle_anlegen(con) -> None:
    con.execute(TABELLE)
    con.commit()


def schattenpfad(ordner: str, rel: str) -> str:
    # Pfade byteweise uebernehmen, nie normalisieren (README, Invarianten).
    return os.path.join(ordner, rel)


def zustand_je_pfad(con) -> dict:
    """{path: (status, grund)} fuer die Statusseite. Leer, wenn es die Tabelle
    noch nicht gibt."""
    try:
        return {p: (s, g) for p, s, g in con.execute("SELECT path, status, grund FROM ocr_dateien")}
    except Exception:
        return {}


# --- Pruefen und Zaehlen (echt mit PyMuPDF, in Tests Attrappe) -------------

def _befund_ohne_seiten(path: str) -> dict:
    """Fuer ein PDF ohne Seiten: abgeschnitten, ausgenullt oder sonst unlesbar."""
    with open(path, "rb") as f:
        daten = f.read()
    if b"%%EOF" in daten[-16384:]:
        return dict(status="unlesbar", grund="no pages, end of file present")
    if daten.count(0) > 0.5 * len(daten):
        return dict(status="ausgenullt", grund=GRUND_TEXT["ausgenullt"])
    return dict(status="abgeschnitten", grund=GRUND_TEXT["abgeschnitten"])


def _kopf_pruefen(path: str) -> dict | None:
    """Ist das ueberhaupt ein PDF? PDF-Dateien beginnen mit %PDF (Toleranz:
    die ersten 1024 Byte, wie es auch PDF-Leser handhaben)."""
    with open(path, "rb") as f:
        kopf = f.read(1024)
    if b"%PDF" in kopf:
        return None
    if kopf[:4] == b"\x00\x05\x16\x07":
        return dict(status="kein_pdf", grund="not a PDF: macOS metadata file (AppleDouble)")
    if kopf and kopf.count(0) == len(kopf):
        return dict(status="ausgenullt", grund=GRUND_TEXT["ausgenullt"])
    try:
        kopf.decode("utf-8")
        art = "text file"
    except UnicodeDecodeError:
        art = "unknown format"
    if kopf.lstrip()[:15].lower().startswith((b"<!doctype html", b"<html")):
        art = "HTML page"
    return dict(status="kein_pdf", grund=f"not a PDF: {art}")


def _pruefen_echt(path: str) -> dict:
    from indexer import fitz   # PyMuPDF, ohne Warnung zum alten Namen
    kopf = _kopf_pruefen(path)
    if kopf:
        return kopf
    try:
        doc = fitz.open(path)
    except Exception as e:  # noqa: BLE001
        return dict(status="unlesbar", grund=f"{type(e).__name__}: {e}"[:300])
    try:
        if doc.needs_pass:
            return dict(status="verschluesselt", grund=GRUND_TEXT["verschluesselt"])
        n = doc.page_count
        if n == 0:
            return _befund_ohne_seiten(path)
        zeichen = 0
        for page in doc:
            zeichen += len(page.get_text("text").strip())
        return dict(status="offen", seiten=n, zeichen=zeichen)
    except Exception as e:  # noqa: BLE001
        return dict(status="unlesbar", grund=f"{type(e).__name__}: {e}"[:300])
    finally:
        doc.close()


def _chunks_echt(path: str, overlap: int) -> int:
    import indexer
    return len(indexer.extract_page_chunks(path, overlap))


pruefen = _pruefen_echt
chunks_zaehlen = _chunks_echt


def _attrappe():
    """Testfaelle ueber den Dateinamen: verschl*, kaputt*, absturz* (beim Pruefen),
    bild* (OCR ergibt keinen Text). Seitenzahl = Dateigroesse in Byte."""
    global pruefen, chunks_zaehlen

    def p(path):
        name = os.path.basename(path)
        if name.startswith("absturz"):
            os.kill(os.getpid(), 9)  # SIGKILL: kein Absturzdialog von macOS
        if name.startswith("verschl"):
            return dict(status="verschluesselt", grund=GRUND_TEXT["verschluesselt"])
        if name.startswith("kaputt"):
            return dict(status="unlesbar", grund="ValueError: kaputt (Attrappe)")
        if name.startswith("befund"):          # echte Kopf-/Endepruefung
            return _kopf_pruefen(path) or _befund_ohne_seiten(path)
        return dict(status="offen", seiten=max(1, os.path.getsize(path)), zeichen=0)

    def c(path, overlap):
        with open(path, "rb") as f:
            return 0 if b"KEINTEXT" in f.read() else 3

    pruefen, chunks_zaehlen = p, c


# --- Der Lauf --------------------------------------------------------------

class Lauf:
    def __init__(self, auftrag: dict, kanal, abbruch: threading.Event):
        self.a = auftrag
        self.kanal = kanal
        self.abbruch = abbruch
        self.base = auftrag["base"]
        self.ordner = auftrag["ordner"]
        self.sprachen = auftrag.get("sprachen") or "eng+deu+fra"
        self.stand = dict(phase="start", total=0, current=0, current_file=None,
                          erfasst=0, nicht_erfasst=0, seiten_fertig=0, seiten_gesamt=0,
                          eta_s=None, s_je_seite=None, error=None, log_file=None, abgebrochen=False)
        self.log = None
        self.prozesse = set()
        self._prozesse_lock = threading.Lock()
        self.rotate = False
        self.ghostscript = False
        self.parallel = max(1, int(auftrag.get("parallel") or 1))
        kerne = os.cpu_count() or 1
        self.jobs = int(auftrag["jobs"]) if auftrag.get("jobs") else max(1, kerne // self.parallel)

    # Ausgabe
    def _send(self, art, nutzlast):
        try:
            self.kanal.write(f"{art} {nutzlast}\n")
            self.kanal.flush()
        except Exception:
            pass

    def melden(self):
        self._send("PROGRESS", json.dumps(self.stand, ensure_ascii=False))

    def zeile(self, text, live=True):
        z = f"[{datetime.now().strftime('%H:%M:%S')}] {text}"
        if self.log:
            self.log.write(z + "\n")
            self.log.flush()
        if live:
            self._send("ZEILE", z.replace("\n", " "))

    # Werkzeuge
    def _werkzeuge_pruefen(self) -> str | None:
        exe = shutil.which("ocrmypdf")
        if not exe:
            return "ocrmypdf is not installed (brew install ocrmypdf tesseract-lang)."
        try:
            r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=60)
            v = (r.stdout.strip() or r.stderr.strip()).splitlines()[-1:] or ["?"]
            v = v[0].strip()
        except Exception as e:  # noqa: BLE001
            return f"ocrmypdf does not start: {e}"
        self.zeile(f"ocrmypdf {v} ({exe})", live=False)
        tess = shutil.which("tesseract")
        if not tess:
            return "tesseract is not installed (brew install tesseract tesseract-lang)."
        try:
            out = subprocess.run([tess, "--list-langs"], capture_output=True, text=True, timeout=60)
            langs = set((out.stdout + out.stderr).split())
        except Exception as e:  # noqa: BLE001
            return f"tesseract does not start: {e}"
        fehlt = [s for s in self.sprachen.split("+") if s not in langs]
        if fehlt:
            return (f"Tesseract language pack missing: {', '.join(fehlt)} "
                    "(brew install tesseract-lang).")
        self.rotate = "osd" in langs
        self.ghostscript = shutil.which("gs") is not None
        self.zeile(f"Languages {self.sprachen}; rotate pages: "
                   + ("yes" if self.rotate else "no (language pack osd missing)")
                   + "; Ghostscript for 2nd attempt: " + ("yes" if self.ghostscript else "no"), live=False)
        return None

    # Aufraeumen
    def _aufraeumen(self, con):
        n_weg = n_tmp = n_wieder = n_absturz = 0
        # O1b: Dateien, die O1 nur als "keine Seiten" kannte, neu pruefen; sie
        # bekommen jetzt einen genaueren Grund (abgeschnitten, ausgenullt, kein PDF).
        con.execute("UPDATE ocr_dateien SET mtime=-1 WHERE status='unlesbar' AND grund='keine Seiten'")
        # O1e: Fehler, fuer die es jetzt einen zweiten Versuch gibt, einmal neu
        # anstossen (Zeilen mit "2. Versuch" hatten ihn schon).
        n_neu = con.execute(
            "UPDATE ocr_dateien SET status='offen', grund=NULL, geaendert=? WHERE status='fehler' "
            "AND grund NOT LIKE '%2. Versuch%' AND grund NOT LIKE '%2nd attempt%' AND (grund LIKE 'Exit 6%' OR grund LIKE '%office document%' "
            "OR grund LIKE '%PdfiumError%' OR grund LIKE '%Failed to fill bitmap%' OR grund LIKE '%Tagged%')",
            (_jetzt(),)).rowcount
        if n_neu:
            self.zeile(f"{n_neu} failed files with a known remedy reopened (2nd attempt)")
        # Bestand: alle PDFs, die der Server derzeit sieht (ohne ausgeschlossene
        # Bereiche und Programmpakete). Fehlt die Liste (alte Aufrufer), zaehlt
        # nur, ob das Original noch existiert.
        bestand = set(self.a["alle_pfade"]) if self.a.get("alle_pfade") is not None else None
        for path, status, versuche, grund in con.execute(
                "SELECT path, status, versuche, grund FROM ocr_dateien").fetchall():
            weg = (path not in bestand) if bestand is not None else \
                not os.path.exists(os.path.join(self.base, path))
            if weg:
                ziel = schattenpfad(self.ordner, path)
                if os.path.exists(ziel):
                    os.remove(ziel)
                con.execute("DELETE FROM ocr_dateien WHERE path=?", (path,))
                n_weg += 1
            elif status == "in_arbeit":
                # Der vorige Lauf endete waehrend dieser Datei (Absturz, Strom,
                # Server hart beendet). Einmal neu versuchen, beim zweiten Mal
                # auf die Liste.
                if versuche >= 2:
                    st = os.stat(os.path.join(self.base, path))
                    con.execute("UPDATE ocr_dateien SET status='absturz', grund=?, groesse=?, mtime=?, geaendert=? "
                                "WHERE path=?",
                                ("run ended twice during this file", st.st_size, st.st_mtime,
                                 _jetzt(), path))
                    n_absturz += 1
                elif grund == "pruefen":
                    # Absturz schon beim Pruefen: mtime ungueltig machen, damit
                    # die Datei neu geprueft wird (versuche bleibt erhalten).
                    con.execute("UPDATE ocr_dateien SET status='offen', mtime=-1, geaendert=? WHERE path=?",
                                (_jetzt(), path))
                    n_wieder += 1
                else:
                    con.execute("UPDATE ocr_dateien SET status='offen', geaendert=? WHERE path=?",
                                (_jetzt(), path))
                    n_wieder += 1
        con.commit()
        if os.path.isdir(self.ordner):
            for wurzel, _, dateien in os.walk(self.ordner):
                for d in dateien:
                    if d.endswith(TMP_ENDUNG):
                        try:
                            os.remove(os.path.join(wurzel, d))
                            n_tmp += 1
                        except OSError:
                            pass
        if n_weg or n_tmp or n_wieder or n_absturz:
            self.zeile(f"Cleaned up: {n_weg} entries without original (deleted, excluded, "
                       "application bundle) removed with their OCR version, "
                       f"{n_tmp} partial files deleted, {n_wieder} interrupted files reopened, "
                       f"{n_absturz} marked as crash")

    # Pruefen aller Kandidaten
    def _pruefen(self, con, kandidaten):
        vorhanden = {r[0]: r for r in con.execute(
            "SELECT path, groesse, mtime, status FROM ocr_dateien")}
        self.stand.update(phase="pruefen", total=len(kandidaten), current=0)
        self.melden()
        neu = 0
        t_meld = time.monotonic()
        for i, rel in enumerate(kandidaten, start=1):
            if self.abbruch.is_set():
                return False
            voll = os.path.join(self.base, rel)
            try:
                st = os.stat(voll)
            except OSError:
                continue
            alt = vorhanden.get(rel)
            if alt is not None and alt[1] == st.st_size and alt[2] == st.st_mtime and alt[3] != "in_arbeit":
                continue            # unveraendert: Ergebnis gilt weiter
            if alt is not None and alt[3] == "erfasst":
                ziel = schattenpfad(self.ordner, rel)
                if os.path.exists(ziel):
                    os.remove(ziel)   # Original geaendert: alte OCR-Fassung ist ungueltig
            neu += 1
            if st.st_size == 0:
                erg = dict(status="leer", grund=GRUND_TEXT["leer"])
            else:
                # Vor dem Pruefen vermerken: Stuerzt PyMuPDF ab, findet der
                # naechste Lauf die Datei "in_arbeit" und behandelt sie wie oben.
                con.execute("INSERT OR REPLACE INTO ocr_dateien(path, groesse, mtime, status, grund, versuche, geaendert) "
                            "VALUES (?,?,?,?,?,COALESCE((SELECT versuche FROM ocr_dateien WHERE path=?),0)+1,?)",
                            (rel, st.st_size, st.st_mtime, "in_arbeit", "pruefen", rel, _jetzt()))
                con.commit()
                erg = pruefen(voll)
            con.execute("INSERT OR REPLACE INTO ocr_dateien(path, groesse, mtime, seiten, zeichen, status, grund, versuche, geaendert) "
                        "VALUES (?,?,?,?,?,?,?,0,?)",
                        (rel, st.st_size, st.st_mtime, erg.get("seiten"), erg.get("zeichen"),
                         erg["status"], erg.get("grund"), _jetzt()))
            con.commit()
            if erg["status"] != "offen":
                self.zeile(f"not recognized ({GRUND_TEXT.get(erg['status'], erg['status'])}): {rel}")
            self.stand["current"] = i
            if time.monotonic() - t_meld > 1:
                self.melden()
                t_meld = time.monotonic()
        self.stand["current"] = len(kandidaten)
        self.melden()
        self.zeile(f"Checked: {len(kandidaten)} missing files, {neu} of them new or changed")
        return True

    # OCR einer Datei
    # --- OCR einer Datei (O1e: parallel) -----------------------------------
    # Die Arbeit mit ocrmypdf laeuft in Faeden (mehrere Dateien gleichzeitig);
    # alles mit der Datenbank und mit PyMuPDF (nicht fadensicher) bleibt im
    # Hauptfaden: vorbereiten -> arbeiten (Faden) -> abschliessen.

    def _ocr_vorbereiten(self, con, rel):
        con.execute("UPDATE ocr_dateien SET status='in_arbeit', grund=NULL, versuche=versuche+1, geaendert=? "
                    "WHERE path=?", (_jetzt(), rel))
        con.commit()

    def _ein_versuch(self, voll, tmp, seiten, force, ghostscript, slot):
        cmd = ["ocrmypdf", "--language", self.sprachen, "--output-type", "pdf", "--optimize", "1",
               "--jobs", str(self.jobs)]
        if self.rotate:
            cmd.append("--rotate-pages")
        if force:
            cmd.append("--force-ocr")
        if ghostscript:
            cmd += ["--rasterizer", "ghostscript"]
        cmd += [voll, tmp]
        limit = max(float(self.a.get("zeitlimit_min_s", 300)),
                    float(self.a.get("zeitlimit_je_seite_s", 60)) * (seiten or 1))
        err_pfad = os.path.join(self.a["log_dir"], f"ocrmypdf_stderr_{slot}.log")
        t0 = time.monotonic()
        with open(err_pfad, "w+b") as err:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=err, start_new_session=True)
            with self._prozesse_lock:
                self.prozesse.add(proc)
            art = rc = None
            try:
                while art is None:
                    try:
                        rc = proc.wait(timeout=0.5)
                        art = "rc"
                    except subprocess.TimeoutExpired:
                        if self.abbruch.is_set():
                            _gruppe_beenden(proc)
                            art = "abbruch"
                        elif time.monotonic() - t0 > limit:
                            _gruppe_beenden(proc)
                            art = "zeitlimit"
            finally:
                with self._prozesse_lock:
                    self.prozesse.discard(proc)
            err.seek(0)
            alles = err.read().decode("utf-8", "replace")
        kurz = " | ".join(alles.strip().splitlines()[-3:])[:400]
        return dict(art=art, rc=rc, err=alles, kurz=kurz, limit=limit, dauer=time.monotonic() - t0)

    def _ocr_arbeit(self, rel, seiten, zeichen, slot):
        """Im Faden: ocrmypdf, bei bekannten Fehlern ein zweiter Versuch."""
        voll = os.path.join(self.base, rel)
        ziel = schattenpfad(self.ordner, rel)
        tmp = ziel + TMP_ENDUNG
        os.makedirs(os.path.dirname(ziel), exist_ok=True)
        force = (zeichen or 0) > 0
        ghost = False
        notizen = []
        t0 = time.monotonic()
        for _ in range(3):
            r = self._ein_versuch(voll, tmp, seiten, force, ghost, slot)
            if r["art"] != "rc" or r["rc"] == 0:
                break
            e = r["err"]
            if not force and (r["rc"] == 6 or "office document" in e or "Tagged PDF" in e
                              or "TaggedPDF" in e or "page already has text" in e):
                force = True
                notizen.append("--force-ocr")
                _datei_weg(tmp)
                continue
            if not ghost and self.ghostscript and ("PdfiumError" in e or "Failed to fill bitmap" in e):
                ghost = True
                notizen.append("--rasterizer ghostscript")
                _datei_weg(tmp)
                continue
            break
        r["dauer"] = time.monotonic() - t0
        r.update(tmp=tmp, ziel=ziel, notizen=notizen)
        return r

    def _ocr_abschliessen(self, con, rel, seiten, r):
        """Im Hauptfaden: Ergebnis bewerten. None = abgebrochen (Datei wieder offen)."""
        tmp, ziel = r["tmp"], r["ziel"]
        zweit = f" [2nd attempt: {', '.join(r['notizen'])}]" if r["notizen"] else ""
        if r["art"] == "abbruch":
            _datei_weg(tmp)
            con.execute("UPDATE ocr_dateien SET status='offen', versuche=MAX(versuche-1,0), geaendert=? "
                        "WHERE path=?", (_jetzt(), rel))
            con.commit()
            return None
        if r["art"] == "zeitlimit":
            _datei_weg(tmp)
            return "zeitlimit", f"after {r['limit']:.0f} s ({seiten} pages){zweit}", None
        if r["rc"] != 0 or not os.path.exists(tmp):
            _datei_weg(tmp)
            g = f"Exit {r['rc']}: {r['kurz']}" if r["kurz"] else f"Exit {r['rc']}"
            return "fehler", g + zweit, None
        try:
            n = chunks_zaehlen(tmp, int(self.a.get("overlap", 0)))
        except Exception as e:  # noqa: BLE001
            _datei_weg(tmp)
            return "fehler", f"OCR version unreadable: {type(e).__name__}: {e}"[:300] + zweit, None
        if n == 0:
            _datei_weg(tmp)
            return "kein_text", GRUND_TEXT["kein_text"], 0
        os.replace(tmp, ziel)
        return "erfasst", (zweit.strip(" []") or None), n

    def ausfuehren(self, con, kandidaten, anzahl):
        self.zeile(f"Start OCR run: {len(kandidaten)} missing files, "
                   f"requested: {'all' if not anzahl else anzahl}, shadow folder {anzeige_pfad(self.ordner)}")
        fehler = self._werkzeuge_pruefen()
        if fehler:
            self.zeile("ERROR: " + fehler)
            self.stand.update(phase="error", error=fehler)
            return
        os.makedirs(self.ordner, exist_ok=True)
        self._aufraeumen(con)
        if not self._pruefen(con, kandidaten):
            self._abgebrochen()
            return

        menge = set(kandidaten)
        offen = [(p, s, z) for p, s, z, g in con.execute(
            "SELECT path, seiten, zeichen, groesse FROM ocr_dateien WHERE status='offen' "
            "ORDER BY seiten, groesse, path") if p in menge]
        rest_gesamt = len(offen)
        if anzahl:
            offen = offen[:anzahl]
        seiten_gesamt = sum(s or 1 for _, s, _ in offen)
        self.stand.update(phase="ocr", total=len(offen), current=0, seiten_gesamt=seiten_gesamt,
                          seiten_fertig=0)
        self.zeile(f"OCR for {len(offen)} of {rest_gesamt} pending files, {seiten_gesamt} pages, "
                   f"smallest first, {self.parallel} at a time with {self.jobs} cores each")
        self.melden()

        from concurrent.futures import ThreadPoolExecutor, FIRST_COMPLETED, wait
        t_start = time.monotonic()
        warteschlange = list(offen)
        laufend = {}                 # future -> (rel, seiten, slot)
        freie_slots = list(range(self.parallel))
        fertig = 0
        with ThreadPoolExecutor(max_workers=self.parallel) as pool:
            while warteschlange or laufend:
                while warteschlange and freie_slots and not self.abbruch.is_set():
                    rel, seiten, zeichen = warteschlange.pop(0)
                    slot = freie_slots.pop()
                    self._ocr_vorbereiten(con, rel)
                    f = pool.submit(self._ocr_arbeit, rel, seiten, zeichen, slot)
                    laufend[f] = (rel, seiten, slot)
                    self.stand["current_file"] = rel
                    self.melden()
                if not laufend:
                    break
                erledigt, _ = wait(list(laufend), timeout=1.0, return_when=FIRST_COMPLETED)
                for f in erledigt:
                    rel, seiten, slot = laufend.pop(f)
                    freie_slots.append(slot)
                    try:
                        r = f.result()
                    except Exception as e:  # noqa: BLE001
                        r = dict(art="rc", rc=-1, err=str(e), kurz=f"{type(e).__name__}: {e}"[:300],
                                 limit=0, dauer=0, tmp=schattenpfad(self.ordner, rel) + TMP_ENDUNG,
                                 ziel=schattenpfad(self.ordner, rel), notizen=[])
                    erg = self._ocr_abschliessen(con, rel, seiten, r)
                    if erg is None:
                        continue
                    status, grund, n = erg
                    con.execute("UPDATE ocr_dateien SET status=?, grund=?, chunks=?, sekunden=?, geaendert=?, "
                                "versuche=CASE WHEN ?='erfasst' THEN 0 ELSE versuche END WHERE path=?",
                                (status, grund, n, round(r["dauer"], 1), _jetzt(), status, rel))
                    con.commit()
                    fertig += 1
                    self.stand["current"] = fertig
                    self.stand["seiten_fertig"] += seiten or 1
                    zusatz = f", {grund}" if status == "erfasst" and grund else ""
                    if status == "erfasst":
                        self.stand["erfasst"] += 1
                        self.zeile(f"{fertig}/{len(offen)} recognized: {rel} ({seiten} p., {r['dauer']:.0f} s, "
                                   f"{n} chunks{zusatz})")
                    else:
                        self.stand["nicht_erfasst"] += 1
                        self.zeile(f"{fertig}/{len(offen)} not recognized ({GRUND_TEXT.get(status, status)}): {rel} "
                                   f"({seiten} p., {r['dauer']:.0f} s)"
                                   f"{' - ' + grund if grund and status != 'kein_text' else ''}")
                    # Tempo nach Uhrzeit, nicht nach Summe der Einzeldauern (die
                    # ueberlappen bei paralleler Arbeit).
                    vergangen = time.monotonic() - t_start
                    sps = vergangen / self.stand["seiten_fertig"]
                    self.stand["s_je_seite"] = round(sps, 2)
                    self.stand["eta_s"] = round(sps * (seiten_gesamt - self.stand["seiten_fertig"]))
                    self.melden()

        if self.abbruch.is_set():
            self._abgebrochen()
        else:
            self.stand.update(phase="done", current_file=None)

    def _abgebrochen(self):
        self.stand.update(phase="cancelled", current_file=None, abgebrochen=True)
        self.zeile("Stopped. The next run continues from here.")

    def abschluss(self, con):
        self.zeile(f"This run: {self.stand['erfasst']} recognized, {self.stand['nicht_erfasst']} not recognized"
                   + (f", {self.stand['s_je_seite']} s per page" if self.stand.get("s_je_seite") else ""))
        try:
            zahlen = dict(con.execute("SELECT status, COUNT(*) FROM ocr_dateien GROUP BY status").fetchall())
            self.zeile("Overall: " + ", ".join(f"{STATUS_EN.get(k, k)} {v}" for k, v in sorted(zahlen.items())))
            liste = con.execute(
                f"SELECT status, grund, path FROM ocr_dateien WHERE status IN ({','.join('?' * len(NICHT_ERFASST))}) "
                "ORDER BY status, path", NICHT_ERFASST).fetchall()
            with open(os.path.join(self.a["log_dir"], LISTE_NICHT_ERFASST), "w", encoding="utf-8") as f:
                f.write(f"# OCR without result, as of {_jetzt()}, {len(liste)} files\n")
                f.write("# status\treason\tpath (relative to " + anzeige_pfad(self.base) + ")\n")
                for s, g, p in liste:
                    f.write(f"{STATUS_EN.get(s, s)}\t{(grund_en(g) or GRUND_TEXT.get(s, '')).replace(chr(9), ' ')}\t{p}\n")
        except Exception as e:  # noqa: BLE001
            self.zeile(f"Final list not written: {e}")


def _datei_weg(pfad):
    if os.path.exists(pfad):
        try:
            os.remove(pfad)
        except OSError:
            pass


def _gruppe_beenden(proc):
    """ocrmypdf startet eigene Prozesse (tesseract, gs ...); die ganze Gruppe beenden."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except OSError:
        pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        proc.wait(timeout=10)


def _jetzt():
    return datetime.now().isoformat(timespec="seconds")


def main() -> int:
    import fcntl
    kanal = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    if os.environ.get("SEE_ALSO_TEST_ATTRAPPE"):
        _attrappe()

    auftrag = json.loads(sys.stdin.buffer.readline().decode("utf-8"))
    abbruch = threading.Event()

    def _leser():
        for raw in sys.stdin.buffer:
            if raw.strip() == b"CANCEL":
                abbruch.set()
        abbruch.set()       # Pipe zu: der Server ist weg, kein verwaister Lauf
    threading.Thread(target=_leser, daemon=True).start()

    try:
        os.nice(int(auftrag.get("nice", 10)))
    except OSError:
        pass

    lauf = Lauf(auftrag, kanal, abbruch)
    # Dieselbe Sperrdatei wie der Indexlauf: OCR und Indexlauf nie gleichzeitig.
    lock_fd = os.open(auftrag["lock_datei"], os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lauf.stand.update(phase="error", error="An index run or OCR run is already in progress.")
        lauf._send("ENDE", json.dumps(lauf.stand, ensure_ascii=False))
        return 0

    os.makedirs(auftrag["log_dir"], exist_ok=True)
    log_name = f"ocr_{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
    lauf.log = open(os.path.join(auftrag["log_dir"], log_name), "w", encoding="utf-8")
    lauf.stand["log_file"] = log_name

    koffein = None
    if sys.platform == "darwin" and os.path.exists("/usr/bin/caffeinate"):
        try:
            koffein = subprocess.Popen(["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())],
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL)
            lauf.zeile("Sleep prevented during the run (caffeinate -i)", live=False)
        except Exception:
            pass

    con = None
    try:
        con = speicher.verbinden(auftrag["db"])
        tabelle_anlegen(con)
        lauf.ausfuehren(con, auftrag["kandidaten"], auftrag.get("anzahl"))
        lauf.abschluss(con)
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        lauf.stand.update(phase="error", error=f"{type(e).__name__}: {e}")
        lauf.zeile(f"ERROR: {type(e).__name__}: {e}")
    finally:
        for proc in list(lauf.prozesse):
            _gruppe_beenden(proc)
        if con is not None:
            con.close()
        lauf.zeile(f"End: {_jetzt()}", live=False)
        lauf.log.close()
        if koffein is not None:
            koffein.terminate()
        lauf._send("ENDE", json.dumps(lauf.stand, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    main()
    # Hart beenden: Der Lesefaden haengt in stdin; ein normales Ende bricht
    # unter Python 3.14 sonst mit "could not acquire lock for <stdin>" ab
    # (wie beim Indexlauf). Alles ist geschrieben und geschlossen.
    sys.stderr.flush()
    os._exit(0)

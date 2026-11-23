from werkzeug.routing import PathConverter
from flask import Flask, render_template, redirect, url_for, request, send_from_directory, abort, jsonify
from urllib.parse import quote
from datetime import datetime
from pathlib import Path
import os
import re
import sys
import json
import unicodedata
import threading
import subprocess
import select
import time
import traceback

import numpy as np

# B5a: Der Indexlauf laeuft als eigener Prozess "python app.py --indexlauf <mode>".
IS_INDEX_CHILD = len(sys.argv) >= 3 and sys.argv[1] == "--indexlauf"
if IS_INDEX_CHILD and os.environ.get("SEE_ALSO_TEST_ATTRAPPE"):   # nur fuer Tests
    import types as _types
    sys.modules.setdefault("fitz", _types.ModuleType("fitz"))

import indexer
import embedder
import speicher
import ocr as _ocr   # G3c: anzeige_pfad (Pfade mit ~); sonst lokal importiert

app = Flask(__name__)


class _AnyPathConverter(PathConverter):
    # Wie <path:>, aber auch mit Zeilenumbruechen im Namen (gibt es in
    # Electronics, Ordner "BOOLEAN ALGEBRA<Umbruch>LOGIC SIMPLIFICATION").
    regex = r"[^/][\s\S]*?"


app.url_map.converters["anypath"] = _AnyPathConverter

# Einmalig beim Prozessstart gesetzt (nicht bei jeder Anfrage neu) - dient
# als Referenzpunkt auf der Statusseite, um "Letzte Nachindizierung" richtig
# einzuordnen: liegt dieser Wert VOR dem Prozessstart, stammt er sichtbar
# aus einer frueheren Sitzung, statt dass ein Neustart die persistierte
# Historie einfach loescht.
PROCESS_STARTED_AT = datetime.now().strftime("%Y-%m-%d %H:%M")

APP_DIR = os.path.dirname(os.path.abspath(__file__))
SCORE_CONTRAST_SCRIPT = os.path.join(APP_DIR, "score_contrast.py")
SCORE_CONTRAST_TAU = 3.0    # wie die Vorgaben von score_contrast.py (--tau, --top)
SCORE_CONTRAST_TOP = 10

with open(os.path.join(APP_DIR, "config.json"), encoding="utf-8") as f:
    CONFIG = json.load(f)

# G1b: config-Schluessel englisch. Die frueheren deutschen Namen gelten weiter
# (Alias); steht beides da, gilt der englische.
CONFIG_ALIASE = {
    "speicher": "storage",
    "ausgeschlossen": "excluded_areas",
    "gpu_leeren_alle_stapel": "gpu_cache_clear_every_batches",
    "pdf_isoliert_lesen": "pdf_read_isolated",
    "pdf_zeitlimit_s": "pdf_time_limit_s",
    "indexlauf_eigener_prozess": "index_run_own_process",
    "indexlauf_nice": "index_run_nice",
    "einbettung_pause_ms": "embed_pause_ms",
    "ocr_ordner": "ocr_folder",
    "ocr_sprachen": "ocr_languages",
    "ocr_zeitlimit_je_seite_s": "ocr_time_limit_per_page_s",
    "ocr_zeitlimit_min_s": "ocr_time_limit_min_s",
    "papierkorb": "trash",
    "bestand_gueltig_s": "scan_cache_s",
    "modell_beim_start_laden": "load_model_at_start",
    "modell_offline": "model_offline",
}
CONFIG_ALTE_SCHLUESSEL = []
for _alt, _neu in CONFIG_ALIASE.items():
    if _alt in CONFIG:
        CONFIG_ALTE_SCHLUESSEL.append(_alt)
        CONFIG.setdefault(_neu, CONFIG[_alt])
        del CONFIG[_alt]

_configured_dir = os.path.expanduser(CONFIG["pdf_base_dir"])   # G3: auch ~/... erlaubt
if os.path.isabs(_configured_dir):
    PDF_BASE_DIR = _configured_dir
else:
    PDF_BASE_DIR = os.path.join(APP_DIR, _configured_dir)

# Der Index liegt seit S4 (PLAN_SQLITE.md) ausschliesslich in see_also.db.
# Die frueheren JSON-Dateien (meta.json, index.npz, id_order.json,
# index_state.json) werden nicht mehr gelesen oder geschrieben.
if str(CONFIG.get("storage") or "sqlite").strip().lower() != "sqlite":
    raise SystemExit('config.json: "storage" can only be "sqlite" (the JSON storage was removed). '
                     'Delete the entry or set it to "sqlite".')
DB_FILE = os.path.join(APP_DIR, "see_also.db")
DB_FEHLT_MELDUNG = ("There is no index yet (see_also.db is missing). \"Rebuild index\" on the status page creates it. "
                    "If see_also.db was only moved, put it back first; otherwise everything is embedded again.")
# Liegen die alten JSON-Dateien noch im Projektordner, weist die Statusseite darauf hin.
ALTE_JSON_DATEIEN = ["meta.json", "index.npz", "id_order.json", "index_state.json"]

# B1: Ausschlussliste. Namen von Ordnern der ersten Unterebene unter
# pdf_base_dir, die nicht zum Bestand gehoeren. Verglichen wird in
# Unicode-NFC (macOS liefert Namen oft zerlegt, in config.json stehen sie
# meist zusammengesetzt); die Pfade selbst werden nie normalisiert.
def _nfc(name: str) -> str:
    return unicodedata.normalize("NFC", name)

EXCLUDED_AREAS = {_nfc(str(n)).strip().strip("/") for n in (CONFIG.get("excluded_areas") or [])
                  if str(n).strip().strip("/")}


def area_of(rel_path: str) -> str:
    """Bereich eines relativen Pfads = sein erster Pfadbestandteil
    (bei einer Datei direkt in der Wurzel: leerer String)."""
    parts = rel_path.replace(os.sep, "/").split("/")
    return parts[0] if len(parts) > 1 else ""


def is_excluded(rel_path: str) -> bool:
    return _nfc(area_of(rel_path)) in EXCLUDED_AREAS


# O1b: Programmpakete (macOS-Bundles) sind Ordner, die der Finder als eine
# Datei zeigt; ihre PDFs sind Programmbestandteile (Symbole, Hilfeseiten),
# keine Dokumente. Sie werden beim Scannen nicht betreten - fuer Index und OCR.
# Erkannt an der Endung oder daran, dass ein Ordner "Name.endung" einen
# Unterordner "Contents" hat (so sind Bundles aufgebaut, z. B. Plug-ins
# wie .rapidweaverplugin).
BUNDLE_ENDUNGEN = {".app", ".pkg", ".mpkg", ".bundle", ".plugin", ".framework", ".kext", ".appex",
                   ".xpc", ".prefpane", ".qlgenerator", ".mdimporter", ".saver", ".component", ".wdgt"}


def ist_programmpaket(ordner: str, name: str) -> bool:
    endung = os.path.splitext(name)[1].lower()
    if not endung or endung == name.lower():
        return False
    if endung in BUNDLE_ENDUNGEN:
        return True
    return os.path.isdir(os.path.join(ordner, name, "Contents"))


def walk_pdfs(base_dir: str):
    """Alle PDF-Dateien unter base_dir als relative Pfade. Ausgeschlossene
    Bereiche werden gar nicht betreten. Symbolischen Links wird nicht gefolgt,
    weder bei Ordnern (os.walk folgt ohnehin nicht) noch bei Dateien.
    Finder-Aliasse sind gewoehnliche Dateien ohne .pdf-Endung und fallen
    damit von selbst heraus."""
    for root, dirs, files in os.walk(base_dir, followlinks=False):
        if os.path.normpath(root) == os.path.normpath(base_dir):
            dirs[:] = [d for d in dirs if _nfc(d) not in EXCLUDED_AREAS]
        dirs[:] = [d for d in dirs if not ist_programmpaket(root, d)]
        for name in files:
            if not name.lower().endswith(".pdf"):
                continue
            full = os.path.join(root, name)
            if os.path.islink(full):
                continue
            yield os.path.relpath(full, base_dir)


# B3: Suchbereich. Auswahl der Bereiche, in denen gesucht wird (Statusseite).
# Unterschied zu "ausgeschlossen": Ausgeschlossenes wird gar nicht erfasst;
# der Suchbereich grenzt nur die Suche im vorhandenen Index ein und ist mit
# einem Klick wieder aufgehoben. None = alle Bereiche. Gespeichert in
# suchbereich.json im Projektordner (ueberlebt einen Neustart).
SEARCH_SCOPE_FILE = os.path.join(APP_DIR, "suchbereich.json")
ROOT_AREA_LABEL = "(top level)"
ROOT_AREA_LABEL_ALT = "(direkt in der Wurzel)"   # bis G1a, kann in suchbereich.json stehen


def _load_search_scope():
    try:
        with open(SEARCH_SCOPE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        areas = data.get("bereiche")
        if areas is None:
            return None
        return {ROOT_AREA_LABEL if a == ROOT_AREA_LABEL_ALT else a for a in (_nfc(str(a)) for a in areas)}
    except (OSError, ValueError, AttributeError):
        return None


SEARCH_SCOPE = _load_search_scope()


def _save_search_scope(scope):
    tmp = SEARCH_SCOPE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"bereiche": None if scope is None else sorted(scope)}, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, SEARCH_SCOPE_FILE)


CROSS_PAGE_OVERLAP_CHARS = CONFIG.get("cross_page_overlap_chars", 0)
PHRASE_MAX_CHARS = CONFIG.get("phrase_max_chars", 2000)
SEARCH_MAX_RESULTS = CONFIG.get("search_max_results", 30)
CHUNK_LIST_MAX_ENTRIES = CONFIG.get("chunk_list_max_entries", 6)

LOG_DIR = os.path.join(APP_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

STATUS_LOCK = threading.Lock()
INDEX_PROGRESS = {
    "is_running": False,
    "mode": None,
    "current": 0,
    "total": 0,
    "current_chunk": 0,
    # VAI 1.1: Phase des Laufs (scan, extract, merge, embed, save, done,
    # error), letzte Fehlermeldung und Name der Logdatei.
    "phase": None,
    "error": None,
    "log_file": None,
    # VAI 1.3: Fortschritt der Einbettung (Chunks), Durchsatz (Chunks/s,
    # ohne Modellladen und ersten Stapel) und geschaetzte Restzeit (s).
    "embed_done": 0,
    "embed_total": 0,
    "embed_rate": None,
    "embed_eta": None,
    # VAI 1.4: Abbruch auf Wunsch (kooperativ, zwischen zwei Schritten)
    "cancel_requested": False,
}

INDEX_CANCEL = threading.Event()


class IndexCancelled(Exception):
    """Der Indexlauf wurde auf Wunsch abgebrochen (kein Fehler)."""


def _check_cancel() -> None:
    if INDEX_CANCEL.is_set():
        raise IndexCancelled()


def _enter_save_phase() -> None:
    """Letzte Abbruchstelle: Ab dem Speichern ist der Lauf nicht mehr
    abbrechbar. Pruefung und Phasenwechsel unter demselben Lock wie die
    Route /index/cancel, damit kein Abbruch 'dazwischen' verloren geht."""
    with STATUS_LOCK:
        if INDEX_CANCEL.is_set():
            raise IndexCancelled()
        INDEX_PROGRESS["phase"] = "save"
    _log_phase("save")

# O1: OCR-Lauf fuer fehlende Dateien (ocr.py, eigener Prozess). Ersetzt das
# fruehere Remastering je Datei (_remasterd.pdf neben dem Original). Die
# OCR-Fassungen liegen im Schattenordner, die Originale bleiben unberuehrt.
# P1b: Modell offline laden, sobald es im Zwischenspeicher liegt (siehe embedder.py).
embedder.OFFLINE = CONFIG.get("model_offline", "auto")
OCR_ORDNER = os.path.abspath(os.path.expanduser(CONFIG.get("ocr_folder") or "~/see_also_ocr"))
OCR_SPRACHEN = CONFIG.get("ocr_languages") or "eng+deu+fra"
OCR_ZEITLIMIT_JE_SEITE_S = float(CONFIG.get("ocr_time_limit_per_page_s", 60))
OCR_ZEITLIMIT_MIN_S = float(CONFIG.get("ocr_time_limit_min_s", 300))
OCR_JOBS = CONFIG.get("ocr_jobs")
# O1e: so viele Dateien gleichzeitig; die Kerne werden aufgeteilt (ocr_jobs
# leer: Kerne / ocr_parallel je Datei).
OCR_PARALLEL = max(1, int(CONFIG.get("ocr_parallel", 4)))
OCR_FILE = os.path.join(APP_DIR, "ocr.py")
OCR_LOCK = threading.Lock()
OCR_LAUF = {"is_running": False, "phase": None, "error": None, "log_file": None,
            "total": 0, "current": 0, "current_file": None, "erfasst": 0, "nicht_erfasst": 0,
            "seiten_fertig": 0, "seiten_gesamt": 0, "eta_s": None, "s_je_seite": None,
            "cancel_requested": False, "anzahl": None}
OCR_ZEILEN = []          # letzte Logzeilen fuer die Live-Anzeige
OCR_ZEILEN_MAX = 40
_OCR_CHILD = [None]

# Ergebnis der letzten Suche - fuer eine einzelne, lokale Nutzung
# ausreichend (kein Mehrnutzer-Betrieb, keine Sessions noetig). Wird von
# document_chunks() gelesen, um Chunks bei aktiver Suche nach Score statt
# nach Position zu sortieren - UND von /search/last, damit ein
# Seitenwechsel (index.html -> status.html -> zurueck) die Suche nicht
# verliert, wie es vorher der Fall war.
SEARCH_LOCK = threading.Lock()
LAST_SEARCH_SCORES: dict[int, float] = {}  # chunk_id -> score
LAST_SEARCH_PHRASE: str = ""
LAST_SEARCH_DOCUMENTS: list[dict] = []
# Vollstaendige (nicht auf SEARCH_MAX_RESULTS gekuerzte) Liste der besten
# Scores pro Dokument, absteigend sortiert - Grundlage fuer die
# Kontrastfunktion (score_contrast.py). Getrennt von LAST_SEARCH_DOCUMENTS
# gehalten, weil genau die Kuerzung dort die "Falle: abgeschnittene Liste"
# waere, vor der das Kontrastfunktions-Dokument (Abschnitt 5) explizit
# warnt: Boden und Streuung muessen ueber die VOLLE Liste geschaetzt werden.
LAST_SEARCH_ALL_SCORES: list[float] = []

# Briefkasten (Bruecke von Obsidian): Eine Anfrage kommt von aussen per
# POST /briefkasten, wird wie eine Suche ausgefuehrt und steht danach auf der
# Startseite. Nur im Speicher (die letzten Briefe), nichts auf der Platte.
# SEARCH_SERIAL zaehlt jede Suche und jedes Leeren: eine offene Startseite
# fragt ihn ab und zeigt eine von aussen gekommene Anfrage ohne Neuladen.
BRIEFKASTEN_MAX = 20
BRIEFE: list[dict] = []
LAST_SEARCH_BRIEF: dict | None = None
SEARCH_SERIAL: int = 0
LAST_SEARCH_SCOPE: list = [None]   # Suchbereich der letzten Suche (fuer die Anzeige)


@app.context_processor
def inject_pdf_base_dir():
    # pdf_base_dir: voller Pfad (fuer Befehle); _anzeige: mit ~ fuer das Benutzerverzeichnis
    return {"pdf_base_dir": PDF_BASE_DIR, "pdf_base_dir_anzeige": _ocr.anzeige_pfad(PDF_BASE_DIR)}


@app.template_filter("anzeige")
def _filter_anzeige(pfad):
    return _ocr.anzeige_pfad(pfad)


@app.context_processor
def inject_neu_count():
    # Fuer das "index -N"-Badge im Header, auf jeder Seite verfuegbar.
    # Reine Lesezugriffe (scan_pdfs, load_index_state) - fuer die
    # aktuelle Bestandsgroesse unproblematisch, siehe README-Hinweis zur
    # spaeteren Abloesung durch einen Indexwert bei grossem Bestand.
    current_scan = bestand()
    state = load_index_state()
    drift = compute_drift_metrics(current_scan, state)
    return {"neu_count": drift["neu_minus_entfernt"]}


def build_pdf_link(dateiname: str, seite: int) -> str:
    encoded_name = quote(dateiname)
    return f"/pdf/{encoded_name}#page={seite}"


def scan_pdfs(base_dir: str) -> dict:
    result = {}
    for rel in walk_pdfs(base_dir):
        result[rel] = os.path.getmtime(os.path.join(base_dir, rel))

    # See-Also-III, Workflow-Spezifikation Punkt 9: Existiert zu einer
    # Datei eine "_remasterd"-Variante, wird ausschliesslich diese fuer
    # Chunking/Embedding verwendet - das Original wird technisch wie eine
    # geloeschte Datei behandelt (bestehender "stillgelegt"-Mechanismus in
    # assign_ids_and_merge greift dafuer bereits, sobald das Original hier
    # aus dem Scan-Ergebnis verschwindet). Bleibt real auf der Platte und
    # ueber /pdf/<path> fuer die reine Ansicht erreichbar (u.a. wegen der
    # Diagramme), taucht aber weder als zu indizierende noch als
    # "fehlende" Datei auf.
    superseded = set()
    for rel in result:
        p = Path(rel)
        if p.stem.endswith("_remasterd"):
            continue
        remastered_rel = str(p.with_name(p.stem + "_remasterd" + p.suffix))
        if remastered_rel in result:
            superseded.add(rel)
    for rel in superseded:
        del result[rel]

    return result


# P1 (19.11.2026): Ein Gang ueber den Ordnerbaum kostet beim Referenz-Mac rund
# 3 s (17.000 PDFs). Frueher lief er bei JEDER Seite (Kopfzeilen-Zaehler) und
# bei der Statusseite fuenf- bis sechsmal (gemessen: Statusseite 19-20 s,
# Suchseite 3,3 s) - bei offener Statusseite alle 60 s. Jetzt teilen sich alle
# Anzeigen ein Ergebnis, das hoechstens BESTAND_GUELTIG_S alt ist. Nach einem
# Index- oder OCR-Lauf wird es verworfen. Der Indexlauf selbst scannt frisch.
BESTAND_GUELTIG_S = float(CONFIG.get("scan_cache_s", 60))
_bestand_lock = threading.Lock()
_bestand_cache = {"zeit": 0.0, "daten": None, "dauer": 0.0}


def bestand(frisch: bool = False) -> dict:
    """{rel_pfad: mtime} aller indizierbaren PDFs, zwischengespeichert.
    Nicht veraendern (geteiltes Objekt)."""
    with _bestand_lock:
        jetzt = time.monotonic()
        if (not frisch and _bestand_cache["daten"] is not None
                and jetzt - _bestand_cache["zeit"] < BESTAND_GUELTIG_S):
            return _bestand_cache["daten"]
        t0 = time.monotonic()
        daten = scan_pdfs(PDF_BASE_DIR)
        _bestand_cache.update(zeit=time.monotonic(), daten=daten, dauer=time.monotonic() - t0)
        return daten


def bestand_verwerfen() -> None:
    with _bestand_lock:
        _bestand_cache["daten"] = None


def count_superseded_originals(base_dir: str) -> int:
    # Fuer die Statusseite: wie viele Originale wurden durch eine
    # "_remasterd"-Variante ersetzt und damit aus scan_pdfs() ausgeblendet?
    # Spiegelt dieselbe Filterlogik wie dort, eigenstaendig gehalten statt
    # scan_pdfs() selbst zu veraendern - vermeidet jedes Risiko fuer die
    # bestehenden Aufrufer von scan_pdfs() (Indexierung, find_missing_files).
    # Grund fuer diese Zeile ueberhaupt: ein Finder-Zaehlung/find-Aufruf
    # zeigt ALLE .pdf-Dateien inklusive der ausgeblendeten Originale, "Anzahl
    # PDFs" auf der Statusseite zeigt dagegen nur die tatsaechlich indexierbaren
    # - beide Zahlen fuer sich genommen sind korrekt, aber ohne diese
    # zweite Zeile leicht zu verwechseln.
    all_paths = set(walk_pdfs(base_dir))

    count = 0
    for rel in all_paths:
        p = Path(rel)
        if p.stem.endswith("_remasterd"):
            continue
        remastered_rel = str(p.with_name(p.stem + "_remasterd" + p.suffix))
        if remastered_rel in all_paths:
            count += 1
    return count


_zustand_cache = {"gen": None, "state": None}


def load_index_state() -> dict:
    """Zustand aus see_also.db. P1: zwischengespeichert, solange sich die
    Generation nicht aendert (der Zustand aendert sich nur mit einem Lauf).
    Rueckgabe ist eine flache Kopie; die grossen Eintraege nicht veraendern."""
    if not os.path.exists(DB_FILE):
        return {"last_full_index_at": None, "last_incremental_index_at": None,
                "full_snapshot": {}, "last_run_snapshot": {}, "next_chunk_id": 0}
    with _db_zugriff() as con:
        gen = speicher.generation(con)
        if _zustand_cache["gen"] != gen or _zustand_cache["state"] is None:
            state = speicher.zustand_lesen(con)
            state.setdefault("next_chunk_id", 0)
            _zustand_cache.update(gen=gen, state=state)
    return dict(_zustand_cache["state"])



# --- VAI N1 / S2: Index im Speicher ---------------------------------------
# Im Speicher stehen nur die Vektoren, die Chunk-IDs und die Zuordnung
# Zeile -> Dokument. Texte holt der Server bei Bedarf aus see_also.db. Neu
# geladen wird, wenn ein Indexlauf die generation in der Datenbank erhoeht hat.

class LoadedIndex:
    __slots__ = ("sig", "matrix", "id_order",
                 "doc_paths", "doc_of_row", "doc_counts", "doc_first_row", "load_seconds",
                 "doc_excluded", "n_visible", "doc_area", "area_chunks", "doc_index")


INDEX_CACHE_LOCK = threading.Lock()
_INDEX_CACHE: "LoadedIndex | None" = None


# S2: EINE Verbindung zur Datenbank fuer den ganzen Server, geschuetzt durch
# eine Sperre (Zugriffe dauern Millisekunden). Frueher eine je Thread: Flask
# startet fuer jede Anfrage einen neuen Thread, also oeffnete jede Anfrage eine
# neue Verbindung, und auf macOS wartete deren erster Zugriff jedes Mal 5,4 s
# (gemessen 15.11.2026, siehe speicher.verbinden). Normal geoeffnet, Schreiben
# gesperrt. Wird die Datei ersetzt (neuer Umzug), merkt das die Inode-Nummer,
# und die Verbindung wird erneuert.
_DB_LOCK = threading.RLock()
_DB_STATE = {"con": None, "ino": None, "geoeffnet": 0}


class _DbZugriff:
    """with _db_zugriff() as con: ... - haelt die Sperre fuer die Dauer des Blocks."""
    def __enter__(self):
        _DB_LOCK.acquire()
        try:
            return _db_con()
        except BaseException:
            _DB_LOCK.release()
            raise

    def __exit__(self, *exc):
        _DB_LOCK.release()
        return False


def _db_zugriff():
    return _DbZugriff()


def _db_con():
    """Nur mit gehaltener _DB_LOCK aufrufen."""
    ino = os.stat(DB_FILE).st_ino          # FileNotFoundError, wenn die Datenbank fehlt
    con = _DB_STATE["con"]
    if con is None or _DB_STATE["ino"] != ino:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass
        t0 = time.monotonic()
        con = speicher.verbinden(DB_FILE, nur_lesen=True, threads=True)
        speicher.generation(con)           # erster Zugriff: hier faellt eine Wartezeit an, falls es sie gibt
        dt = time.monotonic() - t0
        _DB_STATE.update(con=con, ino=ino, geoeffnet=_DB_STATE["geoeffnet"] + 1)
        print(f"[see_also] Opened see_also.db ({dt:.2f} s)", flush=True)
    return con


def _index_signature():
    try:
        with _db_zugriff() as con:
            return ("sqlite", _DB_STATE["ino"], speicher.generation(con))
    except Exception:
        return ("sqlite", None, None)


def _build_index(sig) -> "LoadedIndex":
    return _build_index_db(sig)


def _build_index_db(sig) -> "LoadedIndex":
    """S2: Index aus see_also.db. Im Speicher nur Vektoren, IDs und die
    Zuordnung Zeile -> Dokument; Texte holt der Server bei Bedarf
    (_doc_chunk_entries, _chunk_entry). Alles in einer Lesetransaktion,
    damit IDs, Pfade und Vektoren zum selben Stand gehoeren."""
    t0 = time.monotonic()
    if not os.path.exists(DB_FILE):
        # Leerer Index statt Absturz; run_search und die Statusseite melden es.
        idx = LoadedIndex()
        idx.sig = sig
        idx.matrix = np.zeros((0, embedder.EMBEDDING_DIM), dtype=np.float32)
        idx.id_order, idx.doc_paths, idx.doc_index = [], [], {}
        idx.doc_of_row = np.zeros(0, dtype=np.int32)
        _finish_index(idx)
        idx.load_seconds = 0.0
        return idx
    with _db_zugriff() as con:
        con.execute("BEGIN")
        try:
            gen = speicher.generation(con)
            ip = speicher.ids_und_pfade(con)
            ids, matrix = speicher.vektoren_laden(con)
        finally:
            con.rollback()                 # nur gelesen
    if len(ip) != len(ids) or not np.array_equal(ids, np.fromiter((i for i, _ in ip), dtype=np.int64, count=len(ip))):
        raise RuntimeError("see_also.db: chunks and vectors do not match (run tools/check_index.py)")
    idx = LoadedIndex()
    idx.sig = ("sqlite", sig[1], gen)
    idx.matrix = matrix
    idx.id_order = ids.tolist()
    idx.doc_paths = sorted({p for _, p in ip})
    pos = {p: i for i, p in enumerate(idx.doc_paths)}
    idx.doc_index = pos
    idx.doc_of_row = np.fromiter((pos[p] for _, p in ip), dtype=np.int32, count=len(ip))
    del ip
    _finish_index(idx)
    idx.load_seconds = time.monotonic() - t0
    return idx


def _finish_index(idx) -> None:
    """Gemeinsamer Teil fuer beide Speicher: Chunks je Dokument, erste Zeile
    je Dokument, Ausschluss, Bereiche."""
    n = len(idx.id_order)
    idx.doc_counts = np.bincount(idx.doc_of_row, minlength=len(idx.doc_paths))
    # Erste Matrixzeile je Dokument: Bei exakt gleichem Score entscheidet die
    # Reihenfolge im Index (z. B. zwei identische PDFs). JSON: Reihenfolge in
    # meta.json; SQLite: aufsteigende Chunk-ID.
    first = np.full(len(idx.doc_paths), n, dtype=np.int64)
    np.minimum.at(first, idx.doc_of_row, np.arange(n, dtype=np.int64))
    idx.doc_first_row = first
    # B1: Dokumente aus ausgeschlossenen Bereichen, die noch im Index stehen
    # (bis der naechste Lauf sie entfernt), sind ab sofort unsichtbar.
    idx.doc_excluded = np.array([is_excluded(p) for p in idx.doc_paths], dtype=bool)
    idx.n_visible = int(idx.doc_counts[~idx.doc_excluded].sum()) if len(idx.doc_paths) else 0
    # B3: Bereich je Dokument (NFC, wie in der Ausschlussliste) und Chunks je Bereich.
    idx.doc_area = [_nfc(area_of(p)) or ROOT_AREA_LABEL for p in idx.doc_paths]
    idx.area_chunks = {}
    for k, a in enumerate(idx.doc_area):
        if not idx.doc_excluded[k]:
            idx.area_chunks[a] = idx.area_chunks.get(a, 0) + int(idx.doc_counts[k])



# S2: Zugriff auf Chunk-Texte, unabhaengig vom Speicher.

def _doc_chunk_entries(idx, path: str) -> list[dict]:
    """Chunks eines Dokuments (id, position_in_file, seite, zeile, text).
    Bei SQLite ist text auf 150 Zeichen gekuerzt (mehr zeigt die Chunk-Liste nicht)."""
    with _db_zugriff() as con:
        rows = con.execute("SELECT id, position_in_file, seite, zeile, substr(text, 1, 150) "
                           "FROM chunks WHERE path=?", (path,)).fetchall()
    return [{"id": r[0], "position_in_file": r[1], "seite": r[2], "zeile": r[3], "text": r[4], "path": path}
            for r in rows]


def _chunk_entry(idx, chunk_id: int) -> dict | None:
    with _db_zugriff() as con:
        e = speicher.chunk_lesen(con, chunk_id)
    if e is not None:
        e.pop("text_for_embedding", None)
    return e


def _rss_gb() -> float:
    """Aktueller Speicher des Prozesses in GB (fuer die Messung; ohne
    Zusatzpaket). Auf macOS liefert ps die RSS in KB."""
    try:
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(os.getpid())],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        return int(out) / 1024 / 1024
    except Exception:
        return float("nan")


def get_index() -> "LoadedIndex":
    global _INDEX_CACHE
    sig = _index_signature()
    cached = _INDEX_CACHE
    if cached is not None and cached.sig == sig:
        return cached
    with INDEX_CACHE_LOCK:
        cached = _INDEX_CACHE
        sig = _index_signature()
        if cached is not None and cached.sig == sig:
            return cached
        last_error = None
        for _ in range(3):
            try:
                idx = _build_index(sig)
                break
            except RuntimeError as e:
                last_error = e
                time.sleep(0.5)
                sig = _index_signature()
        else:
            raise last_error
        if _index_signature() == idx.sig:
            _INDEX_CACHE = idx
        print(f"[see_also] Index loaded into memory: {len(idx.id_order)} chunks, "
              f"{len(idx.doc_paths)} documents, {idx.load_seconds:.1f} s, "
              f"process now {_rss_gb():.2f} GB", flush=True)
        return idx



# ---------------------------------------------------------------------
# Fehlerbehandlung und Testschalter
# ---------------------------------------------------------------------
# Seit S3/S4 schreibt ein Indexlauf sein Ergebnis in EINER Transaktion in
# see_also.db (speicher.lauf_festschreiben). Abbruch, Fehler oder Absturz vor
# dem Festschreiben lassen den bisherigen Stand unveraendert; das frueher
# noetige Umschalten von Dateien (*.tmp, *.prev, Marker) entfaellt.
CONFIG_FILE = os.path.join(APP_DIR, "config.json")
STARTUP_NOTICES: list[str] = []

# Orte, an denen config.json -> "debug_inject_error" einen Fehler ausloest
# (nur zum Testen der Fehlerbehandlung, Standard: null = aus).
#   extract   Ausnahme nach dem ersten gelesenen PDF
#   embed     Ausnahme vor der Einbettung
#   save      Ausnahme vor dem Festschreiben (vor Beginn der Transaktion)
#   swap      Ausnahme innerhalb der Transaktion, unmittelbar vor dem Festschreiben
#   swap_kill Prozess wird innerhalb der Transaktion hart beendet (os._exit),
#             simuliert Absturz/Stromausfall; SQLite verwirft die halbe Transaktion
INJECT_POINTS = ("extract", "embed", "save", "swap", "swap_kill")


_RUN_LOG = [None]   # offenes Log des laufenden Indexlaufs (fuer Phasenzeilen)


def _spitze_gb() -> float:
    """Hoechster Speicherstand dieses Prozesses bisher, in GB.

    macOS (S4): lifetime_max_phys_footprint aus proc_pid_rusage - derselbe
    Wert, den die Aktivitaetsanzeige als "Speicher" zeigt, also einschliesslich
    der GPU-Puffer (Metal) und komprimierter Seiten. ru_maxrss zaehlte diese
    nicht (Klir-Lauf 15.11.2026: Log 1,10 GB, Aktivitaetsanzeige 5,85 GB).
    Sonst und falls das nicht geht: ru_maxrss (Linux: KB)."""
    import resource
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform != "darwin":
        return r / 1e6
    fp = _darwin_max_footprint()
    return fp / 1e9 if fp and fp >= r * 0.5 else r / 1e9


_RUSAGE_FN = []


def _aktuell_gb() -> float | None:
    """Aktueller Speicherstand (macOS: phys_footprint wie die Aktivitaetsanzeige)."""
    if sys.platform != "darwin":
        return None
    fp = _darwin_rusage_feld(7)       # ri_phys_footprint
    return fp / 1e9 if fp else None


# S5: Ruhezustand erkennen. time.monotonic() bleibt auf macOS stehen, solange
# der Mac schlaeft, time.time() (Wanduhr) laeuft weiter. Waechst der Abstand,
# hat der Mac geschlafen (Bibliothek 16.11.2026: 113 min, sah aus wie eine
# Luecke im Lauf).
_UHREN = {"wand0": None, "mono0": None, "gemeldet": 0.0}


def _uhren_start() -> None:
    _UHREN.update(wand0=time.time(), mono0=time.monotonic(), gemeldet=0.0)


def _geschlafen_s() -> float:
    if _UHREN["wand0"] is None:
        return 0.0
    return max(0.0, (time.time() - _UHREN["wand0"]) - (time.monotonic() - _UHREN["mono0"]))


def _schlaf_melden(log) -> None:
    """Schreibt eine Zeile, wenn seit der letzten Meldung mindestens 1 min
    Ruhezustand dazugekommen ist."""
    s = _geschlafen_s()
    neu = s - _UHREN["gemeldet"]
    if neu >= 60 and log is not None:
        try:
            log.write(f"[{datetime.now().strftime('%H:%M:%S')}] Mac slept about {neu / 60:.0f} min "
                      f"(the run was paused that long)\n")
        except Exception:
            pass
        _UHREN["gemeldet"] = s


_CAFFEINATE = []


def _wachhalten_start(log) -> None:
    """S5: Den Mac waehrend des Laufs nicht einschlafen lassen (caffeinate -i,
    Bordmittel von macOS; endet von selbst mit diesem Prozess). Der Bildschirm
    darf dunkel werden; Zuklappen im Akkubetrieb laesst ihn trotzdem schlafen."""
    if sys.platform != "darwin" or not os.path.exists("/usr/bin/caffeinate"):
        return
    try:
        _CAFFEINATE.append(subprocess.Popen(["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())],
                                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                            stderr=subprocess.DEVNULL))
        log.write("Sleep prevented during the run (caffeinate -i)\n")
    except Exception as e:
        log.write(f"caffeinate not started: {e}\n")


def _wachhalten_ende() -> None:
    while _CAFFEINATE:
        p = _CAFFEINATE.pop()
        try:
            p.terminate()
            p.wait(timeout=5)
        except Exception:
            pass


def _darwin_max_footprint() -> int | None:
    """proc_pid_rusage(getpid(), RUSAGE_INFO_V4) -> ri_lifetime_max_phys_footprint
    (das 29. uint64-Feld, Index 28)."""
    return _darwin_rusage_feld(28)


def _darwin_rusage_feld(index: int) -> int | None:
    """Ein uint64-Feld aus struct rusage_info_v4 (16 Byte uuid, danach nur
    uint64-Felder; 7 = ri_phys_footprint, 28 = ri_lifetime_max_phys_footprint).
    Ohne Unterprozess, nur ein Systemaufruf."""
    import ctypes
    try:
        if not _RUSAGE_FN:
            lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
            fn = lib.proc_pid_rusage
            fn.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
            fn.restype = ctypes.c_int
            _RUSAGE_FN.append(fn)

        class _RU(ctypes.Structure):
            _fields_ = [("uuid", ctypes.c_uint8 * 16), ("f", ctypes.c_uint64 * 40)]
        ru = _RU()
        if _RUSAGE_FN[0](os.getpid(), 4, ctypes.byref(ru)) != 0:
            return None
        return int(ru.f[index])
    except Exception:
        return None


def _log_phase(phase: str) -> None:
    lf = _RUN_LOG[0]
    if lf is not None:
        try:
            _schlaf_melden(lf)
            lf.write(f"[{datetime.now().strftime('%H:%M:%S')}] Phase: {phase}"
                     f"  (memory peak so far {_spitze_gb():.2f} GB)\n")
            lf.flush()
        except Exception:
            pass


def _set_phase(phase: str) -> None:
    with STATUS_LOCK:
        INDEX_PROGRESS["phase"] = phase
    _log_phase(phase)


def _maybe_inject(point: str) -> None:
    # config.json wird hier bewusst jedes Mal frisch gelesen, damit der
    # Schalter ohne Neustart des Servers umgestellt werden kann.
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            wanted = json.load(f).get("debug_inject_error")
    except Exception:
        return
    if wanted != point:
        return
    if point == "swap_kill":
        print("[TEST] debug_inject_error = swap_kill: killing the process.", flush=True)
        os._exit(1)
    raise RuntimeError(f"Intentional test error (debug_inject_error = {point!r})")



def run_indexing_guarded(mode: str) -> None:
    """Rahmen um run_indexing: Gleich, was schiefgeht, wird der Zustand
    "laeuft" wieder freigegeben (sonst bleiben die Buttons bis zum Neustart
    des Servers gesperrt), die Fehlermeldung wird festgehalten, angezeigt und
    mit Traceback ins Log geschrieben."""
    try:
        run_indexing(mode)
    except IndexCancelled:
        with STATUS_LOCK:
            phase = INDEX_PROGRESS.get("phase")
            log_name = INDEX_PROGRESS.get("log_file")
        if log_name:
            try:
                with open(os.path.join(LOG_DIR, log_name), "a", encoding="utf-8") as lf:
                    lf.write(f"\n[STOPPED on request in phase {phase!r}] "
                             "The previous index is unchanged.\n")
            except OSError:
                pass
        with STATUS_LOCK:
            INDEX_PROGRESS.update(phase="cancelled", error=None)
    except Exception as e:
        traceback.print_exc()
        with STATUS_LOCK:
            phase = INDEX_PROGRESS.get("phase")
            log_name = INDEX_PROGRESS.get("log_file")
        if not log_name:
            log_name = f"index_error_{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
        try:
            with open(os.path.join(LOG_DIR, log_name), "a", encoding="utf-8") as lf:
                lf.write(f"\n[ABORTED in phase {phase!r}] {type(e).__name__}: {e}\n"
                         f"{traceback.format_exc()}\n")
        except OSError:
            pass
        with STATUS_LOCK:
            INDEX_PROGRESS.update(
                error=f"Phase {phase}: {type(e).__name__}: {e}",
                phase="error", log_file=log_name)
    finally:
        with STATUS_LOCK:
            INDEX_PROGRESS["is_running"] = False
            bestand_verwerfen()   # P1: Bestand nach dem Lauf neu einlesen
            INDEX_PROGRESS["cancel_requested"] = False


def compute_drift_metrics(current_scan: dict, state: dict) -> dict:
    full_snapshot = state.get("full_snapshot", {})
    if not full_snapshot:
        full_drift_pct = None
    else:
        changed = 0
        for path, mtime in current_scan.items():
            if path not in full_snapshot or full_snapshot[path] != mtime:
                changed += 1
        removed = sum(1 for path in full_snapshot if path not in current_scan)
        total_relevant = len(current_scan) or 1
        full_drift_pct = round(100 * (changed + removed) / total_relevant, 1)

    last_run_snapshot = state.get("last_run_snapshot", {})
    neu_seit_letzter = 0
    for path, mtime in current_scan.items():
        if path not in last_run_snapshot or last_run_snapshot[path] != mtime:
            neu_seit_letzter += 1

    # Gegenstueck zu neu_seit_letzter: Dateien, die im letzten bekannten
    # Snapshot noch vorhanden waren, aber aktuell nicht mehr gescannt
    # werden (geloescht oder umbenannt). Ohne diese Zaehlung blieb eine
    # geloeschte, aber noch indizierte Datei fuer den Nutzer unsichtbar -
    # die Badge zeigte weiterhin "0", obwohl tatsaechlich nachindiziert
    # werden muesste, um die verwaisten Chunks stillzulegen.
    entfernt_seit_letzter = sum(1 for path in last_run_snapshot if path not in current_scan)

    return {
        "full_drift_pct": full_drift_pct,
        "neu_seit_letzter_indizierung": neu_seit_letzter,
        "entfernt_seit_letzter_indizierung": entfernt_seit_letzter,
        # Signierter Netto-Wert fuer die kompakte Kopfzeilen-Badge: positiv
        # bei ueberwiegend neuen/geaenderten Dateien, negativ bei
        # ueberwiegend entfernten - in beiden Faellen ist eine
        # Nachindizierung faellig, nur mit unterschiedlichem Vorzeichen.
        # Die Statusseite selbst zeigt beide Rohzahlen separat und
        # unsigniert (siehe _status_block.html), damit nichts verschleiert
        # wird - nur die Badge fasst sie zu einer Zahl zusammen.
        "neu_minus_entfernt": neu_seit_letzter - entfernt_seit_letzter,
    }


# B5b: PDFs werden in einem abgeschotteten Arbeitsprozess gelesen
# (extract_worker.py). Stuerzt PyMuPDF bei einer Datei im C-Code ab oder
# haengt es, endet nur der Arbeitsprozess; die Datei wird als Problemdatei
# vermerkt und der Lauf geht weiter. pdf_isoliert_lesen=false schaltet zurueck
# auf das Lesen im Server-Prozess (Notbehelf, und fuer aeltere Tests).
PDF_ISOLATED = bool(CONFIG.get("pdf_read_isolated", True))
PDF_TIME_LIMIT_S = float(CONFIG.get("pdf_time_limit_s", 120))
WORKER_FILE = os.path.join(APP_DIR, "extract_worker.py")


class ExtractCrashed(Exception):
    def __init__(self, befund, meldung):
        super().__init__(meldung)
        self.befund = befund      # "absturz" oder "zeitlimit"
        self.meldung = meldung


class IsolatedExtractor:
    """Haelt einen Arbeitsprozess und gibt ihm Datei fuer Datei. Wird ein
    Abbruch angefordert, waehrend eine Datei gelesen wird, wird der
    Arbeitsprozess beendet (Abbruch auch mitten in einem grossen PDF)."""

    def __init__(self):
        self.proc = None
        self.buf = b""
        self.err = None

    def _start(self):
        # stderr in eine Datei, nicht in eine Pipe: MuPDF schreibt viele
        # Warnungen; eine nicht geleerte Pipe liefe voll und der Arbeitsprozess
        # bliebe stehen. S4: feste Datei in logs/, bei jedem Start geleert,
        # statt einer temporaeren Datei - deren Loeschen kostete auf dem Referenz-Mac
        # (haengender Virenschutz) jedes Mal 5,4 s.
        if self.err is not None:
            try:
                self.err.close()
            except Exception:
                pass
        self.err = open(os.path.join(LOG_DIR, "lese_prozess_stderr.log"), "w+b")
        t0 = time.monotonic()
        self.proc = subprocess.Popen(
            [sys.executable, WORKER_FILE, str(int(CROSS_PAGE_OVERLAP_CHARS))],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.err)
        self.buf = b""
        lf = _RUN_LOG[0]
        if lf is not None:     # S3: Messung zum Start von Unterprozessen
            try:
                lf.write(f"[{datetime.now().strftime('%H:%M:%S')}] Reader process started "
                         f"(subprocess start {time.monotonic() - t0:.2f} s)\n")
            except Exception:
                pass

    def _kill(self):
        if self.proc is not None:
            try:
                self.proc.kill()
                self.proc.wait(timeout=10)
            except Exception:
                pass
        self.proc = None

    def close(self):
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.stdin.close()
                self.proc.wait(timeout=10)
            except Exception:
                self._kill()
        self.proc = None
        if self.err is not None:
            try:
                self.err.close()
            except Exception:
                pass
            self.err = None

    def _stderr_tail(self):
        try:
            self.err.seek(0, os.SEEK_END)
            size = self.err.tell()
            self.err.seek(max(0, size - 4000))
            err = self.err.read().decode("utf-8", "replace").strip().splitlines()
            return " | ".join(err[-3:])[:300]
        except Exception:
            return ""

    def _read_line(self, deadline):
        """Naechste Protokollzeile (START oder ERG ...). Andere Zeilen auf dem
        Kanal werden ueberlesen. None = Zeitlimit, b"" = Prozess beendet."""
        while True:
            line = self._read_raw_line(deadline)
            if line is None or line == b"" or line == b"START" or line.startswith(b"ERG "):
                return line

    def _read_raw_line(self, deadline):
        fd = self.proc.stdout.fileno()
        while b"\n" not in self.buf:
            if INDEX_CANCEL.is_set():
                self._kill()
                raise IndexCancelled()
            rest = deadline - time.monotonic()
            if rest <= 0:
                return None
            r, _, _ = select.select([fd], [], [], min(rest, 0.5))
            if not r:
                continue
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                return b""
            self.buf += chunk
        line, _, self.buf = self.buf.partition(b"\n")
        return line

    def extract(self, full_path: str) -> list:
        if self.proc is None or self.proc.poll() is not None:
            self._start()
        # Pfad als JSON: ein Zeilenumbruch im Dateinamen wuerde sonst das
        # Protokoll zerreissen und Ergebnisse den falschen Dateien zuordnen.
        msg = (json.dumps(full_path, ensure_ascii=True) + "\n").encode("ascii")
        try:
            self.proc.stdin.write(msg)
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError):
            self._kill()
            self._start()
            self.proc.stdin.write(msg)
            self.proc.stdin.flush()
        # "START" (beim ersten Mal nach dem Laden von PyMuPDF)
        line = self._read_line(time.monotonic() + 120)
        if line != b"START":
            msg = self._stderr_tail() if line == b"" else "worker process does not respond"
            self._kill()
            raise ExtractCrashed("absturz", f"Worker process ended when starting the file. {msg}".strip())
        line = self._read_line(time.monotonic() + PDF_TIME_LIMIT_S)
        if line is None:
            self._kill()
            raise ExtractCrashed("zeitlimit", f"stopped after {PDF_TIME_LIMIT_S:.0f} s")
        if line == b"" or not line.startswith(b"ERG "):
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._kill()
                raise ExtractCrashed("absturz", "Worker process no longer responds")
            code = self.proc.returncode
            tail = self._stderr_tail()
            self.proc = None
            raise ExtractCrashed("absturz", f"PyMuPDF ended (exit code {code}). {tail}".strip())
        data = json.loads(line[4:].decode("utf-8"))
        if not data["ok"]:
            raise ValueError(data["msg"])
        return data["chunks"]


EMBED_BATCH_SIZE = int(CONFIG.get("embed_batch_size", 32))
# B5a: kurze Pause nach jedem Einbettungsstapel, damit der Mac bedienbar bleibt
# (verlaengert den Lauf entsprechend). 0 = keine Pause.
EMBED_PAUSE_S = max(0.0, float(CONFIG.get("embed_pause_ms", 0)) / 1000.0)
# S5: alle N Stapel den GPU-Vorrat leeren (0 = nie). Bei Stapeln zu 32 Chunks
# sind 50 Stapel 1.600 Chunks, also gut alle 2,5 Minuten.
GPU_LEEREN_ALLE = max(0, int(CONFIG.get("gpu_cache_clear_every_batches", 50)))
# B5a: Indexlauf in eigenem Prozess (gibt seinen Speicher am Ende vollstaendig
# zurueck, teilt das Modell nicht mit Suchanfragen), mit niedriger Prioritaet.
INDEX_SUBPROCESS = bool(CONFIG.get("index_run_own_process", True))
INDEX_NICE = int(CONFIG.get("index_run_nice", 10))
INDEX_LOCK_FILE = os.path.join(APP_DIR, "indexlauf.lock")
# VAI 1.5: Nachindizieren kann auf die ersten N geaenderten Dateien begrenzt
# werden (None oder 0 = unbegrenzt). Gilt NICHT fuer den Komplettaufbau: Dort
# wuerden nicht verarbeitete Dateien als geloescht behandelt.
MAX_FILES_PER_INCREMENTAL_RUN = int(CONFIG.get("max_files_per_incremental_run") or 0) or None
EMBED_LOG_INTERVAL_S = 30


def embed_with_progress(texts: list, log) -> np.ndarray:
    """VAI 1.3: Einbettung in Stapeln mit Fortschritt, Durchsatz und Restzeit.
    Das Ergebnis ist dasselbe wie ein einziger embed_passages-Aufruf (die
    Vektoren je Text haengen nicht vom Stapel ab, bis auf Rundungsrauschen)."""
    total = len(texts)
    with STATUS_LOCK:
        INDEX_PROGRESS.update(embed_done=0, embed_total=total, embed_rate=None, embed_eta=None)
    parts = []
    done = 0
    t_start = time.monotonic()
    t_rate0 = None      # Zeitpunkt nach dem ersten Stapel (Modellladen zaehlt nicht)
    done_rate0 = 0
    last_log = t_start
    for i in range(0, total, EMBED_BATCH_SIZE):
        _check_cancel()
        batch = texts[i:i + EMBED_BATCH_SIZE]
        parts.append(embedder.embed_passages(batch))
        done += len(batch)
        stapel = i // EMBED_BATCH_SIZE + 1
        if GPU_LEEREN_ALLE and stapel % GPU_LEEREN_ALLE == 0 and done < total:
            embedder.gpu_vorrat_leeren()
        now = time.monotonic()
        if t_rate0 is None:
            t_rate0, done_rate0 = now, done
            rate = eta = None
        else:
            span = now - t_rate0
            rate = (done - done_rate0) / span if span > 0 else None
            eta = (total - done) / rate if rate else None
        with STATUS_LOCK:
            INDEX_PROGRESS.update(embed_done=done, embed_rate=rate, embed_eta=eta)
        if EMBED_PAUSE_S and done < total:
            INDEX_CANCEL.wait(EMBED_PAUSE_S)   # unterbrechbar durch Abbruch
        if now - last_log >= EMBED_LOG_INTERVAL_S or done == total:
            _schlaf_melden(log)
            akt = _aktuell_gb()
            log.write(f"[{datetime.now().strftime('%H:%M:%S')}] [EMBED] {done}/{total}"
                      + (f", {rate:.1f} chunks/s, about {eta:.0f} s left" if rate else "")
                      + f", {now - t_start:.0f} s since start"
                      + (f", memory {akt:.2f} GB" if akt is not None else "") + "\n")
            try:
                log.flush()
            except Exception:
                pass
            last_log = now
    return np.vstack(parts).astype(np.float32, copy=False)


# --- O2: OCR-Fassungen beim Indizieren -------------------------------------
# Hat eine Datei eine gueltige OCR-Fassung im Schattenordner (Zustand
# "erfasst", Original seitdem unveraendert, Fassung vorhanden), liest der
# Indexlauf den Text aus der Fassung; die Chunks gehoeren aber zum Original
# (gleicher Pfad, gleiche Seitenzahlen - ocrmypdf aendert die Seiten nicht).
# Welche Fassung (mtime) zuletzt eingebettet wurde, steht im Zustand unter
# "ocr_quellen"; aendert sich das, wird die Datei neu gelesen.

def ocr_quellen_aktuell(con, current_scan: dict) -> dict:
    """{rel: (pfad_der_fassung, mtime_der_fassung)} fuer gueltige OCR-Fassungen."""
    try:
        zeilen = con.execute("SELECT path, mtime FROM ocr_dateien WHERE status='erfasst'").fetchall()
    except Exception:
        return {}
    out = {}
    for rel, mt in zeilen:
        if rel not in current_scan or current_scan[rel] != mt:
            continue                     # Original fehlt oder wurde seit der OCR geaendert
        fassung = os.path.join(OCR_ORDNER, rel)
        try:
            out[rel] = (fassung, os.path.getmtime(fassung))
        except OSError:
            continue
    return out


def ocr_wechsel(current_scan: dict, aktuell: dict, state: dict) -> set:
    """Dateien, deren Textquelle sich seit dem letzten Lauf geaendert hat
    (neue oder erneuerte OCR-Fassung, oder Fassung weggefallen)."""
    alt = state.get("ocr_quellen", {})
    neu = {r for r, (_, m) in aktuell.items() if alt.get(r) != m}
    weg = {r for r in alt if r not in aktuell and r in current_scan}
    return neu | weg


def _extract_files(paths_to_process: list, log, ocr_quellen: dict | None = None):
    """Liest die PDFs nacheinander (im Arbeitsprozess, B5b) und gibt
    ({Pfad: Chunks}, {Pfad: Problembefund}) zurueck. Gemeinsam fuer beide Speicher."""
    total = len(paths_to_process)
    current_chunks_by_path = {}
    extractor = IsolatedExtractor() if PDF_ISOLATED else None
    problems_found = {}     # rel_path -> {"befund", "meldung"}
    try:
      for i, rel_path in enumerate(paths_to_process, start=1):
        _check_cancel()
        full_path = Path(PDF_BASE_DIR) / rel_path
        quelle = ""
        if ocr_quellen and rel_path in ocr_quellen:
            full_path = Path(ocr_quellen[rel_path][0])      # Text aus der OCR-Fassung
            quelle = " (OCR)"
        t_file = time.monotonic()
        try:
            if extractor is not None:
                page_chunks = extractor.extract(str(full_path))
            else:
                page_chunks = indexer.extract_page_chunks(full_path, CROSS_PAGE_OVERLAP_CHARS)
        except ExtractCrashed as e:
            log.write(f"[{BEFUND_EN.get(e.befund, e.befund).upper()}] {rel_path}: {e.meldung}\n")
            problems_found[rel_path] = {"befund": e.befund, "meldung": e.meldung}
            page_chunks = []
        except IndexCancelled:
            raise
        except Exception as e:
            log.write(f"[READ ERROR] {rel_path}: {e}\n")
            problems_found[rel_path] = {"befund": "fehler", "meldung": str(e)[:500]}
            page_chunks = []
        file_seconds = time.monotonic() - t_file

        current_chunks_by_path[rel_path] = page_chunks
        if i == 1:
            _maybe_inject("extract")

        # VAI 1.5: eine Zeile je PDF (Chunks, Dauer) statt einer je Chunk.
        with STATUS_LOCK:
            INDEX_PROGRESS.update(current=i, current_chunk=len(page_chunks))
        log.write(f"[{datetime.now().strftime('%H:%M:%S')}] ({i}/{total}) {rel_path}{quelle}: "
                  f"{len(page_chunks)} chunks, {file_seconds:.1f} s\n")
        log.flush()
    finally:
        if extractor is not None:
            extractor.close()
    if problems_found:
        log.write(f"\nProblem files in this run: {len(problems_found)} "
                  f"(crash/time limit/read error; see the status page)\n")
    return current_chunks_by_path, problems_found


def _zustand_nach_lauf(state, mode, current_scan, deferred, last_known,
                       paths_to_process, problems_found, next_id) -> dict:
    """Zustand nach einem erfolgreichen Lauf (Zeitpunkte, Dateiprotokoll,
    next_chunk_id, Problemdateien). Gemeinsam fuer beide Speicher."""
    state = dict(state)
    now_readable = datetime.now().strftime("%Y-%m-%d %H:%M")
    if mode == "full":
        state["last_full_index_at"] = now_readable
        state["full_snapshot"] = current_scan
    else:
        state["last_incremental_index_at"] = now_readable
    # Zurueckgestellte Dateien (Begrenzung) gelten NICHT als erledigt:
    # bekannte behalten ihren alten Stand, neue fehlen im Protokoll.
    snapshot_after = {}
    for p_, sig_ in current_scan.items():
        if p_ in deferred:
            if p_ in last_known:
                snapshot_after[p_] = last_known[p_]
        else:
            snapshot_after[p_] = sig_
    state["last_run_snapshot"] = snapshot_after
    state["next_chunk_id"] = next_id
    # B5b: Problemdateien. Verarbeitete Dateien bekommen ihren neuen Befund
    # (oder verlieren ihn, wenn sie jetzt lesbar waren); Dateien, die es
    # nicht mehr gibt oder die ausgeschlossen sind, fallen heraus. Nicht
    # erneut verarbeitete behalten ihren Befund (sie werden erst wieder
    # versucht, wenn sich die Datei aendert).
    old_problems = state.get("problemdateien", {})
    processed_now = set(paths_to_process)
    new_problems = {}
    for p_, info in old_problems.items():
        if p_ in current_scan and p_ not in processed_now:
            new_problems[p_] = info
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    for p_, info in problems_found.items():
        new_problems[p_] = dict(info, zeit=stamp)
    state["problemdateien"] = new_problems

    return state



def run_indexing(mode: str) -> None:
    """S3 (PLAN_SQLITE.md): Indexlauf gegen see_also.db. Liest NICHT den ganzen
    Bestand ein (frueher meta.json komplett, rund 10 GB im Speicher), sondern
    nur die IDs der Dateien, die er bearbeitet oder entfernt, und schreibt am
    Ende nur seine Aenderungen, in einer einzigen Transaktion. Bis dahin ist
    nichts veraendert (Abbruch, Fehler, Absturz: alter Stand bleibt)."""
    _set_phase("scan")
    scan_started = datetime.now()   # Log ist noch nicht offen; Zeile wird nachgetragen
    if not os.path.exists(DB_FILE):
        if mode != "full":
            raise RuntimeError(DB_FEHLT_MELDUNG + " Nachindizieren setzt einen vorhandenen Index voraus.")
        speicher.anlegen(DB_FILE).close()      # erster Komplettlauf: leere Datenbank anlegen
    con = speicher.verbinden(DB_FILE)          # schreibend, eigene Verbindung des Laufs
    try:
        state = speicher.zustand_lesen(con)
        state.setdefault("next_chunk_id", 0)
        db_pfade = speicher.pfade_mit_chunks(con)
        n_vorher = speicher.anzahl_chunks(con)
        current_scan = scan_pdfs(PDF_BASE_DIR)

        if not os.path.isdir(PDF_BASE_DIR):
            raise RuntimeError(f"Collection folder not found: {PDF_BASE_DIR} "
                               "(check pdf_base_dir in config.json). The index was not changed.")
        if not current_scan and (n_vorher or mode == "full"):
            raise RuntimeError(f"No PDFs found in {PDF_BASE_DIR}, but the index holds {n_vorher} passages. "
                               "Check pdf_base_dir in config.json (is the drive connected?). The index was not changed.")

        last_known = state.get("last_run_snapshot", {})
        ocr_aktuell = ocr_quellen_aktuell(con, current_scan)
        if mode == "full":
            paths_to_process = sorted(current_scan.keys())
        else:
            paths_to_process = sorted(set(p for p, mtime in current_scan.items()
                                          if p not in last_known or last_known[p] != mtime)
                                      | ocr_wechsel(current_scan, ocr_aktuell, state))
        deferred = set()
        if (mode == "incremental" and MAX_FILES_PER_INCREMENTAL_RUN
                and len(paths_to_process) > MAX_FILES_PER_INCREMENTAL_RUN):
            deferred = set(paths_to_process[MAX_FILES_PER_INCREMENTAL_RUN:])
            paths_to_process = paths_to_process[:MAX_FILES_PER_INCREMENTAL_RUN]
        # Verschwundene Dateien (geloescht, umbenannt, Bereich ausgeschlossen):
        # ihre Chunks werden entfernt.
        verschwunden = sorted(p for p in db_pfade if p not in current_scan)

        total = len(paths_to_process)
        timestamp_for_log = datetime.now().strftime("%Y%m%d-%H%M%S")
        log_path = os.path.join(LOG_DIR, f"index_{mode}_{timestamp_for_log}.log")
        with STATUS_LOCK:
            INDEX_PROGRESS.update(is_running=True, mode=mode, current=0, total=total, current_chunk=0,
                                  phase="extract", error=None, log_file=os.path.basename(log_path))

        with open(log_path, "w", encoding="utf-8") as log:
          _RUN_LOG[0] = log
          _uhren_start()
          try:
            log.write(f"Start {mode} indexing: {scan_started.isoformat()}\n")
            log.write("Storage: SQLite (see_also.db)\n")
            if IS_INDEX_CHILD:
                try:
                    log.write(f"Priority: nice {os.nice(0)}\n")   # G3d: tatsaechlich wirksamer Wert
                except OSError:
                    pass
            _wachhalten_start(log)
            log.write("Clear GPU cache: " + (f"every {GPU_LEEREN_ALLE} batches" if GPU_LEEREN_ALLE else "off") + "\n")
            log.write(f"[{scan_started.strftime('%H:%M:%S')}] Phase: scan\n")
            _log_phase("extract")
            log.write(f"PDFs to process: {total}, vanished files: {len(verschwunden)}\n")
            if deferred:
                log.write(f"Limited to {MAX_FILES_PER_INCREMENTAL_RUN} files (max_files_per_incremental_run): "
                          f"{len(deferred)} more changed files are left for the next run.\n")
            log.write("\n")

            n_ocr = sum(1 for p_ in paths_to_process if p_ in ocr_aktuell)
            if n_ocr:
                log.write(f"{n_ocr} of them with text from the OCR version ({_ocr.anzeige_pfad(OCR_ORDNER)})\n")
            current_chunks_by_path, problems_found = _extract_files(paths_to_process, log, ocr_aktuell)

            _set_phase("merge")
            alt = speicher.ids_nach_pfad(con, list(current_chunks_by_path) + verschwunden)
            new_meta_processed, next_id, id_log_lines = indexer.assign_ids_and_merge(
                current_chunks_by_path, alt, state["next_chunk_id"], unchanged_paths=set())
            for line in id_log_lines:
                log.write(f"{line}\n")

            _set_phase("embed")
            log.write(f"\nEmbedding {len(new_meta_processed)} new chunks, "
                      f"{n_vorher - len(alt)} chunks reused (left untouched in the database)\n")
            _maybe_inject("embed")
            if new_meta_processed:
                vektoren = embed_with_progress([e["text_for_embedding"] for e in new_meta_processed], log)
            else:
                vektoren = np.zeros((0, embedder.EMBEDDING_DIM), dtype=np.float32)

            _enter_save_phase()
            _maybe_inject("save")
            alte_quellen = dict(state.get("ocr_quellen", {}))
            state = _zustand_nach_lauf(state, mode, current_scan, deferred, last_known,
                                       paths_to_process, problems_found, next_id)
            # O2: eingebettete OCR-Fassungen merken (nur fuer verarbeitete Dateien
            # aendern; zurueckgestellte behalten ihren alten Eintrag).
            for p_ in paths_to_process:
                if p_ in deferred:
                    continue
                if p_ in ocr_aktuell and not problems_found.get(p_):
                    alte_quellen[p_] = ocr_aktuell[p_][1]
                else:
                    alte_quellen.pop(p_, None)
            state["ocr_quellen"] = {k: v for k, v in alte_quellen.items() if k in current_scan}

            def vor_commit():
                _maybe_inject("swap")
                _maybe_inject("swap_kill")

            t0 = time.monotonic()
            erg = speicher.lauf_festschreiben(con, set(current_chunks_by_path) | set(verschwunden),
                                              new_meta_processed, vektoren, state, vor_commit=vor_commit)
            log.write(f"Committed to see_also.db ({time.monotonic() - t0:.1f} s): "
                      f"{erg['entfernt']} chunks removed, {erg['eingefuegt']} added, "
                      f"generation {erg['generation']}\n")
            try:
                t0 = time.monotonic()
                busy, wal_seiten, kopiert = con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                log.write(f"WAL file checkpointed ({time.monotonic() - t0:.1f} s)"
                          + (" - partly, a reader was active" if busy else "") + "\n")
            except Exception as e:           # nur Aufraeumen; der Lauf ist schon festgeschrieben
                log.write(f"WAL checkpoint skipped: {e}\n")
            log.write(f"End: {datetime.now().isoformat()}\n")
            log.write(f"Total chunks: {erg['chunks_gesamt']}\n")
            log.write(f"Memory peak of the run: {_spitze_gb():.2f} GB\n")
            log.write(f"Asleep during the run: {_geschlafen_s() / 60:.0f} min\n")
          finally:
            _wachhalten_ende()
            _RUN_LOG[0] = None
    finally:
        con.close()

    with STATUS_LOCK:
        INDEX_PROGRESS.update(is_running=False, phase="done")
        bestand_verwerfen()   # P1: Bestand nach dem Lauf neu einlesen


def find_missing_files() -> list[str]:
    # Dateien im Bestand, die aktuell keinen einzigen Chunk in meta.json
    # haben - typischerweise weil keine lesbare Textebene vorhanden ist
    # (siehe README, Abschnitt "Geplante Erweiterung: See-Also-III").
    # Bewusst NUR komplett fehlende Dateien, nicht "wenige Chunks" - siehe
    # Begruendung dort (sich selbst aufloesender Zustand vs. Stempel-Bedarf).
    current_scan = bestand()
    paths_with_chunks = get_index().doc_index
    state = load_index_state()
    problems = state.get("problemdateien", {})
    # G1c: nur Dateien, die ein Indexlauf in diesem Stand gelesen und ohne
    # Text gefunden hat. Neue oder geaenderte Dateien zaehlen bis zum naechsten
    # Lauf unter "Waiting for the next update", nicht als "need OCR".
    gelesen = state.get("last_run_snapshot", {})
    # B5b: Problemdateien stehen in einem eigenen Block, nicht hier (sie sind
    # keine Kandidaten fuer OCR).
    return sorted(p for p, mtime in current_scan.items()
                  if p not in paths_with_chunks and p not in problems and gelesen.get(p) == mtime)


# G1b: Meldungen frueherer Laeufe stehen deutsch im Zustand; fuer die Anzeige uebersetzen.
_MELDUNG_DE_EN = [
    (re.compile(r"^Arbeitsprozess beendet beim Start der Datei\.?"), "Worker process ended when starting the file."),
    (re.compile(r"Arbeitsprozess antwortet nicht mehr"), "Worker process no longer responds"),
    (re.compile(r"Arbeitsprozess antwortet nicht"), "worker process does not respond"),
    (re.compile(r"^nach (\d+) s abgebrochen"), r"stopped after \1 s"),
    (re.compile(r"^PyMuPDF beendet \(Rueckgabewert"), "PyMuPDF ended (exit code"),
]


def meldung_en(text: str | None) -> str:
    text = text or ""
    for muster, ersatz in _MELDUNG_DE_EN:
        text = muster.sub(ersatz, text)
    return text


def find_problem_files() -> list[dict]:
    current_scan = bestand()
    problems = load_index_state().get("problemdateien", {})
    order = {"absturz": 0, "zeitlimit": 1, "fehler": 2}
    rows = [dict(path=p, **dict(info, meldung=meldung_en(info.get("meldung"))))
            for p, info in problems.items() if p in current_scan]
    return sorted(rows, key=lambda r: (order.get(r.get("befund"), 9), r["path"]))


def build_status() -> dict:
    current_scan = bestand()
    state = load_index_state()
    drift = compute_drift_metrics(current_scan, state)
    fehlend = find_missing_files()
    ocr_einzubetten = 0
    if os.path.exists(DB_FILE):
        with _db_zugriff() as con:
            ocr_einzubetten = len(ocr_wechsel(current_scan, ocr_quellen_aktuell(con, current_scan), state))
    with STATUS_LOCK:
        progress = dict(INDEX_PROGRESS)

    return {
        "index_error": progress.get("error"),
        "index_log_file": progress.get("log_file"),
        "index_cancelled": progress.get("phase") == "cancelled" and not progress.get("is_running"),
        "startup_notices": [n_ for n_ in STARTUP_NOTICES
                            if not (n_ == DB_FEHLT_MELDUNG and os.path.exists(DB_FILE))],
        "datum": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "prozess_gestartet": PROCESS_STARTED_AT,
        "anzahl_pdfs": len(current_scan),
        "anzahl_pdfs_remasterd": 0,      # P1: frueher eigener Gang ueber den Baum; seit O1 nicht angezeigt
        # 0 Chunks ist ein legitimer Wert und von "noch nie indiziert" (keine
        # Datenbank) zu unterscheiden.
        "anzahl_chunks": get_index().n_visible if os.path.exists(DB_FILE) else None,
        "alte_json_dateien": [f for f in ALTE_JSON_DATEIEN if os.path.exists(os.path.join(APP_DIR, f))],
        "ausgeschlossen": sorted(EXCLUDED_AREAS),
        "missing_files": fehlend,
        "ocr": _ocr_mit_zahlen(fehlend),
        "ocr_ordner": OCR_ORDNER,
        "problem_files": find_problem_files(),
        "last_full_index_at": state.get("last_full_index_at"),
        "last_incremental_index_at": state.get("last_incremental_index_at"),
        "full_drift_pct": drift["full_drift_pct"],
        "neu_seit_letzter_indizierung": drift["neu_seit_letzter_indizierung"],
        "ocr_einzubetten": ocr_einzubetten,
        "entfernt_seit_letzter_indizierung": drift["entfernt_seit_letzter_indizierung"],
    }


@app.route("/")
def index():
    # Neue, leichtgewichtige Einstiegsseite - Terminal-artiger Chunk-Viewer.
    # Status-Details und Indizierungs-Steuerung sind jetzt unter /status.
    return render_template("index.html", phrase_max_chars=PHRASE_MAX_CHARS)


@app.route("/reveal-in-finder", methods=["POST"])
def reveal_in_finder():
    # macOS-spezifisch (open -R) - bindet diese eine Komfortfunktion an
    # das Betriebssystem, bewusst in Kauf genommen. Dieselbe
    # Pfad-Absicherung wie bei /pdf/<filename>, da hier ebenfalls ein
    # extern beeinflussbarer Pfad in einen Subprozess-Aufruf einfliesst.
    doc_path = request.args.get("path")
    if not doc_path:
        abort(400)
    base = os.path.realpath(PDF_BASE_DIR)
    target = os.path.realpath(os.path.join(base, doc_path))
    if not target.startswith(base + os.sep):
        abort(403)
    return jsonify(**finder_zeigen(target))


# O1d: Statt "open -R" (oeffnet jedes Mal ein neues Fenster) das vorderste
# Finder-Fenster in den Ordner wechseln lassen und die Datei markieren. Ohne
# offenes Fenster: reveal (oeffnet eines). Beim ersten Mal fragt macOS, ob
# Python den Finder steuern darf (Datenschutz & Sicherheit > Automation).
# Scheitert AppleScript (z. B. Erlaubnis verweigert), bleibt "open -R".
_FINDER_SKRIPT = [
    "on run argv",
    "set d to POSIX file (item 1 of argv) as alias",
    "set o to POSIX file (item 2 of argv) as alias",
    'tell application "Finder"',
    "activate",
    "if (count of Finder windows) > 0 then",
    "set target of Finder window 1 to o",
    "select d",
    "else",
    "reveal d",
    "end if",
    "end tell",
    "end run",
]


def finder_zeigen(target: str) -> dict:
    cmd = ["osascript"]
    for zeile in _FINDER_SKRIPT:
        cmd += ["-e", zeile]
    cmd += [target, os.path.dirname(target)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if r.returncode == 0:
            return dict(ok=True, weg="applescript")
        fehler = (r.stderr or "").strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        fehler = str(e)
    try:
        subprocess.run(["open", "-R", target], check=True)
        return dict(ok=True, weg="open -R", hinweis=fehler[:300])
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        return dict(ok=False, error=f"{fehler[:200]} / {e}")


# --- Seite "Problemfaelle" (18.11.2026) -----------------------------------
# Sammelt alle Dateien, bei denen der Nutzer entscheiden muss (loeschen,
# ersetzen, lassen): OCR ohne Ergebnis, Problemdateien des Indexlaufs und
# abgeschnittene PDFs mit Chunks (aus tools/pdf_dateiende.py). Jede Zeile
# hat "Finder" (open -R, Datei markiert) und "Oeffnen". Nur lesend; geprueft
# wird gegen den aktuellen Bestand, Erledigtes faellt beim Neuladen heraus.
PROBLEM_GRUPPEN = [
    # (key, heading, explanation and typical decision) - G1: English
    ("abgeschnitten", "Truncated, no pages",
     "The end of the file is missing (for example an interrupted copy); no program finds any pages. "
     "The content is lost. Delete it, or restore it from a backup or the original source."),
    ("ausgenullt", "Zeroed",
     "The file consists mostly of zero bytes. The content is lost. Delete it or restore it from a backup."),
    ("leer", "Empty (0 bytes)", "No content at all. Delete it."),
    ("kein_pdf", "Not a PDF",
     "Only the name ends in .pdf (companion files, system files, text files). Usually delete."),
    ("unlesbar", "Unreadable", "The PDF cannot be opened. Check it in a viewer."),
    ("fehler", "OCR error", "The message is shown in the row. Often damaged images or unusual formats."),
    ("zeitlimit", "OCR time limit", "Very large or difficult scans. Leave them, or raise the time limit."),
    ("absturz", "OCR crash", "The OCR run ended twice while working on this file. Check it in a viewer."),
    ("verschluesselt", "Password protected",
     "Without the password there is neither text nor OCR. Unlock and save it again, delete it, or leave it."),
    ("index_problem", "Could not be read while indexing",
     "Reading crashed, timed out or failed. Check whether the file opens in a viewer."),
    ("index_teilweise", "Truncated, but indexed",
     "No end-of-file marker, but pages and passages exist: searchable, only the end is missing. "
     "Replace it if a complete copy is available; otherwise leave it. "
     "Source: last run of tools/pdf_dateiende.py."),
]
BEFUND_EN = {"absturz": "crash", "zeitlimit": "time limit", "fehler": "read error"}
# Ziel fuer "Befehl kopieren": mv statt rm, damit ein Fehlgriff umkehrbar bleibt.
PAPIERKORB = CONFIG.get("trash") or "~/.Trash/"


def _dateiende_liste() -> str | None:
    """Liste von tools/pdf_dateiende.py (G1b: pdf_missing_eof.txt, vorher
    pdf_ohne_dateiende.txt). None, wenn es keine gibt."""
    for name in ("pdf_missing_eof.txt", "pdf_ohne_dateiende.txt"):
        p = os.path.join(LOG_DIR, name)
        if os.path.exists(p):
            return p
    return None


def _teilweise_abgeschnitten(bestand: dict) -> list[dict]:
    """Liest logs/pdf_ohne_dateiende.txt und prueft jede Zeile frisch nach:
    nur Dateien im Bestand, mit Chunks, weiterhin ohne Dateiende."""
    liste = _dateiende_liste()
    if not liste:
        return []
    pfade = []
    with open(liste, encoding="utf-8") as f:
        for z in f:
            if z.startswith("#") or not z.strip():
                continue
            teile = z.rstrip("\n").split("\t")
            if len(teile) == 3 and teile[2] in bestand:
                pfade.append(teile[2])
    zeilen = []
    if not pfade:
        return zeilen
    with _db_zugriff() as con:
        anzahl = {}
        for i in range(0, len(pfade), 500):
            stueck = pfade[i:i + 500]
            anzahl.update(con.execute(
                f"SELECT path, COUNT(*) FROM chunks WHERE path IN ({','.join('?' * len(stueck))}) GROUP BY path",
                stueck).fetchall())
    for p in pfade:
        if not anzahl.get(p):
            continue
        voll = os.path.join(PDF_BASE_DIR, p)
        try:
            g = os.path.getsize(voll)
            with open(voll, "rb") as h:
                h.seek(max(0, g - 16384))
                if b"%%EOF" in h.read():
                    continue        # inzwischen ersetzt
        except OSError:
            continue
        zeilen.append(dict(path=p, detail=f"{anzahl[p]} passages in the index"))
    return zeilen


def ocr_detail(status: str, grund: str | None) -> str:
    """Grund aus ocr_dateien fuer die Anzeige: Standardtexte (deutsch aus
    frueheren Laeufen oder englisch) nicht wiederholen, die Gruppe sagt es schon."""
    import ocr as _ocr
    if not grund or grund in _ocr.STANDARD_GRUENDE:
        return ""
    return _ocr.grund_en(grund)


def problemfaelle_daten() -> dict:
    import ocr as _ocr
    im_bestand = bestand()
    gruppen = {k: [] for k, _, _ in PROBLEM_GRUPPEN}
    ohne_text = []
    if os.path.exists(DB_FILE):
        with _db_zugriff() as con:
            je_pfad = _ocr.zustand_je_pfad(con)
        for p, (st, grund) in je_pfad.items():
            # Zwischengespeicherter Bestand plus Einzelpruefung: Was eben in den
            # Papierkorb verschoben wurde, faellt beim Neuladen sofort heraus.
            if p not in im_bestand or not os.path.exists(os.path.join(PDF_BASE_DIR, p)):
                continue
            if st == "kein_text":
                ohne_text.append(p)
            elif st in gruppen:
                gruppen[st].append(dict(path=p, detail=ocr_detail(st, grund)))
        gruppen["index_teilweise"] = _teilweise_abgeschnitten(im_bestand)
    for p, info in load_index_state().get("problemdateien", {}).items():
        if p in im_bestand and os.path.exists(os.path.join(PDF_BASE_DIR, p)):
            gruppen["index_problem"].append(dict(
                path=p, detail=f"{BEFUND_EN.get(info.get('befund', ''), info.get('befund', ''))}: "
                               f"{meldung_en(info.get('meldung'))}".strip(": ")))
    for zeilen in gruppen.values():
        for z in zeilen:
            try:
                z["groesse"] = os.path.getsize(os.path.join(PDF_BASE_DIR, z["path"]))
            except OSError:
                z["groesse"] = None
        zeilen.sort(key=lambda z: z["path"])
    liste = [dict(key=k, titel=t, erklaerung=e, zeilen=gruppen[k]) for k, t, e in PROBLEM_GRUPPEN if gruppen[k]]
    return dict(gruppen=liste, gesamt=sum(len(g["zeilen"]) for g in liste),
                dateiende_liste=_dateiende_liste() is not None,
                ohne_text=sorted(ohne_text), base=PDF_BASE_DIR, papierkorb=PAPIERKORB)


def ohne_text_daten() -> dict:
    """OCR ohne Text, nach Bereichen (erste Ordnerebene)."""
    d = problemfaelle_daten()
    bereiche = {}
    for p in d["ohne_text"]:
        try:
            g = os.path.getsize(os.path.join(PDF_BASE_DIR, p))
        except OSError:
            g = None
        bereiche.setdefault(area_of(p) or ROOT_AREA_LABEL, []).append(dict(path=p, groesse=g, detail=""))
    liste = [dict(name=k, zeilen=v) for k, v in sorted(bereiche.items(), key=lambda kv: kv[0].lower())]
    return dict(bereiche=liste, gesamt=len(d["ohne_text"]), zu_entscheiden=d["gesamt"])


@app.route("/problemfaelle")
def problemfaelle_seite():
    return render_template("problemfaelle.html", d=problemfaelle_daten())


@app.route("/ohne-text")
def ohne_text_seite():
    return render_template("ohne_text.html", d=ohne_text_daten())


@app.route("/status")
def status_page():
    return render_template("status.html", status=build_status())


@app.route("/status/panel")
def status_panel():
    return render_template("_status_block.html", status=build_status())


@app.route("/index/badge")
def index_badge():
    # Fuer das Header-Badge nach Abschluss eines Laufs - liefert den
    # signierten Netto-Wert (neu/geaendert minus entfernt seit letzter
    # Indizierung), unabhaengig davon, ob der Lauf inkrementell oder
    # komplett war und egal von welcher Seite aus er gestartet wurde.
    current_scan = bestand()
    state = load_index_state()
    drift = compute_drift_metrics(current_scan, state)
    return jsonify(neu=drift["neu_minus_entfernt"])


@app.route("/documents")
def documents():
    # Liste aller Dokumente, die aktuell mindestens einen Chunk im Index
    # haben - noch OHNE Aehnlichkeits-Ranking (kommt erst mit Block D).
    # Alphabetisch sortiert, kein "best match".
    idx = get_index()
    docs = [{"path": p, "chunk_count": int(idx.doc_counts[k])}
            for k, p in enumerate(idx.doc_paths) if not idx.doc_excluded[k]]
    return jsonify(docs)


@app.route("/documents/chunks")
def document_chunks():
    # Pfad als Query-Parameter, nicht als URL-Pfadsegment - vermeidet
    # jede Mehrdeutigkeit bei Unterordnern/Sonderzeichen im Dateipfad.
    doc_path = request.args.get("path")
    if not doc_path:
        abort(400)
    if is_excluded(doc_path):
        abort(404)
    entries = _doc_chunk_entries(get_index(), doc_path)

    with SEARCH_LOCK:
        scores = dict(LAST_SEARCH_SCORES)

    if scores:
        # Aktive Suche vorhanden: nach Score sortieren (bester zuerst).
        # Chunks ohne Score (sollte nicht vorkommen, da eine Suche ueber
        # den gesamten Index laeuft) fallen ans Ende.
        entries.sort(key=lambda e: scores.get(e["id"], float("-inf")), reverse=True)
    else:
        entries.sort(key=lambda e: e["position_in_file"])

    # Begrenzung, konfigurierbar - wenn wirklich mehr als diese Anzahl
    # relevanter Treffer in einem Dokument existieren, ist ohnehin das
    # ganze PDF zu betrachten, nicht eine lange Chunk-Liste.
    entries = entries[:CHUNK_LIST_MAX_ENTRIES]

    result = []
    for e in entries:
        item = {
            "id": e["id"],
            "seite": e["seite"],
            "zeile": e["zeile"],
            "snippet": e["text"][:150].replace("\n", " "),
        }
        if scores:
            item["score"] = round(scores.get(e["id"], 0.0), 3)
        result.append(item)
    return jsonify(result)


@app.route("/search", methods=["POST"])
def search():
    data = request.get_json(silent=True) or {}
    phrase = (data.get("phrase") or "").strip()
    if not phrase:
        return jsonify(error="Enter some text to search for."), 400
    if len(phrase) > PHRASE_MAX_CHARS:
        # Server-seitige Pruefung, nicht nur das maxlength-Attribut im
        # Frontend - explizite Fehlerbehandlung statt blindem Vertrauen
        # in eine Client-seitige Beschraenkung.
        return jsonify(error=f"The text is too long (more than {PHRASE_MAX_CHARS} characters)."), 400

    documents_result, error = run_search(phrase)
    if error:
        return jsonify(error=error), 400
    with SEARCH_LOCK:
        scope = LAST_SEARCH_SCOPE[0]
    return jsonify(documents=documents_result, scope=scope)


def effective_search_scope(idx):
    """Gewaehlter Suchbereich, bereinigt um Bereiche, die es im Index nicht
    (mehr) gibt. None = alle. Bleibt nach dem Bereinigen nichts uebrig, gilt
    wieder "alle" (sonst waere die Suche stumm leer)."""
    if SEARCH_SCOPE is None:
        return None
    valid = SEARCH_SCOPE & set(idx.area_chunks)
    if not valid or valid == set(idx.area_chunks):
        return None
    return valid


def scope_summary(idx):
    scope = effective_search_scope(idx)
    if scope is None:
        return None
    return {"bereiche": sorted(scope), "von": len(idx.area_chunks),
            "chunks": sum(idx.area_chunks[a] for a in scope)}


def rank_documents(idx, query_vector):
    """Kern der Suche, ohne Seiteneffekte (S2: auch vom Vergleichswerkzeug
    benutzt). Gibt (Scores je Matrixzeile, {Pfad: bester Score} in
    Rangfolge, wirksamer Suchbereich) zurueck."""
    # Vektoren sind L2-normalisiert -> Skalarprodukt = Kosinus-Aehnlichkeit.
    scores = idx.matrix @ query_vector
    # Pro Dokument den besten Score (N1: mit NumPy statt Python-Schleife).
    best = np.full(len(idx.doc_paths), -np.inf, dtype=np.float64)
    np.maximum.at(best, idx.doc_of_row, scores.astype(np.float64))
    order = np.lexsort((idx.doc_first_row, -best))
    # B1: ausgeschlossene Bereiche fallen vor dem Ranking heraus (auch aus der
    # Liste fuer die Kontrastfunktion).
    scope = effective_search_scope(idx)
    best_per_doc = {idx.doc_paths[k]: float(best[k]) for k in order
                    if not idx.doc_excluded[k] and (scope is None or idx.doc_area[k] in scope)}
    return scores, best_per_doc, scope


def run_search(phrase: str, brief: dict | None = None):
    """Fuehrt eine Suche aus und merkt sie als "letzte Suche". Gemeinsamer
    Kern von /search (Eingabefeld) und /briefkasten (Anfrage von aussen).
    Gibt (dokumente, None) oder (None, fehlermeldung) zurueck."""
    if not os.path.exists(DB_FILE):
        return None, DB_FEHLT_MELDUNG
    idx = get_index()
    id_order = idx.id_order
    if len(id_order) == 0:
        return None, "There is no index yet. Build it on the status page first."

    query_vector = embedder.embed_query(phrase)
    scores, best_per_doc, scope = rank_documents(idx, query_vector)

    with SEARCH_LOCK:
        LAST_SEARCH_SCORES.clear()
        LAST_SEARCH_SCORES.update(zip(id_order, scores.tolist()))

    if not best_per_doc:
        return None, ("There are no documents in the selected search scope. Change it on the status page."
                      if scope is not None else "All areas of the index are excluded.")
    count_per_doc = {p: int(idx.doc_counts[k]) for k, p in enumerate(idx.doc_paths)}

    ranked = list(best_per_doc.items())[:SEARCH_MAX_RESULTS]
    mit_ocr = _ocr_eingebettet([p for p, _ in ranked])
    documents_result = [
        {"path": path, "best_score": round(score, 3), "chunk_count": count_per_doc[path],
         "ocr": path in mit_ocr}
        for path, score in ranked
    ]

    # Vollstaendige Liste (kein [:SEARCH_MAX_RESULTS]) fuer die
    # Kontrastfunktion - siehe Kommentar bei LAST_SEARCH_ALL_SCORES oben.
    all_scores_full = sorted(best_per_doc.values(), reverse=True)

    with SEARCH_LOCK:
        global LAST_SEARCH_PHRASE, LAST_SEARCH_DOCUMENTS, LAST_SEARCH_ALL_SCORES
        global LAST_SEARCH_BRIEF, SEARCH_SERIAL
        LAST_SEARCH_PHRASE = phrase
        LAST_SEARCH_DOCUMENTS = documents_result
        LAST_SEARCH_ALL_SCORES = all_scores_full
        LAST_SEARCH_BRIEF = brief
        LAST_SEARCH_SCOPE[:] = [scope_summary(idx)]
        SEARCH_SERIAL += 1

    return documents_result, None


def _clean_brief_text(text: str) -> str:
    # Zeilenenden vereinheitlichen, Leerraum am Rand weg. Der Inhalt selbst
    # bleibt unveraendert (keine Markdown-Bereinigung, keine Normalisierung).
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


@app.route("/briefkasten", methods=["POST"])
def briefkasten_einwurf():
    """Nimmt eine Anfrage von aussen an (z. B. vom Obsidian-Knopf "Anfrage an
    VAI"), fuehrt sie als Suche aus und merkt sie als letzte Suche. Zu langer
    Text wird auf phrase_max_chars gekuerzt und das wird gemeldet (das Modell
    liest ohnehin nur etwa 512 Token)."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(ok=False, error="Expected JSON with a field 'text'."), 400
    raw = data.get("text")
    if not isinstance(raw, str):
        return jsonify(ok=False, error="Field 'text' is missing."), 400
    text = _clean_brief_text(raw)
    if not text:
        return jsonify(ok=False, error="The text is empty."), 400

    original_chars = len(text)
    gekuerzt = original_chars > PHRASE_MAX_CHARS
    phrase = text[:PHRASE_MAX_CHARS].rstrip() if gekuerzt else text

    brief = {
        "zeit": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "quelle": str(data.get("quelle") or "")[:300],
        "herkunft": str(data.get("herkunft") or "")[:60],
        "art": str(data.get("art") or "")[:30],
        "zeichen": original_chars,
        "gekuerzt": gekuerzt,
        "zeichen_gesucht": len(phrase),
    }

    documents_result, error = run_search(phrase, brief=brief)
    if error:
        return jsonify(ok=False, error=error), 400

    brief["treffer"] = len(documents_result)
    brief["suchbereich"] = scope_summary(get_index())
    brief["bester_score"] = documents_result[0]["best_score"] if documents_result else None
    with SEARCH_LOCK:
        BRIEFE.insert(0, dict(brief, anfang=phrase[:120]))
        del BRIEFE[BRIEFKASTEN_MAX:]

    return jsonify(ok=True, url=request.host_url, brief=brief)


@app.route("/suchbereich", methods=["GET"])
def suchbereich_lesen():
    idx = get_index()
    scope = effective_search_scope(idx)
    areas = [{"name": a, "chunks": n, "gewaehlt": scope is None or a in scope}
             for a, n in sorted(idx.area_chunks.items(), key=lambda x: x[0].lower())]
    return jsonify(bereiche=areas, alle=scope is None, weitere=areas_without_chunks(idx))


def areas_without_chunks(idx) -> list[dict]:
    """Freigegebene Bereiche (Ordner der ersten Ebene, nicht ausgeschlossen),
    die keinen Chunk im Index haben. Nur zur Anzeige im Kasten "Suchbereich".
    hinweis: "noch nicht indiziert" = kein Lauf hat den Bereich je gesehen;
    "ohne Text" = ein Lauf hat dort PDFs gefunden, aber keine mit Textebene
    (Scans, OCR-Kandidaten); "keine PDFs" = Ordner ohne ein einziges PDF."""
    try:
        entries = [e for e in os.scandir(PDF_BASE_DIR)
                   if e.is_dir(follow_symlinks=False) and not e.name.startswith(".")]
    except OSError:
        return []
    try:
        snap = load_index_state().get("last_run_snapshot", {})
    except (OSError, ValueError):
        snap = {}
    seen = {_nfc(area_of(p)) for p in snap}
    result = []
    for e in entries:
        name = _nfc(e.name)
        if name in EXCLUDED_AREAS or name in idx.area_chunks:
            continue
        if name in seen:
            hinweis = "no text"
        elif _has_any_pdf(e.path):
            hinweis = "not indexed yet"
        else:
            hinweis = "no PDFs"
        result.append({"name": name, "hinweis": hinweis})
    return sorted(result, key=lambda x: x["name"].lower())


def _has_any_pdf(folder: str) -> bool:
    """Bricht beim ersten PDF ab; durchsucht also nur so weit wie noetig."""
    for _root, _dirs, files in os.walk(folder, followlinks=False):
        if any(f.lower().endswith(".pdf") for f in files):
            return True
    return False


@app.route("/suchbereich", methods=["POST"])
def suchbereich_setzen():
    """Body: {"bereiche": [Namen]} oder {"bereiche": null} fuer alle."""
    global SEARCH_SCOPE
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or "bereiche" not in data:
        return jsonify(ok=False, error="Expected JSON with a field 'bereiche'."), 400
    wanted = data["bereiche"]
    idx = get_index()
    if wanted is None:
        new_scope = None
    else:
        if not isinstance(wanted, list):
            return jsonify(ok=False, error="'bereiche' must be a list or null."), 400
        new_scope = {_nfc(str(a)) for a in wanted} & set(idx.area_chunks)
        if not new_scope:
            return jsonify(ok=False, error="Select at least one area."), 400
        if new_scope == set(idx.area_chunks):
            new_scope = None
    try:
        _save_search_scope(new_scope)
    except OSError as e:
        return jsonify(ok=False, error=f"Could not write suchbereich.json: {e}"), 500
    SEARCH_SCOPE = new_scope
    return jsonify(ok=True, suchbereich=scope_summary(idx))


@app.route("/briefkasten", methods=["GET"])
def briefkasten_liste():
    # Die letzten Briefe (nur seit dem Start des Servers), neuester zuerst.
    with SEARCH_LOCK:
        return jsonify(briefe=list(BRIEFE))


@app.route("/search/serial")
def search_serial():
    # Billige Abfrage (kein Plattenzugriff): Hat sich die letzte Suche
    # geaendert? Die Startseite fragt das im Sekundentakt ab.
    with SEARCH_LOCK:
        return jsonify(serial=SEARCH_SERIAL)


@app.route("/search/last")
def search_last():
    # Ermoeglicht, eine aktive Suche nach Seitenwechsel wiederherzustellen
    # (index.html -> status.html -> zurueck), statt sie stillschweigend zu
    # verlieren. Leere Phrase bedeutet "keine aktive Suche".
    with SEARCH_LOCK:
        return jsonify(phrase=LAST_SEARCH_PHRASE, documents=LAST_SEARCH_DOCUMENTS,
                       brief=LAST_SEARCH_BRIEF, serial=SEARCH_SERIAL,
                       scope=LAST_SEARCH_SCOPE[0] if LAST_SEARCH_PHRASE else None)


@app.route("/search/clear", methods=["POST"])
def search_clear():
    # Gegenstueck zu search_last(): "clear" im Frontend leert Phrase,
    # Ergebnisse und Chunks - ohne diesen Reset wuerde ein Neuladen der
    # Seite die geleerte Suche ueber /search/last sofort wiederherstellen,
    # da der Server sie ja weiterhin im Speicher haette.
    with SEARCH_LOCK:
        global LAST_SEARCH_PHRASE, LAST_SEARCH_DOCUMENTS, LAST_SEARCH_ALL_SCORES
        global LAST_SEARCH_BRIEF, SEARCH_SERIAL
        # Zaehler nur erhoehen, wenn wirklich etwas geleert wird. Die
        # Startseite ruft "clear" auch auf, wenn sie einen leeren Zustand
        # uebernimmt - ohne diese Bedingung wuerden sich Seite und Zaehler
        # gegenseitig endlos anstossen.
        if LAST_SEARCH_PHRASE or LAST_SEARCH_DOCUMENTS:
            SEARCH_SERIAL += 1
        LAST_SEARCH_PHRASE = ""
        LAST_SEARCH_DOCUMENTS = []
        LAST_SEARCH_ALL_SCORES = []
        LAST_SEARCH_BRIEF = None
    return jsonify(ok=True)


@app.route("/scores/bars", methods=["POST"])
def scores_bars():
    with SEARCH_LOCK:
        scores = list(LAST_SEARCH_ALL_SCORES)

    if not scores:
        return jsonify(error="No search yet. Search for something first."), 400

    # Die fertige HTML-Seite geht als Overlay-Inhalt ans Frontend (rechts
    # angedockt statt neuer Tab).
    # S2 (15.11.2026): Rechnung im Server selbst statt als Unterprozess.
    # Gemessen auf macOS: Das Skript allein braucht 0,1 s, der Aufruf als
    # Unterprozess aus dem Server (5,4 GB, Modell geladen) aber konstant 11 s.
    # Die genaue Ursache des langsamen Prozessstarts ist nicht geklaert; die
    # Rechnung ist reines Python ohne Seiteneffekte und laeuft hier genauso.
    # score_contrast.py bleibt als eigenstaendiges Werkzeug aufrufbar.
    import score_contrast
    try:
        scores = sorted(scores, reverse=True)
        res = score_contrast.contrast(scores, SCORE_CONTRAST_TAU)
        html_content = score_contrast.build_page(scores, res, SCORE_CONTRAST_TAU, SCORE_CONTRAST_TOP,
                                                 len(scores), "letzte Suche")
    except Exception as e:  # noqa: BLE001 - als Meldung an die Seite, nicht als Absturz
        return jsonify(error=f"Could not build the contrast view: {e}"), 500

    return jsonify(ok=True, html=html_content)


# --- B5a: Indexlauf als eigener Prozess -------------------------------------
# Elternseite (Server): startet "python app.py --indexlauf <mode>", liest
# dessen Fortschritt (Zeilen "PROGRESS {json}") und uebernimmt ihn in
# INDEX_PROGRESS; leitet "Abbrechen" als Zeile "CANCEL" weiter. Kindseite:
# fuehrt run_indexing_guarded aus und meldet INDEX_PROGRESS. Endet der Server,
# schliesst sich die Pipe; das Kind bricht dann ab (kein verwaister Lauf).
# Eine Sperrdatei (flock) verhindert zwei Laeufe gleichzeitig.

_CHILD = [None]
_CHILD_LOCK = threading.Lock()
_PROGRESS_KEYS_FROM_CHILD = ("phase", "error", "log_file", "mode", "current", "total",
                             "current_chunk", "embed_done", "embed_total", "embed_rate",
                             "embed_eta")


def _index_lock_held_elsewhere() -> bool:
    import fcntl
    try:
        fd = os.open(INDEX_LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except OSError:
        return True
    finally:
        os.close(fd)


def run_indexing_in_subprocess(mode: str) -> None:
    env = dict(os.environ)
    env.pop("WERKZEUG_RUN_MAIN", None)
    env.pop("WERKZEUG_SERVER_FD", None)
    # S4: Die harmlose Warnung "leaked semaphore objects ... loky" beim Ende des
    # Lauf-Prozesses unterdruecken (stammt aus einer Hilfsbibliothek des Modells,
    # der Lauf-Prozess endet bewusst hart mit os._exit).
    env["PYTHONWARNINGS"] = ",".join(filter(None, [env.get("PYTHONWARNINGS"),
                                                   "ignore::UserWarning:multiprocessing.resource_tracker"]))
    got_end = False
    try:
        t0 = time.monotonic()
        proc = subprocess.Popen([sys.executable, os.path.abspath(__file__), "--indexlauf", mode],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None,
                                cwd=APP_DIR, env=env)
        # S3: Messung zum langsamen Start von Unterprozessen aus dem Server (S2: ≈ 11 s).
        print(f"[see_also] Index run process started (subprocess start {time.monotonic() - t0:.2f} s)", flush=True)
        with _CHILD_LOCK:
            _CHILD[0] = proc
        if INDEX_CANCEL.is_set():
            _forward_cancel()
        for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").rstrip("\n")
            kind, _, payload = line.partition(" ")
            if kind not in ("PROGRESS", "ENDE"):
                continue
            try:
                data = json.loads(payload)
            except ValueError:
                continue
            with STATUS_LOCK:
                for k in _PROGRESS_KEYS_FROM_CHILD:
                    if k in data:
                        INDEX_PROGRESS[k] = data[k]
            if kind == "ENDE":
                got_end = True
        rc = proc.wait()
        if not got_end or rc not in (0,):
            # Kind ohne Abschlussmeldung beendet (Absturz, getoetet). Der Stand
            # in see_also.db ist der alte (nichts festgeschrieben) oder der neue
            # (festgeschrieben); halbe Zustaende gibt es nicht.
            with STATUS_LOCK:
                INDEX_PROGRESS.update(phase="error", error=(
                    f"The index run process ended unexpectedly (exit code {rc}). "
                    "The previous index is intact. Details are in the server's terminal."))
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        with STATUS_LOCK:
            INDEX_PROGRESS.update(phase="error", error=f"Index run process: {type(e).__name__}: {e}")
    finally:
        with _CHILD_LOCK:
            _CHILD[0] = None
        with STATUS_LOCK:
            INDEX_PROGRESS["is_running"] = False
            bestand_verwerfen()   # P1: Bestand nach dem Lauf neu einlesen
            INDEX_PROGRESS["cancel_requested"] = False


def _forward_cancel() -> None:
    with _CHILD_LOCK:
        proc = _CHILD[0]
    if proc is not None and proc.poll() is None:
        try:
            proc.stdin.write(b"CANCEL\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass


def index_child_main(mode: str) -> int:
    """Einstieg im Kindprozess."""
    import fcntl
    # Protokoll ueber eigenen Kanal; alles andere (Modell-Ladebalken, Warnungen)
    # geht nach stderr und damit ins Terminal des Servers.
    proto = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    try:
        os.nice(INDEX_NICE)
    except OSError:
        pass
    lock_fd = os.open(INDEX_LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        proto.write("ENDE " + json.dumps({"phase": "error", "error": "Another index run is already in progress."}) + "\n")
        proto.flush()
        os._exit(0)
    if os.environ.get("SEE_ALSO_TEST_ATTRAPPE"):   # nur fuer Tests
        import hashlib
        def _vec(t):
            r = np.random.default_rng(int(hashlib.md5(t.encode()).hexdigest()[:8], 16))
            v = r.standard_normal(embedder.EMBEDDING_DIM).astype(np.float32)
            return v / np.linalg.norm(v)
        def _fake(ts):
            time.sleep(float(os.environ.get("SEE_ALSO_TEST_EMBED_S", "0")))
            return (np.array([_vec(t) for t in ts], dtype=np.float32) if ts
                    else np.zeros((0, embedder.EMBEDDING_DIM), np.float32))
        embedder.embed_passages = _fake

    def _stdin_reader():
        for raw in sys.stdin.buffer:
            if raw.strip() == b"CANCEL":
                with STATUS_LOCK:
                    INDEX_PROGRESS["cancel_requested"] = True
                INDEX_CANCEL.set()
        # Pipe geschlossen: der Server ist weg. Kein verwaister Lauf.
        INDEX_CANCEL.set()
    threading.Thread(target=_stdin_reader, daemon=True).start()

    last = [None]
    stop = threading.Event()

    def _report(kind="PROGRESS"):
        with STATUS_LOCK:
            snap = {k: INDEX_PROGRESS.get(k) for k in _PROGRESS_KEYS_FROM_CHILD}
        text = json.dumps(snap, ensure_ascii=False)
        if kind == "ENDE" or text != last[0]:
            last[0] = text
            try:
                proto.write(f"{kind} {text}\n")
                proto.flush()
            except (BrokenPipeError, OSError):
                INDEX_CANCEL.set()

    def _reporter():
        while not stop.wait(0.5):
            _report()
    threading.Thread(target=_reporter, daemon=True).start()

    with STATUS_LOCK:
        INDEX_PROGRESS.update(is_running=True, phase="scan", mode=mode, error=None, log_file=None,
                              embed_done=0, embed_total=0, embed_rate=None, embed_eta=None,
                              current=0, total=0, current_chunk=0)
    run_indexing_guarded(mode)
    stop.set()
    _report("ENDE")
    try:
        proto.flush()
        sys.stderr.flush()
    finally:
        # Hart beenden: Ein Daemon-Thread liest noch stdin; ein normales
        # Interpreter-Ende bricht dann unter Python 3.14 mit SIGABRT ab
        # ("could not acquire lock for <stdin> at interpreter shutdown").
        # Alles Wichtige ist geschrieben (Index, Log, Abschlussmeldung).
        os._exit(0)


@app.route("/index/start/<mode>", methods=["POST"])
def start_indexing(mode):
    if mode not in ("incremental", "full"):
        abort(400)

    with OCR_LOCK:
        if OCR_LAUF["is_running"]:
            return jsonify(started=False, reason="ocr_running"), 409
    with STATUS_LOCK:
        if INDEX_PROGRESS["is_running"]:
            return jsonify(started=False, reason="already_running"), 409
        INDEX_CANCEL.clear()
        INDEX_PROGRESS.update(is_running=True, phase="scan", error=None, log_file=None,
                              cancel_requested=False,
                              embed_done=0, embed_total=0, embed_rate=None, embed_eta=None,
                              mode=mode, current=0, total=0, current_chunk=0)

    if INDEX_SUBPROCESS and _index_lock_held_elsewhere():
        with STATUS_LOCK:
            INDEX_PROGRESS.update(is_running=False, phase=None)
            bestand_verwerfen()   # P1: Bestand nach dem Lauf neu einlesen
        return jsonify(started=False, reason="running_elsewhere"), 409
    target = run_indexing_in_subprocess if INDEX_SUBPROCESS else run_indexing_guarded
    thread = threading.Thread(target=target, args=(mode,), daemon=True)
    thread.start()
    return jsonify(started=True)


@app.route("/index/cancel", methods=["POST"])
def cancel_indexing():
    """VAI 1.4: Abbruch anfordern. Der Lauf haelt an der naechsten Stelle an
    (zwischen zwei PDFs bzw. zwei Einbettungsstapeln); der bisherige Index
    bleibt unveraendert. Ab dem Speichern ist kein Abbruch mehr moeglich."""
    with STATUS_LOCK:
        if not INDEX_PROGRESS["is_running"]:
            return jsonify(cancelling=False, reason="not_running"), 409
        if INDEX_PROGRESS.get("phase") in ("save", "done"):
            return jsonify(cancelling=False, reason="too_late"), 409
        INDEX_CANCEL.set()
        INDEX_PROGRESS["cancel_requested"] = True
    _forward_cancel()
    return jsonify(cancelling=True)


@app.after_request
def no_store_for_pages(response):
    # VAI 1.4: Seiten und JSON nie zwischenspeichern, damit nach einem Update
    # keine alte Fassung im Browser haengen bleibt. PDFs sind ausgenommen.
    if response.mimetype in ("text/html", "application/json"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/index/status")
def index_status():
    with STATUS_LOCK:
        progress = dict(INDEX_PROGRESS)
    return jsonify(progress)


def run_ocr_lauf(anzahl) -> None:
    """Startet ocr.py als eigenen Prozess und uebernimmt dessen Meldungen."""
    try:
        alle = bestand(frisch=True)          # P1: OCR immer auf frischem Bestand
        kandidaten = find_missing_files()
        auftrag = dict(base=PDF_BASE_DIR, db=DB_FILE, ordner=OCR_ORDNER, sprachen=OCR_SPRACHEN,
                       kandidaten=kandidaten, alle_pfade=sorted(alle), anzahl=anzahl, log_dir=LOG_DIR, lock_datei=INDEX_LOCK_FILE,
                       nice=INDEX_NICE, overlap=int(CROSS_PAGE_OVERLAP_CHARS),
                       zeitlimit_je_seite_s=OCR_ZEITLIMIT_JE_SEITE_S, zeitlimit_min_s=OCR_ZEITLIMIT_MIN_S,
                       jobs=OCR_JOBS, parallel=OCR_PARALLEL)
        env = dict(os.environ)
        env.pop("WERKZEUG_RUN_MAIN", None)
        env.pop("WERKZEUG_SERVER_FD", None)
        proc = subprocess.Popen([sys.executable, OCR_FILE], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=None, cwd=APP_DIR, env=env)
        with OCR_LOCK:
            _OCR_CHILD[0] = proc
        proc.stdin.write((json.dumps(auftrag, ensure_ascii=False) + "\n").encode("utf-8"))
        proc.stdin.flush()
        with OCR_LOCK:
            if OCR_LAUF["cancel_requested"]:
                _ocr_cancel_weitergeben()
        got_end = False
        for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").rstrip("\n")
            kind, _, payload = line.partition(" ")
            if kind == "ZEILE":
                with OCR_LOCK:
                    OCR_ZEILEN.append(payload)
                    del OCR_ZEILEN[:-OCR_ZEILEN_MAX]
                continue
            if kind not in ("PROGRESS", "ENDE"):
                continue
            try:
                data = json.loads(payload)
            except ValueError:
                continue
            with OCR_LOCK:
                for k, v in data.items():
                    if k in OCR_LAUF and k != "is_running":
                        OCR_LAUF[k] = v
            if kind == "ENDE":
                got_end = True
        rc = proc.wait()
        if not got_end:
            with OCR_LOCK:
                OCR_LAUF.update(phase="error", error=(
                    f"The OCR process ended unexpectedly (exit code {rc}). "
                    "The next run continues. Details are in the server's terminal."))
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        with OCR_LOCK:
            OCR_LAUF.update(phase="error", error=f"OCR process: {type(e).__name__}: {e}")
    finally:
        with OCR_LOCK:
            _OCR_CHILD[0] = None
            OCR_LAUF["is_running"] = False
            bestand_verwerfen()   # P1: Bestand nach dem Lauf neu einlesen
            OCR_LAUF["cancel_requested"] = False


def _ocr_cancel_weitergeben() -> None:
    proc = _OCR_CHILD[0]
    if proc is not None and proc.poll() is None:
        try:
            proc.stdin.write(b"CANCEL\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass


@app.route("/ocr/lauf/start", methods=["POST"])
def ocr_lauf_start():
    data = request.get_json(silent=True) or {}
    anzahl = data.get("anzahl")
    if anzahl in (None, "", "alle", 0):
        anzahl = None
    else:
        try:
            anzahl = int(anzahl)
        except (TypeError, ValueError):
            return jsonify(started=False, reason="bad_count"), 400
        if anzahl < 1:
            return jsonify(started=False, reason="bad_count"), 400
    if not os.path.exists(DB_FILE):
        return jsonify(started=False, reason="no_index"), 409
    with STATUS_LOCK:
        if INDEX_PROGRESS["is_running"]:
            return jsonify(started=False, reason="index_running"), 409
    with OCR_LOCK:
        if OCR_LAUF["is_running"]:
            return jsonify(started=False, reason="already_running"), 409
        OCR_LAUF.update(is_running=True, phase="start", error=None, log_file=None, total=0, current=0,
                        current_file=None, erfasst=0, nicht_erfasst=0, seiten_fertig=0, seiten_gesamt=0,
                        eta_s=None, s_je_seite=None, cancel_requested=False, anzahl=anzahl)
        OCR_ZEILEN.clear()
    threading.Thread(target=run_ocr_lauf, args=(anzahl,), daemon=True).start()
    return jsonify(started=True)


@app.route("/ocr/lauf/cancel", methods=["POST"])
def ocr_lauf_cancel():
    with OCR_LOCK:
        if not OCR_LAUF["is_running"]:
            return jsonify(cancelling=False, reason="not_running"), 409
        OCR_LAUF["cancel_requested"] = True
        _ocr_cancel_weitergeben()
    return jsonify(cancelling=True)


@app.route("/ocr/lauf/status")
def ocr_lauf_status():
    with OCR_LOCK:
        d = dict(OCR_LAUF)
        d["zeilen"] = list(OCR_ZEILEN)
    return jsonify(d)


def _ocr_mit_zahlen(fehlend: list[str]) -> dict:
    o = ocr_uebersicht()
    zahlen = {"erfasst": 0, "nicht_erfasst": 0, "offen": 0}
    import ocr as _ocr
    for p in fehlend:
        st = o["je_pfad"].get(p, (None, None))[0]
        if st == "erfasst":
            zahlen["erfasst"] += 1
        elif st in _ocr.NICHT_ERFASST:
            zahlen["nicht_erfasst"] += 1
        else:
            zahlen["offen"] += 1
    o["zahlen_fehlend"] = zahlen
    return o


def ocr_uebersicht() -> dict:
    """Fuer die Statusseite: OCR-Zustand je Pfad und die Liste ohne Ergebnis."""
    import ocr as _ocr
    if not os.path.exists(DB_FILE):
        return dict(je_pfad={}, nicht_erfasst=[], erfasst=0)
    with _db_zugriff() as con:
        je_pfad = _ocr.zustand_je_pfad(con)
    im_bestand = bestand()
    nicht = sorted(((p, s, g or _ocr.GRUND_TEXT.get(s, s)) for p, (s, g) in je_pfad.items()
                    if s in _ocr.NICHT_ERFASST and p in im_bestand),
                   key=lambda t: (t[1], t[0]))
    return dict(je_pfad=je_pfad, nicht_erfasst=nicht,
                erfasst=sum(1 for s, _ in je_pfad.values() if s == "erfasst"),
                grund_text=_ocr.GRUND_TEXT)


@app.route("/lookup")
def lookup():
    chunk_id = request.args.get("chunk_id")
    if chunk_id is None or not chunk_id.isdigit():
        return "Ungueltige Chunk-Nummer.", 400
    return redirect(url_for("show_chunk", chunk_id=int(chunk_id)))


def get_chunk_meta(chunk_id: int) -> dict | None:
    idx = get_index()
    entry = _chunk_entry(idx, chunk_id)
    if entry is None or is_excluded(entry["path"]):
        return None
    result = dict(entry)
    result["dateiname"] = entry["path"]
    try:
        mtime_ts = os.path.getmtime(os.path.join(PDF_BASE_DIR, entry["path"]))
        result["mtime"] = datetime.fromtimestamp(mtime_ts).strftime("%Y-%m-%d %H:%M")
    except OSError:
        result["mtime"] = "not available (file missing?)"
    result["textausschnitt"] = entry["text"][:200].replace("\n", " ")
    result["pdf_link"] = build_pdf_link(entry["path"], entry["seite"])
    return result



@app.route("/chunk/<int:chunk_id>")
def show_chunk(chunk_id):
    meta = get_chunk_meta(chunk_id)
    if meta is None:
        return render_template("chunk.html", chunk_id=chunk_id, meta=None), 404
    return render_template("chunk.html", chunk_id=chunk_id, meta=meta)


@app.route("/chunk/<int:chunk_id>/panel")
def chunk_panel(chunk_id):
    meta = get_chunk_meta(chunk_id)
    if meta is None:
        return render_template("_chunk_block.html", chunk_id=chunk_id, meta=None), 404
    return render_template("_chunk_block.html", chunk_id=chunk_id, meta=meta)


def _ocr_eingebettet(pfade: list) -> set:
    """Welche dieser Dokumente wurden aus einer OCR-Fassung eingebettet?"""
    if not pfade or not os.path.exists(DB_FILE):
        return set()
    try:
        with _db_zugriff() as con:
            st = speicher.zustand_lesen(con)
        q = st.get("ocr_quellen", {})
        return {p for p in pfade if p in q}
    except Exception:
        return set()


@app.route("/pdf-ocr/<anypath:filename>")
def serve_pdf_ocr(filename):
    """O2: die OCR-Fassung aus dem Schattenordner (Text markierbar)."""
    base = os.path.realpath(OCR_ORDNER)
    target = os.path.realpath(os.path.join(base, filename))
    if not target.startswith(base + os.sep) or is_excluded(filename) or not os.path.isfile(target):
        abort(404)
    safe_name = os.path.basename(filename).replace("\r", " ").replace("\n", " ")
    return send_from_directory(OCR_ORDNER, filename, download_name=safe_name)


@app.route("/pdf/<anypath:filename>")
def serve_pdf(filename):
    base = os.path.realpath(PDF_BASE_DIR)
    target = os.path.realpath(os.path.join(base, filename))
    if not target.startswith(base + os.sep):
        abort(403)
    # B1: nichts aus ausgeschlossenen Bereichen, nichts ueber symbolische Links
    # (realpath oben loest Links auf; der angefragte Pfad muss selbst echt sein).
    if is_excluded(filename) or os.path.realpath(os.path.join(base, filename)) != os.path.join(base, os.path.normpath(filename)):
        abort(403)
    # Zeilenumbrueche duerfen nicht in HTTP-Kopfzeilen (Dateiname der Antwort).
    safe_name = os.path.basename(filename).replace("\r", " ").replace("\n", " ")
    return send_from_directory(PDF_BASE_DIR, filename, download_name=safe_name)


# G1b: Debug-Modus von Flask (interaktiver Debugger im Browser, Neustart bei
# geaenderten .py-Dateien) nur auf Wunsch: config "debug": true oder
# Umgebungsvariable SEE_ALSO_DEBUG=1. Im Alltag aus.
RUN_DEBUG = bool(CONFIG.get("debug")) or os.environ.get("SEE_ALSO_DEBUG", "") not in ("", "0")

# Wiederherstellung nach unterbrochenem Umschalten. Im Debug-Modus startet
# Flask den eigentlichen Server als Kindprozess neu (WERKZEUG_RUN_MAIN=true).
# Die Pruefung laeuft dort, nicht im Elternprozess, damit die Meldung auf der
# Statusseite im richtigen Prozess steht.
def _db_schliessen() -> None:
    """Beim Beenden (auch Ctrl+C) die Verbindung ordentlich schliessen. Sonst
    wartet auf macOS der erste Zugriff nach dem naechsten Start 5,4 s."""
    con = _DB_STATE.get("con")
    if con is not None:
        try:
            con.close()
        except Exception:
            pass
        _DB_STATE["con"] = None


import atexit  # noqa: E402
atexit.register(_db_schliessen)

# Hinweise nur einmal: im Server selbst (mit Debug-Modus im neu gestarteten
# Kindprozess von Flask), nicht im Indexlauf-Prozess.
_SERVER_MELDET = (__name__ == "__main__" and not IS_INDEX_CHILD
                  and (not RUN_DEBUG or os.environ.get("WERKZEUG_RUN_MAIN") == "true"))
if _SERVER_MELDET:
    if CONFIG_ALTE_SCHLUESSEL:
        print("[see_also] config.json uses old key names, which still work: "
              + ", ".join(f"{a} -> {CONFIG_ALIASE[a]}" for a in CONFIG_ALTE_SCHLUESSEL), flush=True)
if _SERVER_MELDET:
    try:
        import resource
        _soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        print(f"[see_also] Open file limit: {_soft} (hard {_hard})", flush=True)
    except Exception:
        pass

if not IS_INDEX_CHILD and not os.path.exists(DB_FILE):
    STARTUP_NOTICES.append(DB_FEHLT_MELDUNG)
    print(f"[see_also] {DB_FEHLT_MELDUNG}", flush=True)
if IS_INDEX_CHILD:
    pass   # Kindprozess: keine Wiederherstellung (macht der Server), kein Vorladen
elif not (__name__ == "__main__" and RUN_DEBUG and os.environ.get("WERKZEUG_RUN_MAIN") != "true"):
    # N1: Index schon beim Start im Hintergrund laden, damit auch die erste
    # Suche schnell ist. Schlaegt das fehl (kein Index), passiert nichts; die
    # erste Anfrage meldet es dann wie bisher.
    def _warm_index():
        try:
            get_index()
        except Exception as e:  # noqa: BLE001 - nur Hinweis im Terminal
            print(f"[see_also] Index not loaded at start: {e}", flush=True)
        # P1: Bestand einmal einlesen (erste Seite schnell) und auf Wunsch das
        # Modell laden (erste Suche schnell; belegt sofort rund 3,5 GB).
        try:
            t0 = time.monotonic()
            n = len(bestand())
            print(f"[see_also] Collection scanned: {n} PDFs, {time.monotonic() - t0:.1f} s", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[see_also] Collection not scanned at start: {e}", flush=True)
        if CONFIG.get("load_model_at_start", True):
            try:
                t0 = time.monotonic()
                embedder.get_model()
                print(f"[see_also] Model loaded ({time.monotonic() - t0:.1f} s"
                      f"{', offline' if os.environ.get('HF_HUB_OFFLINE') == '1' else ''})", flush=True)
            except Exception as e:  # noqa: BLE001
                hinweis = (" - loaded offline; if the model is incomplete in the cache, set "
                           '"model_offline": false in config.json and restart the server'
                           if embedder.OFFLINE_GESETZT else "")
                print(f"[see_also] Model not loaded at start: {e}{hinweis}", flush=True)
    if __name__ == "__main__":
        threading.Thread(target=_warm_index, daemon=True).start()

if __name__ == "__main__" and IS_INDEX_CHILD:
    sys.exit(index_child_main(sys.argv[2]))
elif __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001, debug=RUN_DEBUG, threaded=True)

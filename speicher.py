"""speicher.py - see_also-Index in einer SQLite-Datenbank (PLAN_SQLITE.md).

S1: Anlegen, Befuellen (Umzug aus den JSON-Dateien), Lesen.
S2: Der Server liest von hier.
S3: Der Indexlauf schreibt hierher, und zwar nur seine Aenderungen, in einer
    einzigen Transaktion (lauf_festschreiben).

Aufbau der Datenbank see_also.db (Namen bewusst sprechend, damit sie auch
im DB Browser for SQLite lesbar ist):

  chunks          eine Zeile je Chunk: id, path, position_in_file, seite,
                  zeile, text, text_for_embedding, bbox (JSON-Text)
  vektoren        id, vektor (1024 x float32 = 4096 Byte, Bytefolge des
                  Rechners, auf Apple Silicon little endian)
  dateien         path, mtime_letzter_lauf, mtime_voll
                  (bisher last_run_snapshot und full_snapshot)
  problemdateien  path, befund, meldung, zeit
  zustand         schluessel, wert (JSON): next_chunk_id, letzte Laeufe,
                  generation, schema_version, sonstige Schluessel

Die Vektoren liegen in einer eigenen Tabelle, damit das Laden aller
Vektoren beim Start nur die Vektorseiten liest und nicht die Texte.
Seitengroesse 64 KB: 15 Vektoren je Seite statt ein Vektor mit
Ueberlaufseite (bei 4 KB je Seite wuerde die Datei fast doppelt so gross).

Pfade werden unveraendert gespeichert (keine Unicode-Normalisierung; auf
macOS sind zerlegte Formen haeufig). Die Matrix-Reihenfolge ist aufsteigend
nach id.
"""
import json
import os
import sqlite3

import numpy as np

SCHEMA_VERSION = 1
EMBEDDING_DIM = 1024
VEKTOR_BYTES = EMBEDDING_DIM * 4
PAGE_SIZE = 65536

SCHEMA = """
CREATE TABLE chunks (
    id                 INTEGER PRIMARY KEY,
    path               TEXT    NOT NULL,
    position_in_file   INTEGER NOT NULL,
    seite              INTEGER,
    zeile              INTEGER,
    text               TEXT    NOT NULL,
    text_for_embedding TEXT    NOT NULL,
    bbox               TEXT,
    UNIQUE (path, position_in_file)
);
-- Schmaler Index fuer das Laden beim Start (id -> path ohne die Textseiten zu lesen).
CREATE INDEX chunks_id_path ON chunks (id, path);
CREATE TABLE vektoren (
    id     INTEGER PRIMARY KEY,
    vektor BLOB NOT NULL
);
CREATE TABLE dateien (
    path               TEXT PRIMARY KEY,
    mtime_letzter_lauf REAL,
    mtime_voll         REAL
);
CREATE TABLE problemdateien (
    path    TEXT PRIMARY KEY,
    befund  TEXT,
    meldung TEXT,
    zeit    TEXT
);
CREATE TABLE zustand (
    schluessel TEXT PRIMARY KEY,
    wert       TEXT
);
"""

# Schluessel aus index_state.json, die eigene Tabellen haben.
_STATE_TABLE_KEYS = ("last_run_snapshot", "full_snapshot", "problemdateien")


def verbinden(path: str, nur_lesen: bool = False, threads: bool = False) -> sqlite3.Connection:
    """Oeffnet die Datenbank. nur_lesen sperrt das Schreiben fuer diese
    Verbindung (PRAGMA query_only).

    Bewusst NICHT mit mode=ro: Gemessen auf macOS (15.11.2026) wartet nach dem
    Schliessen einer mode=ro-Verbindung der erste Zugriff der naechsten
    Verbindung immer 5,4 s, unabhaengig von der Groesse der Datenbank und ohne
    zu lesen (eine mode=ro-Verbindung darf die WAL-Verwaltung -shm beim
    Schliessen nicht aufraeumen). Normal geoeffnet plus query_only: kein
    Warten, Schreiben trotzdem gesperrt."""
    if not os.path.exists(path):
        raise FileNotFoundError(path)   # sqlite3.connect wuerde sonst eine leere Datei anlegen
    # threads=True: Die Verbindung darf von mehreren Threads benutzt werden; der
    # Aufrufer muss die Zugriffe dann selbst nacheinander ordnen (Sperre).
    con = sqlite3.connect(path, check_same_thread=not threads)
    con.execute("PRAGMA synchronous=FULL")
    # Waehrend ein Indexlauf festschreibt, warten andere Schreiber/Checkpoints
    # bis zu 30 s statt sofort mit "database is locked" abzubrechen.
    con.execute("PRAGMA busy_timeout=30000")
    if nur_lesen:
        con.execute("PRAGMA query_only=ON")
    return con


def anlegen(path: str) -> sqlite3.Connection:
    """Neue, leere Datenbank. Die Datei darf noch nicht existieren."""
    if os.path.exists(path):
        raise FileExistsError(path)
    con = sqlite3.connect(path)
    con.execute(f"PRAGMA page_size={PAGE_SIZE}")   # nur vor der ersten Tabelle wirksam
    con.executescript(SCHEMA)
    con.execute("INSERT INTO zustand VALUES ('schema_version', ?)", (json.dumps(SCHEMA_VERSION),))
    con.execute("INSERT INTO zustand VALUES ('generation', ?)", (json.dumps(0),))
    con.commit()
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=FULL")
    return con


def vektor_bytes(v: np.ndarray) -> bytes:
    v = np.ascontiguousarray(v, dtype=np.float32)
    if v.shape != (EMBEDDING_DIM,):
        raise ValueError(f"vector has shape {v.shape}, expected ({EMBEDDING_DIM},)")
    return v.tobytes()


# --- Schreiben (S1: nur der Umzug) ------------------------------------------

def chunks_einfuegen(con, eintraege):
    """eintraege: Iterable von meta-Eintraegen (dict wie in meta.json)."""
    def zeilen():
        for e in eintraege:
            yield (e["id"], e["path"], e["position_in_file"], e.get("seite"), e.get("zeile"),
                   e["text"], e["text_for_embedding"],
                   None if e.get("bbox") is None else json.dumps(e["bbox"]))
    con.executemany("INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?)", zeilen())


def vektoren_einfuegen(con, ids, matrix):
    def zeilen():
        for i, cid in enumerate(ids):
            yield (cid, vektor_bytes(matrix[i]))
    con.executemany("INSERT INTO vektoren VALUES (?,?)", zeilen())


def zustand_schreiben(con, state: dict):
    """index_state.json -> dateien, problemdateien, zustand."""
    last = state.get("last_run_snapshot") or {}
    full = state.get("full_snapshot") or {}
    paths = sorted(set(last) | set(full))
    con.executemany("INSERT INTO dateien VALUES (?,?,?)",
                    ((p, last.get(p), full.get(p)) for p in paths))
    con.executemany("INSERT INTO problemdateien VALUES (?,?,?,?)",
                    ((p, i.get("befund"), i.get("meldung"), i.get("zeit"))
                     for p, i in (state.get("problemdateien") or {}).items()))
    for k, v in state.items():
        if k in _STATE_TABLE_KEYS:
            continue
        con.execute("INSERT OR REPLACE INTO zustand VALUES (?,?)", (k, json.dumps(v, ensure_ascii=False)))


# --- Lesen ------------------------------------------------------------------

def zustand_lesen(con) -> dict:
    """Gibt den Zustand in derselben Form wie index_state.json zurueck
    (ohne die internen Schluessel schema_version und generation)."""
    state = {k: json.loads(v) for k, v in con.execute("SELECT schluessel, wert FROM zustand")}
    state.pop("schema_version", None)
    state.pop("generation", None)
    state.pop("umzug", None)
    last, full = {}, {}
    for p, ml, mv in con.execute("SELECT path, mtime_letzter_lauf, mtime_voll FROM dateien"):
        if ml is not None:
            last[p] = ml
        if mv is not None:
            full[p] = mv
    state["last_run_snapshot"] = last
    state["full_snapshot"] = full
    probs = {}
    for p, b, m, z in con.execute("SELECT path, befund, meldung, zeit FROM problemdateien"):
        info = {"befund": b, "meldung": m}
        if z is not None:
            info["zeit"] = z
        probs[p] = info
    state["problemdateien"] = probs
    return state


def generation(con) -> int:
    row = con.execute("SELECT wert FROM zustand WHERE schluessel='generation'").fetchone()
    return json.loads(row[0]) if row else 0


def vektoren_laden(con):
    """Alle Vektoren, aufsteigend nach id. Gibt (ids als int64-Array,
    Matrix float32 [n, 1024]) zurueck."""
    n = con.execute("SELECT COUNT(*) FROM vektoren").fetchone()[0]
    ids = np.empty(n, dtype=np.int64)
    matrix = np.empty((n, EMBEDDING_DIM), dtype=np.float32)
    flat = matrix.reshape(-1).view(np.uint8)
    cur = con.execute("SELECT id, vektor FROM vektoren ORDER BY id")
    i = 0
    while True:
        rows = cur.fetchmany(4096)
        if not rows:
            break
        for cid, blob in rows:
            if len(blob) != VEKTOR_BYTES:
                raise ValueError(f"vector for id {cid} has {len(blob)} bytes, expected {VEKTOR_BYTES}")
            ids[i] = cid
            flat[i * VEKTOR_BYTES:(i + 1) * VEKTOR_BYTES] = np.frombuffer(blob, dtype=np.uint8)
            i += 1
    if i != n:
        raise RuntimeError(f"vectors: {i} read, {n} counted")
    return ids, matrix


def chunk_lesen(con, cid: int) -> dict | None:
    row = con.execute("SELECT id, path, position_in_file, seite, zeile, text, text_for_embedding, bbox "
                      "FROM chunks WHERE id=?", (cid,)).fetchone()
    return None if row is None else _zeile_zu_eintrag(row)


def _zeile_zu_eintrag(row) -> dict:
    cid, path, pos, seite, zeile, text, tfe, bbox = row
    return {"id": cid, "path": path, "position_in_file": pos, "seite": seite, "zeile": zeile,
            "text": text, "text_for_embedding": tfe,
            "bbox": None if bbox is None else json.loads(bbox)}


def ids_und_pfade(con):
    """(id, path) aller Chunks aufsteigend nach id; liest nur den schmalen
    Index chunks_id_path, nicht die Texte."""
    return con.execute("SELECT id, path FROM chunks INDEXED BY chunks_id_path ORDER BY id").fetchall()


def alle_chunks(con, batch=8192):
    """Alle Chunks aufsteigend nach id, als dicts (Generator, speicherschonend)."""
    cur = con.execute("SELECT id, path, position_in_file, seite, zeile, text, text_for_embedding, bbox "
                      "FROM chunks ORDER BY id")
    while True:
        rows = cur.fetchmany(batch)
        if not rows:
            return
        for r in rows:
            yield _zeile_zu_eintrag(r)


# --- S3: Indexlauf ------------------------------------------------------------

def pfade_mit_chunks(con) -> set:
    """Alle Pfade, zu denen Chunks im Index stehen."""
    return {r[0] for r in con.execute("SELECT DISTINCT path FROM chunks")}


def anzahl_chunks(con) -> int:
    return con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]


def ids_nach_pfad(con, paths) -> list[dict]:
    """Bisherige Chunks der genannten Dateien als knappe Eintraege
    {"id", "path"}, je Datei in der Reihenfolge position_in_file. Das ist,
    was indexer.assign_ids_and_merge als "old_meta" braucht, um IDs nach
    struktureller Position zu erhalten - ohne den ganzen Bestand zu lesen."""
    out = []
    for p in paths:
        for (cid,) in con.execute("SELECT id FROM chunks WHERE path=? ORDER BY position_in_file", (p,)):
            out.append({"id": cid, "path": p})
    return out


def zustand_ersetzen(con, state: dict) -> None:
    """Schreibt den Zustand (Form wie index_state.json) vollstaendig neu.
    Nur innerhalb einer Transaktion aufrufen."""
    con.execute("DELETE FROM dateien")
    con.execute("DELETE FROM problemdateien")
    alt = {k for (k,) in con.execute("SELECT schluessel FROM zustand")}
    behalten = {"schema_version", "generation", "umzug"}
    for k in alt - behalten - set(state):
        con.execute("DELETE FROM zustand WHERE schluessel=?", (k,))
    zustand_schreiben(con, state)


def lauf_festschreiben(con, zu_entfernen, neue_eintraege: list, vektoren: np.ndarray,
                       state: dict, vor_commit=None) -> dict:
    """Schreibt das Ergebnis eines Indexlaufs in EINER Transaktion:
    - alle Chunks (und Vektoren) der Dateien in zu_entfernen loeschen
      (neu verarbeitete Dateien und verschwundene Dateien),
    - neue_eintraege mit ihren Vektoren einfuegen (Zeile i von vektoren
      gehoert zu neue_eintraege[i]),
    - den Zustand ersetzen und die generation um 1 erhoehen.
    Entweder alles oder nichts: Bei einer Ausnahme oder einem Absturz vor dem
    Festschreiben bleibt die Datenbank, wie sie war. vor_commit: optionaler
    Aufruf unmittelbar vor dem Festschreiben (nur fuer Tests)."""
    if vektoren.shape != (len(neue_eintraege), EMBEDDING_DIM):
        raise ValueError(f"{len(neue_eintraege)} entries, but vectors of shape {vektoren.shape}")
    ids = [e["id"] for e in neue_eintraege]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate chunk IDs in the run result")
    con.execute("BEGIN IMMEDIATE")
    try:
        entfernt = entfernt_v = 0
        for p in zu_entfernen:
            entfernt_v += con.execute("DELETE FROM vektoren WHERE id IN (SELECT id FROM chunks WHERE path=?)",
                                      (p,)).rowcount
            entfernt += con.execute("DELETE FROM chunks WHERE path=?", (p,)).rowcount
        # Jeder entfernte Chunk muss genau einen Vektor gehabt haben. (Bewusst
        # kein COUNT(*) ueber alle Vektoren: das liest die ganze Vektortabelle,
        # bei 450.000 Chunks 1,85 GB, nur um zwei Zahlen zu vergleichen.)
        if entfernt_v != entfernt:
            raise RuntimeError(f"{entfernt} chunks but {entfernt_v} vectors removed - nothing committed")
        chunks_einfuegen(con, neue_eintraege)
        vektoren_einfuegen(con, ids, vektoren)
        zustand_ersetzen(con, state)
        gen = generation(con) + 1
        con.execute("INSERT OR REPLACE INTO zustand VALUES ('generation', ?)", (json.dumps(gen),))
        n_c = anzahl_chunks(con)       # liest nur den schmalen Index
        if vor_commit is not None:
            vor_commit()
        con.commit()
    except BaseException:
        con.rollback()
        raise
    return {"entfernt": entfernt, "eingefuegt": len(neue_eintraege), "chunks_gesamt": n_c, "generation": gen}

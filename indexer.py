"""
indexer.py - Block A: echte PDF-Extraktion und seitenweises Chunking.

Zentrale Entscheidung (bestaetigt durch den User): Ein Chunk ueberschreitet
NIE eine Seitengrenze. Das macht "welche Seite zeige ich beim Klick" immer
eindeutig - der Hauptgrund fuer den Usability-Vorteil gegenueber See-Also-I
(vault-suggest), das Text seitenlos zu einem Fliesstext zusammenfuegt.

Persistente Chunk-IDs (siehe README): Eine ID bezeichnet eine *Stelle*
(n-ter Chunk einer Datei in Extraktionsreihenfolge), nicht einen
eingefrorenen Inhalt. Aendert sich der Text an dieser Stelle, bleibt die ID
gleich - "neueste Auflage aus der Bibliothek". Nur bei struktureller
Verschiebung (mehr/weniger Chunks als vorher) werden IDs neu vergeben
bzw. endgueltig stillgelegt (nie wiederverwendet).

Noch NICHT Teil dieses Moduls: Embeddings (Block C) - bewusst getrennt,
da sentence-transformers/torch eine weitere, schwerere Abhaengigkeit ist.
"""

from __future__ import annotations

import os
from pathlib import Path

import warnings

# PyMuPDF: neuere Versionen warnen beim alten Namen "fitz" (FutureWarning) und
# wollen ihn spaeter abschaffen. Erst "fitz" (die Testattrappen setzen diesen
# Namen), ohne Warnung; fehlt er, "pymupdf".
with warnings.catch_warnings():
    warnings.filterwarnings("ignore", message=".*fitz.*deprecated", category=FutureWarning)
    try:
        import fitz  # PyMuPDF
    except ImportError:
        import pymupdf as fitz

CHUNK_CHARS = 1500
CHUNK_OVERLAP = 200


def file_sig(path: Path) -> str:
    # Aenderungssignatur - Muster aus vault-suggest uebernommen (dort
    # file_sig genannt), da es sich dort bereits bewaehrt hat.
    s = path.stat()
    return f"{s.st_mtime:.0f}:{s.st_size}"


def scan_pdf_paths(base_dir: str) -> list[str]:
    # Relative Pfade, rekursiv - PDF_BASE_DIR ist laut Aufgabenstellung
    # eine Baumstruktur, kein flaches Verzeichnis.
    result = []
    for root, _dirs, files in os.walk(base_dir):
        for name in files:
            if name.lower().endswith(".pdf"):
                full = os.path.join(root, name)
                result.append(os.path.relpath(full, base_dir))
    return sorted(result)


def _page_lines_with_layout_check(page) -> tuple[list[dict], bool]:
    """
    Liefert Zeilen einer Seite mit Bounding-Box, in der von PyMuPDF
    gelieferten Reihenfolge, plus ein Flag, ob das Layout als zuverlaessig
    (einspaltig, lineare Lesereihenfolge) eingeschaetzt wird.

    Heuristik fuer "mehrspaltig/unzuverlaessig" (bewusst einfach, keine
    Garantie): Wenn Zeilen mit deutlich unterschiedlicher X-Startposition
    UND ueberlappendem Y-Bereich existieren, deutet das auf Spalten hin,
    bei denen die reine Ausgabereihenfolge nicht der visuellen Lesart
    entsprechen muss.
    """
    raw = page.get_text("dict")
    lines = []
    for block in raw.get("blocks", []):
        for line in block.get("lines", []):
            bbox = line.get("bbox")
            text = "".join(span.get("text", "") for span in line.get("spans", []))
            if text.strip():
                lines.append({"text": text, "bbox": bbox})

    reliable = True
    for i, a in enumerate(lines):
        for b in lines[i + 1:]:
            x_diff = abs(a["bbox"][0] - b["bbox"][0])
            y_overlap = not (a["bbox"][3] < b["bbox"][1] or b["bbox"][3] < a["bbox"][1])
            if x_diff > 100 and y_overlap:
                reliable = False
                break
        if not reliable:
            break

    return lines, reliable


def extract_page_chunks(pdf_path: Path, cross_page_overlap_chars: int = 0) -> list[dict]:
    """
    Oeffnet ein PDF, liefert eine geordnete Liste von Chunks UEBER ALLE
    SEITEN, wobei kein Chunk eine Seitengrenze ueberschreitet. Jeder
    Chunk-Eintrag: {"seite": int, "zeile": int|None, "text": str,
    "text_for_embedding": str, "bbox": tuple|None}.

    "text" ist der reine, seitenechte Chunk-Text (fuer die Anzeige -
    zeigt nur, was tatsaechlich auf dieser Seite steht).

    "text_for_embedding" enthaelt zusaetzlich Kontext von der jeweils
    vorherigen/naechsten Seite (siehe README, Abschnitt "Chunk-Grenze an
    Seitenuebergaengen") - NUR fuer spaetere Embeddings (Block C) gedacht,
    nie fuer die Anzeige. Zweiseitig: der erste Chunk einer Seite bekommt
    Kontext von der Vorseite vorangestellt, der letzte Chunk einer Seite
    bekommt Kontext von der Folgeseite angehaengt. Eine Seite, die
    komplett in einen Chunk passt, ist gleichzeitig erster und letzter
    Chunk und bekommt entsprechend Kontext von BEIDEN Nachbarseiten -
    kein Fehler, sondern eine bewusst in Kauf genommene Randbedingung.
    Erste/letzte Seite des Dokuments haben nur eine Nachbarseite.

    Diese Entscheidung (zweiseitig, "cross_page_overlap_chars" als Wert)
    ist ausdruecklich NICHT empirisch validiert - siehe README fuer die
    Begruendung dieser bewussten Unsicherheit.
    """
    doc = fitz.open(str(pdf_path))
    try:
        # Phase 1: alle Seiten einlesen, BEVOR gechunkt wird - noetig,
        # um beim Chunken bereits auf Vorseiten-/Folgeseiten-Text
        # zugreifen zu koennen.
        pages_data = []
        for page in doc:
            lines, reliable = _page_lines_with_layout_check(page)
            page_text_parts = []
            line_offsets = []
            running_offset = 0
            for line_no, line in enumerate(lines, start=1):
                line_offsets.append((running_offset, line_no, line["bbox"]))
                page_text_parts.append(line["text"])
                running_offset += len(line["text"]) + 1
            pages_data.append({
                "text": "\n".join(page_text_parts),
                "line_offsets": line_offsets,
                "reliable": reliable,
            })
    finally:
        doc.close()

    chunks = []

    # Phase 2: pro Seite chunken, mit Zugriff auf Nachbarseiten fuer den
    # Cross-Page-Overlap.
    for page_index, page_data in enumerate(pages_data):
        page_text = page_data["text"]
        if not page_text.strip():
            continue

        line_offsets = page_data["line_offsets"]
        reliable = page_data["reliable"]
        page_number = page_index + 1

        prev_tail = ""
        if cross_page_overlap_chars > 0 and page_index > 0:
            prev_tail = pages_data[page_index - 1]["text"][-cross_page_overlap_chars:]

        next_head = ""
        if cross_page_overlap_chars > 0 and page_index < len(pages_data) - 1:
            next_head = pages_data[page_index + 1]["text"][:cross_page_overlap_chars]

        # Chunk-Grenzen innerhalb der Seite bestimmen. Enden und Anfaenge
        # werden auf den naechsten Wortzwischenraum verschoben, statt hart
        # bei CHUNK_CHARS zu schneiden - vorher wurden Woerter an den
        # Chunk-Raendern mitten durchtrennt (z.B. "r Aufgabe" statt "zur
        # Aufgabe"). MIN_CHUNK_CHARS ist eine Rueckfallgrenze: wird sie bei
        # der Rueckwaertssuche erreicht, ohne ein Leerzeichen zu finden
        # (z.B. eine sehr lange zusammenhaengende Zeichenkette ohne
        # Trennpunkt), greift wieder der harte Schnitt als Rueckfall.
        MIN_CHUNK_CHARS = max(1, CHUNK_CHARS // 2)

        def _snap_end_to_word_boundary(text: str, raw_end: int, floor: int) -> int:
            pos = raw_end
            while pos > floor and not text[pos - 1].isspace():
                pos -= 1
            return pos if pos > floor else raw_end

        start = 0
        length = len(page_text)
        page_chunk_bounds = []
        while start < length:
            raw_end = min(start + CHUNK_CHARS, length)
            if raw_end < length:
                end = _snap_end_to_word_boundary(page_text, raw_end, start + MIN_CHUNK_CHARS)
            else:
                end = raw_end
            page_chunk_bounds.append((start, end))
            if end == length:
                break
            # Naechster Start: Overlap wie bisher, zusaetzlich vorwaerts
            # zum naechsten Wortanfang verschoben (kurze Suchdistanz, da
            # bereits nahe einer durch obigen Schnitt gewaehlten Grenze).
            start = max(end - CHUNK_OVERLAP, start + 1)
            while start < end and start > 0 and not page_text[start - 1].isspace():
                start += 1

        for i, (start, end) in enumerate(page_chunk_bounds):
            chunk_text = page_text[start:end].strip()
            if not chunk_text:
                continue

            zeile = None
            bbox = None
            if reliable:
                for offset, line_no, line_bbox in line_offsets:
                    if offset <= start:
                        zeile = line_no
                        bbox = line_bbox
                    else:
                        break

            text_for_embedding = chunk_text
            if i == 0 and prev_tail:
                text_for_embedding = prev_tail + " " + text_for_embedding
            if i == len(page_chunk_bounds) - 1 and next_head:
                text_for_embedding = text_for_embedding + " " + next_head

            chunks.append({
                "seite": page_number,
                "zeile": zeile,
                "text": chunk_text,
                "text_for_embedding": text_for_embedding,
                "bbox": bbox,
            })

    return chunks


def assign_ids_and_merge(
    current_chunks_by_path: dict[str, list[dict]],
    old_meta: list[dict],
    next_id: int,
    unchanged_paths=frozenset(),
) -> tuple[list[dict], int, list[str]]:
    """
    Fuehrt neu extrahierte Chunks mit dem bisherigen meta-Bestand zusammen,
    unter Beibehaltung persistenter IDs nach struktureller Position
    (n-ter Chunk derselben Datei) - siehe README, Abschnitt "Persistente
    Chunk-IDs". Dateien, die in current_chunks_by_path fehlen, werden als
    geloescht behandelt (ihre IDs werden stillgelegt, nie wiederverwendet).

    unchanged_paths: Dateien, die bewusst nicht neu verarbeitet wurden
    (inkrementeller Lauf). Sie gelten NICHT als geloescht und tauchen im
    Protokoll nicht auf (VAI 1.5: vorher eine "stillgelegt"-Zeile je Chunk).

    Gibt zurueck: (neue_meta_liste, neuer_next_id_zaehler, log_zeilen)
    """
    log_lines = []

    # Alte Chunks nach Pfad gruppieren, in ihrer bisherigen Reihenfolge -
    # das IST die strukturelle Position (Index in dieser Liste = n-ter Chunk).
    old_by_path: dict[str, list[dict]] = {}
    for entry in old_meta:
        old_by_path.setdefault(entry["path"], []).append(entry)

    new_meta = []

    for path, new_chunks in current_chunks_by_path.items():
        old_chunks = old_by_path.get(path, [])
        first_new_id = None
        n_new_ids = 0
        for i, chunk in enumerate(new_chunks):
            if i < len(old_chunks):
                # Gleiche strukturelle Position - ID beibehalten, auch
                # wenn sich der Text an dieser Stelle geaendert hat
                # (gewolltes Verhalten: "neueste Auflage am gleichen Platz").
                chunk_id = old_chunks[i]["id"]
            else:
                # Datei hat mehr Chunks als vorher - neue ID vergeben.
                chunk_id = next_id
                next_id += 1
                if first_new_id is None:
                    first_new_id = chunk_id
                n_new_ids += 1

            new_meta.append({
                "id": chunk_id,
                "path": path,
                "position_in_file": i,
                "seite": chunk["seite"],
                "zeile": chunk["zeile"],
                "text": chunk["text"],
                "text_for_embedding": chunk["text_for_embedding"],
                "bbox": chunk["bbox"],
            })

        # Neue IDs je Datei in einer Zeile (VAI 1.5).
        if n_new_ids:
            log_lines.append(f"New IDs {first_new_id} to {first_new_id + n_new_ids - 1} "
                             f"({n_new_ids}) for {path}")

        # Stillgelegte IDs protokollieren (Datei ist geschrumpft), eine Zeile je Datei.
        if len(old_chunks) > len(new_chunks):
            retired = old_chunks[len(new_chunks):]
            log_lines.append(
                f"{len(retired)} IDs retired for {path} "
                f"(IDs {retired[0]['id']} to {retired[-1]['id']}, file shrank)"
            )

    # Dateien, die komplett verschwunden sind - alle ihre IDs stilllegen.
    # Bewusst nicht verarbeitete (unchanged_paths) sind NICHT verschwunden.
    for path, old_chunks in old_by_path.items():
        if path not in current_chunks_by_path and path not in unchanged_paths:
            log_lines.append(
                f"{len(old_chunks)} IDs retired for {path} "
                f"(IDs {old_chunks[0]['id']} to {old_chunks[-1]['id']}, file deleted)"
            )

    return new_meta, next_id, log_lines


def count_extractable_chars(pdf_path: Path) -> int:
    # Grober Vorab-Check (kein vollstaendiges Chunking): Wie viele Zeichen
    # Text stecken ueberhaupt in der Datei? Dient der automatischen
    # OCR-Modus-Wahl beim Remastering (See-Also-III): 0 bzw. sehr wenige
    # Zeichen -> normaler ocrmypdf-Modus reicht (kein Textebene vorhanden,
    # Fall EAI); spuerbar vorhandener, aber vermutlich fehlerhafter Text
    # -> --force-ocr noetig, um die alte Ebene zu verwerfen statt nur zu
    # ergaenzen (Fall "Druckschrift"). Eigene, robuste try/except-Klammer,
    # da dieser Check auch auf Dateien laufen kann, die aus anderen
    # Gruenden nicht oeffnbar sind - dann lieber 0 zurueckgeben (fuehrt
    # zu normalem Modus) als den ganzen Remastering-Lauf abzubrechen.
    try:
        doc = fitz.open(str(pdf_path))
        total = sum(len(page.get_text()) for page in doc)
        doc.close()
        return total
    except Exception:
        return 0


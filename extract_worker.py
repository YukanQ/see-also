"""extract_worker.py - Arbeitsprozess fuer das Lesen der PDFs (B5b).

Wird von app.py als eigener Prozess gestartet. Liest je Zeile einen absoluten
PDF-Pfad als JSON-Zeichenkette von stdin, schreibt zuerst "START" und dann eine Zeile
"ERG <json>" mit {"ok": true, "chunks": [...]} oder {"ok": false, "msg": ...}.
Stuerzt PyMuPDF im C-Code ab, endet nur dieser Prozess; app.py merkt das,
vermerkt die Datei als Problemdatei und startet einen neuen Arbeitsprozess.
Laedt kein Modell und kein torch.

Nur fuer Tests: Ist die Umgebungsvariable SEE_ALSO_TEST_ATTRAPPE gesetzt,
wird statt PyMuPDF eine Attrappe benutzt (Dateinamen absturz*, haengt*,
kaputt*, leer*, laut*, spricht* loesen den jeweiligen Fall aus).
"""
import json
import os
import sys
import time

overlap = int(sys.argv[1]) if len(sys.argv) > 1 else 0

# Eigener Kanal fuer das Protokoll: PyMuPDF/MuPDF schreibt Meldungen wie
# "MuPDF error: format error: cmsOpenProfileFromMem failed" auf stdout. Damit
# sie das Protokoll nicht stoeren, wird stdout (auch auf C-Ebene, fd 1) auf
# stderr umgelenkt und das Protokoll ueber eine Kopie des alten stdout geschrieben.
PROTO = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
os.dup2(2, 1)
sys.stdout = sys.stderr

if os.environ.get("SEE_ALSO_TEST_ATTRAPPE"):
    import types
    sys.modules["fitz"] = types.ModuleType("fitz")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import indexer  # noqa: E402

if os.environ.get("SEE_ALSO_TEST_ATTRAPPE"):
    def _fake(path, o=0):
        name = os.path.basename(str(path))
        if name.startswith("absturz"):
            # Absturz nachstellen. SIGKILL statt os.abort(): gleiche Wirkung fuer
            # see_also (Prozess weg, negativer Rueckgabewert), aber macOS zeigt
            # keinen Dialog "Python wurde unerwartet beendet".
            import signal
            os.kill(os.getpid(), signal.SIGKILL)
        if name.startswith("haengt"):
            time.sleep(3600)
        if name.startswith("kaputt"):
            raise ValueError("format error: kaputt (Attrappe)")
        if name.startswith("leer"):
            return []
        if name.startswith("spricht"):
            print("MuPDF error: format error: cmsOpenProfileFromMem failed")
            os.write(1, b"MuPDF error: auf C-Ebene\n")
        if name.startswith("laut"):
            sys.stderr.write("warning: MuPDF Warnung\n" * 50000)
            sys.stderr.flush()
        mt = int(os.path.getmtime(path))
        return [dict(seite=i + 1, zeile=1, text=f"{name} S{i+1} v{mt}",
                     text_for_embedding=f"E {name} S{i+1} v{mt}", bbox=(0.0, 1.0, 2.0, 3.0))
                for i in range(2)]
    indexer.extract_page_chunks = _fake

for line in sys.stdin:
    # Je Zeile ein Pfad als JSON-Zeichenkette: Dateinamen duerfen Zeilenumbrueche
    # enthalten (gesehen 13.11.2026 in Electronics), JSON maskiert sie.
    if not line.strip():
        continue
    path = json.loads(line)
    PROTO.write("START\n"); PROTO.flush()
    try:
        chunks = indexer.extract_page_chunks(path, overlap)
        out = {"ok": True, "chunks": chunks}
    except Exception as e:  # noqa: BLE001 - Python-Fehler werden gemeldet, nicht verschluckt
        out = {"ok": False, "msg": f"{type(e).__name__}: {e}"[:500]}
    PROTO.write("ERG " + json.dumps(out, ensure_ascii=False) + "\n")
    PROTO.flush()

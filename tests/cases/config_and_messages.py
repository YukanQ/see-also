# Test case: English config keys with German aliases, debug switch, English messages, tools.
# Run: python3 config_and_messages.py <project folder> [output file]  (or via pytest, see tests/README.md)
"""G1b: englische Meldungen, englische config-Schluessel mit deutschen Aliasen, Debug-Schalter."""
import sys, os, json, shutil, tempfile, time, types, subprocess, signal, socket
src = os.path.realpath(sys.argv[1]); BASE = os.path.realpath(tempfile.mkdtemp(prefix="sa_g1b_"))
R = BASE + "/daten"; os.makedirs(R + "/A")
for n in range(3): open(f"{R}/A/d{n}.pdf", "wb").write(b"%PDF x")
open(f"{R}/wurzel.pdf", "wb").write(b"%PDF x")
T = BASE + "/proj"; os.makedirs(T)
for f in ["app.py", "indexer.py", "embedder.py", "score_contrast.py", "extract_worker.py", "speicher.py", "ocr.py"]:
    shutil.copy(os.path.join(src, f), T)
shutil.copytree(os.path.join(src, "templates"), T + "/templates")
shutil.copytree(os.path.join(src, "tools"), T + "/tools")
os.makedirs(T + "/static", exist_ok=True)
res = []
def check(n, ok, d=""): ok = bool(ok); res.append(ok); print("ok    " if ok else "FEHLER", n, d, flush=True)

sys.path.insert(0, T); os.chdir(T)
sys.modules["fitz"] = types.ModuleType("fitz")
import speicher; speicher.anlegen(T + "/see_also.db").close()

PROBE = r'''
import sys, json, types; sys.modules["fitz"] = types.ModuleType("fitz"); sys.path.insert(0, ".")
import app as A
print("ERG " + json.dumps(dict(excluded=sorted(A.EXCLUDED_AREAS), ocr=A.OCR_ORDNER, trash=A.PAPIERKORB,
      cache=A.BESTAND_GUELTIG_S, limit=A.PDF_TIME_LIMIT_S, debug=A.RUN_DEBUG, alt=A.CONFIG_ALTE_SCHLUESSEL,
      offline=A.embedder.OFFLINE, base=A.PDF_BASE_DIR)))
'''
def probe(cfg, env=None):
    c = dict(pdf_base_dir=R); c.update(cfg); json.dump(c, open(T + "/config.json", "w"))
    e = dict(os.environ); e.pop("SEE_ALSO_DEBUG", None); e.update(env or {})
    r = subprocess.run([sys.executable, "-c", PROBE], capture_output=True, text=True, env=e, cwd=T)
    zeile = [z for z in r.stdout.splitlines() if z.startswith("ERG ")]
    return json.loads(zeile[0][4:]) if zeile else {"fehler": r.stderr[-500:]}

# 1. config-Schluessel
en = probe(dict(excluded_areas=["X"], ocr_folder="/tmp/ocr_en", trash="/tmp/trash_en", scan_cache_s=5,
                pdf_time_limit_s=77, model_offline=False))
check("englische Schluessel wirken", en.get("excluded") == ["X"] and en.get("ocr") == "/tmp/ocr_en"
      and en.get("trash") == "/tmp/trash_en" and en.get("cache") == 5 and en.get("limit") == 77
      and en.get("offline") is False and en.get("alt") == [], en)
de = probe(dict(ausgeschlossen=["Y"], ocr_ordner="/tmp/ocr_de", papierkorb="/tmp/trash_de", bestand_gueltig_s=6,
                pdf_zeitlimit_s=88, modell_offline=True))
check("deutsche Schluessel gelten weiter (Alias)", de.get("excluded") == ["Y"] and de.get("ocr") == "/tmp/ocr_de"
      and de.get("trash") == "/tmp/trash_de" and de.get("cache") == 6 and de.get("limit") == 88
      and de.get("offline") is True, de)
check("alte Schluessel werden gemeldet", sorted(de.get("alt", [])) == sorted(
    ["ausgeschlossen", "ocr_ordner", "papierkorb", "bestand_gueltig_s", "pdf_zeitlimit_s", "modell_offline"]), de)
beide = probe(dict(ausgeschlossen=["alt"], excluded_areas=["neu"]))
check("beide da: der englische gilt", beide.get("excluded") == ["neu"], beide)

# G3: pdf_base_dir mit ~ und relativ zum Projektordner
b = probe(dict(pdf_base_dir="~/Sammlung"))
check("pdf_base_dir mit ~ wird aufgeloest", b.get("base") == os.path.expanduser("~/Sammlung"), b)
b = probe(dict(pdf_base_dir="examples/sample-library"))
check("pdf_base_dir relativ zum Projektordner", b.get("base") == os.path.join(T, "examples/sample-library"), b)

# 2. Debug-Schalter
check("Debug: Vorgabe aus", probe({}).get("debug") is False)
check("Debug: config debug true", probe(dict(debug=True)).get("debug") is True)
check("Debug: SEE_ALSO_DEBUG=1", probe({}, {"SEE_ALSO_DEBUG": "1"}).get("debug") is True)
check("Debug: SEE_ALSO_DEBUG=0 bleibt aus", probe({}, {"SEE_ALSO_DEBUG": "0"}).get("debug") is False)

# 3. Serverstart: Meldungen englisch, einmal, ohne Reloader
frei = True
with socket.socket() as s:
    try:
        s.bind(("127.0.0.1", 5001))
    except OSError:
        frei = False
if frei:
    json.dump(dict(pdf_base_dir=R, ausgeschlossen=[], modell_beim_start_laden=False), open(T + "/config.json", "w"))
    e = dict(os.environ); e.pop("SEE_ALSO_DEBUG", None); e["PYTHONPATH"] = T
    p = subprocess.Popen([sys.executable, "-c", "import sys,types; sys.modules['fitz']=types.ModuleType('fitz'); "
                          "import runpy; sys.argv=['app.py']; runpy.run_path('app.py', run_name='__main__')"],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=T, env=e)
    time.sleep(6)
    p.send_signal(signal.SIGINT)
    try:
        out = p.communicate(timeout=15)[0]
    except subprocess.TimeoutExpired:
        p.kill(); out = p.communicate()[0]
    check("Start: Flask meldet 'Debug mode: off'", "Debug mode: off" in out, out[-800:])
    check("Start: alte Schluessel einmal genannt", out.count("config.json uses old key names") == 1
          and "ausgeschlossen -> excluded_areas" in out and "modell_beim_start_laden -> load_model_at_start" in out, out[-800:])
    check("Start: kein Reloader", "Restarting with" not in out and "Debugger is active" not in out, out[-800:])
    check("Start: Bestand englisch gemeldet", "Collection scanned: 4 PDFs" in out, out[-800:])
    check("Start: keine deutschen Servermeldungen", not any(w in out for w in
          ("Bestand eingelesen", "Grenze offene", "Modell geladen", "Verbindung zu")), out[-800:])
else:
    print("(Port 5001 belegt: Serverstart nicht geprueft)")

# 4. Uebersetzung alter Eintraege, Wurzelbereich, Listen
json.dump(dict(pdf_base_dir=R), open(T + "/config.json", "w"))
json.dump({"bereiche": ["(direkt in der Wurzel)", "A"]}, open(T + "/suchbereich.json", "w"))
import app as A, ocr
check("Suchbereich: alter Name des Wurzelbereichs uebernommen", A.SEARCH_SCOPE == {"(top level)", "A"}, A.SEARCH_SCOPE)
check("OCR-Grund alt -> englisch", ocr.grund_en("nach 300 s (12 Seiten) [2. Versuch: --force-ocr]")
      == "after 300 s (12 pages) [2nd attempt: --force-ocr]" and ocr.grund_en("kein PDF: Textdatei") == "not a PDF: text file")
check("Standardgrund bleibt ohne Detail", A.ocr_detail("leer", "leere Datei (0 Byte)") == "" and A.ocr_detail("leer", "empty file (0 bytes)") == "")
check("Detail alter Grund englisch", A.ocr_detail("kein_pdf", "kein PDF: HTML-Seite") == "not a PDF: HTML page")
check("Meldung Indexlauf alt -> englisch",
      A.meldung_en("PyMuPDF beendet (Rueckgabewert -6). x") == "PyMuPDF ended (exit code -6). x"
      and A.meldung_en("nach 120 s abgebrochen") == "stopped after 120 s"
      and A.meldung_en("Arbeitsprozess beendet beim Start der Datei. Arbeitsprozess antwortet nicht")
      == "Worker process ended when starting the file. worker process does not respond")
os.makedirs(T + "/logs", exist_ok=True)
check("keine Dateiende-Liste -> None", A._dateiende_liste() is None)
open(T + "/logs/pdf_ohne_dateiende.txt", "w").write("# alt\n")
check("alte Dateiende-Liste wird gelesen", A._dateiende_liste().endswith("pdf_ohne_dateiende.txt"))
open(T + "/logs/pdf_missing_eof.txt", "w").write("# neu\n")
check("neue Dateiende-Liste hat Vorrang", A._dateiende_liste().endswith("pdf_missing_eof.txt"))

# G3c: Pfade in der Anzeige mit ~ statt Benutzerverzeichnis
heim = os.path.expanduser("~")
check("anzeige_pfad: Benutzerverzeichnis -> ~", ocr.anzeige_pfad(heim + "/x/y") == "~/x/y" and ocr.anzeige_pfad(heim) == "~"
      and ocr.anzeige_pfad("/opt/daten") == "/opt/daten" and ocr.anzeige_pfad(heim + "x/y") == heim + "x/y")
_alt = A.PDF_BASE_DIR
A.PDF_BASE_DIR = heim + "/Sammlung"
with A.app.test_request_context():
    _ctx = A.inject_pdf_base_dir()
check("Statusseite/Titel zeigen ~ statt des Benutzerverzeichnisses",
      _ctx["pdf_base_dir_anzeige"] == "~/Sammlung" and _ctx["pdf_base_dir"] == heim + "/Sammlung")
A.PDF_BASE_DIR = _alt
seite = A.app.test_client().get("/status/panel").get_data(as_text=True)
check("Statusseite ohne Benutzerverzeichnis im OCR-Ordner", heim + "/" not in seite or not A.OCR_ORDNER.startswith(heim), A.OCR_ORDNER)

# 5. Werkzeuge
r = subprocess.run([sys.executable, "score_contrast.py", "--help"], capture_output=True, text=True, cwd=T)
check("score_contrast --help englisch", "COMPLETE hit list" in r.stdout and "do not open the browser" in r.stdout, r.stdout[:300])
open(T + "/leer.json", "w").write("[]")
r = subprocess.run([sys.executable, "score_contrast.py", "leer.json", "--no-open"], capture_output=True, text=True, cwd=T)
check("score_contrast Fehler englisch", r.returncode == 1 and "cannot read input - Empty score list" in r.stderr, r.stderr)
r = subprocess.run([sys.executable, "tools/check_index.py"], capture_output=True, text=True, cwd=T)
check("check_index englisch", r.returncode == 0 and "RESULT: all fine" in r.stdout, r.stdout[-300:] + r.stderr[-300:])
r = subprocess.run([sys.executable, "tools/pdf_dateiende.py"], capture_output=True, text=True, cwd=T)
check("pdf_dateiende schreibt pdf_missing_eof.txt", r.returncode == 0 and "PDFs checked: 4" in r.stdout
      and open(T + "/logs/pdf_missing_eof.txt").read().startswith("# PDFs without end of file"), r.stdout + r.stderr[-300:])
r = subprocess.run([sys.executable, "tools/backup_index.py", BASE + "/bk"], capture_output=True, text=True, cwd=T)
check("backup_index englisch", r.returncode == 0 and "Backed up to:" in r.stdout, r.stdout + r.stderr[-300:])

print(f"\nERGEBNIS: {sum(res)}/{len(res)} bestanden"); shutil.rmtree(BASE, ignore_errors=True); sys.exit(0 if all(res) else 1)

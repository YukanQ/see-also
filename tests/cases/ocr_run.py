# Test case: OCR run (ocr.py) with a fake ocrmypdf: order, statuses, retries, cancel, crash, reasons, problem pages.
# Run: python3 ocr_run.py <project folder> [output file]  (or via pytest, see tests/README.md)
import sys, os, json, shutil, tempfile, time, subprocess, threading, fcntl, sqlite3
src = os.path.realpath(sys.argv[1]); BASE = os.path.realpath(tempfile.mkdtemp(prefix="sa_o1_"))
R = BASE + "/daten"; os.makedirs(R + "/A/unter")
SCH = BASE + "/schatten"; LOGS = BASE + "/logs"; os.makedirs(LOGS)
T = BASE + "/proj"; os.makedirs(T)
for f in ["app.py", "indexer.py", "embedder.py", "score_contrast.py", "extract_worker.py", "speicher.py", "ocr.py"]:
    shutil.copy(os.path.join(src, f), T)
shutil.copytree(os.path.join(src, "templates"), T + "/templates")
BIN = BASE + "/bin"; os.makedirs(BIN)
open(BIN + "/ocrmypdf", "w").write("""#!/usr/bin/env python3
import sys, shutil, os, time
a = sys.argv[1:]
if a == ["--version"]: print("16.0-attrappe"); sys.exit(0)
src, dst = a[-2], a[-1]
open(os.environ["OCR_ARGS_LOG"], "a").write(" ".join(a) + "\\n")
n = os.path.basename(src)
if n.startswith("fehler"): sys.stderr.write("ERROR: kaputtes Bild (Attrappe)\\n"); sys.exit(2)
if n.startswith("lang"): time.sleep(30)
if n.startswith("bremse"): time.sleep(3)
if n.startswith("par"): time.sleep(1)
if n.startswith("vortext") and "--force-ocr" not in a:
    sys.stderr.write("PriorOcrFoundError: page already has text! - aborting\\n"); sys.exit(6)
if n.startswith("buero") and "--force-ocr" not in a:
    sys.stderr.write("that the PDF was generated from an office document and does\\n"); sys.exit(2)
if n.startswith("pdfium") and "ghostscript" not in a:
    sys.stderr.write('raise PdfiumError("Failed to fill bitmap rectangle.")\\n'); sys.exit(15)
if n.startswith("immerfehl"):
    sys.stderr.write("PriorOcrFoundError: page already has text!\\n"); sys.exit(6)
shutil.copy(src, dst)
""")
os.chmod(BIN + "/ocrmypdf", 0o755)
open(BIN + "/gs", "w").write("#!/bin/sh\necho 10.07.1\n"); os.chmod(BIN + "/gs", 0o755)
# tesseract als Attrappe (Sprachen eng deu fra osd), damit der Test ohne installiertes Tesseract laeuft
open(BIN + "/tesseract", "w").write("#!/bin/sh\nif [ \"$1\" = --list-langs ]; then printf 'List of available languages (4):\\ndeu\\neng\\nfra\\nosd\\n'; else echo 'tesseract 5.5.0'; fi\n")
os.chmod(BIN + "/tesseract", 0o755)
ARGS = BASE + "/args.log"
ENV = dict(os.environ, PATH=BIN + ":" + os.environ["PATH"], SEE_ALSO_TEST_ATTRAPPE="1", OCR_ARGS_LOG=ARGS)

def w(rel, data):
    p = os.path.join(R, rel); os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "wb").write(data)
w("A/a1.pdf", b"x")                 # 1 Seite
w("A/unter/a3.pdf", b"xxx")         # 3 Seiten
w("A/bild.pdf", b"KEINTEXT--")      # 10 Seiten, OCR ohne Text
w("A/verschl.pdf", b"xx")
w("A/kaputt.pdf", b"xx")
w("A/leer.pdf", b"")
w("A/fehler.pdf", b"xxxxx")         # 5 Seiten
w("A/lang.pdf", b"x" * 20)          # 20 Seiten, Zeitlimit
w("A/gross.pdf", b"y" * 40)         # 40 Seiten
sys.path.insert(0, T)
import speicher
DB = BASE + "/see_also.db"
speicher.anlegen(DB).close()
LOCK = BASE + "/indexlauf.lock"
res = []
def check(n, ok, d=""): ok = bool(ok); res.append(ok); print("ok    " if ok else "FEHLER", n, d, flush=True)

def kand():
    out = []
    for wz, _, fs in os.walk(R):
        for f in fs:
            out.append(os.path.relpath(os.path.join(wz, f), R))
    return sorted(out)

def lauf(anzahl=None, cancel_nach=None, kandidaten=None, zmin=0.5, parallel=1):
    a = dict(base=R, db=DB, ordner=SCH, sprachen="eng+deu+fra", kandidaten=kandidaten or kand(), anzahl=anzahl,
             log_dir=LOGS, lock_datei=LOCK, nice=0, overlap=0, zeitlimit_je_seite_s=0.05, zeitlimit_min_s=zmin, parallel=parallel)
    p = subprocess.Popen([sys.executable, T + "/ocr.py"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, env=ENV)
    p.stdin.write((json.dumps(a) + "\n").encode()); p.stdin.flush()
    zeilen, ende, prog = [], None, []
    def lesen():
        nonlocal ende
        for raw in p.stdout:
            k, _, pl = raw.decode().rstrip("\n").partition(" ")
            if k == "ZEILE": zeilen.append(pl)
            elif k == "PROGRESS": prog.append(json.loads(pl))
            elif k == "ENDE": ende = json.loads(pl)
    t = threading.Thread(target=lesen); t.start()
    if cancel_nach:
        while not (prog and prog[-1].get("current_file", "") and cancel_nach in str(prog[-1].get("current_file"))):
            time.sleep(0.05)
            if p.poll() is not None: break
        time.sleep(0.5)
        p.stdin.write(b"CANCEL\n"); p.stdin.flush()
    t.join(60); p.wait(60)
    return ende, zeilen, p.returncode

def tab():
    con = sqlite3.connect(DB)
    r = {p: (s, g, c, v) for p, s, g, c, v in con.execute("SELECT path,status,grund,chunks,versuche FROM ocr_dateien")}
    con.close(); return r

# 1. Zwei Dateien
ende, zeilen, rc = lauf(anzahl=2)
check("Lauf 1 endet sauber", ende and ende["phase"] == "done", str(ende))
t = tab()
check("leer/verschluesselt/unlesbar beim Pruefen erkannt, ohne OCR",
      t["A/leer.pdf"][0] == "leer" and t["A/verschl.pdf"][0] == "verschluesselt" and t["A/kaputt.pdf"][0] == "unlesbar", str(t))
check("die zwei kleinsten zuerst erfasst (a1 1 S., a3 3 S.)",
      t["A/a1.pdf"][0] == "erfasst" and t["A/unter/a3.pdf"][0] == "erfasst" and t["A/fehler.pdf"][0] == "offen", str(t))
check("Schattendateien unter gleichem Pfad", os.path.exists(SCH + "/A/a1.pdf") and os.path.exists(SCH + "/A/unter/a3.pdf"))
check("Original unveraendert", open(R + "/A/a1.pdf", "rb").read() == b"x")
args = open(ARGS).read()
check("ocrmypdf mit eng+deu+fra, --output-type pdf, --rotate-pages",
      "--language eng+deu+fra" in args and "--output-type pdf" in args and "--rotate-pages" in args, args.splitlines()[0])
check("Log-Datei geschrieben", ende["log_file"] and os.path.exists(LOGS + "/" + ende["log_file"]))
check("Live-Zeilen enthalten Ergebnis je Datei", any("recognized: A/a1.pdf" in z for z in zeilen), zeilen[-3:])

# 2. Rest
ende, zeilen, rc = lauf()
t = tab()
check("Lauf 2: bild -> kein_text, keine Schattendatei", t["A/bild.pdf"][0] == "kein_text" and not os.path.exists(SCH + "/A/bild.pdf"))
check("fehler -> fehler mit Meldung", t["A/fehler.pdf"][0] == "fehler" and "Exit 2" in (t["A/fehler.pdf"][1] or "") and "Attrappe" in t["A/fehler.pdf"][1], str(t["A/fehler.pdf"]))
check("lang -> zeitlimit, keine halbe Datei", t["A/lang.pdf"][0] == "zeitlimit" and not os.path.exists(SCH + "/A/lang.pdf.ocr-tmp.pdf"), str(t["A/lang.pdf"]))
check("gross erfasst", t["A/gross.pdf"][0] == "erfasst")
liste = open(LOGS + "/ocr_not_recognized.txt").read()
check("ocr_not_recognized.txt mit 6 Eintraegen", liste.count("\n") == 2 + 6, liste)
check("Reihenfolge nach Seiten: bild(10) vor lang(20) vor gross(40)",
      [z for z in zeilen if "bild.pdf" in z and "/" in z.split()[1]] and
      min(i for i, z in enumerate(zeilen) if "A/bild.pdf" in z) < min(i for i, z in enumerate(zeilen) if "A/lang.pdf" in z) <
      min(i for i, z in enumerate(zeilen) if "A/gross.pdf" in z))

# 3. Nichts mehr zu tun
open(ARGS, "w").close()
ende, zeilen, rc = lauf()
check("Lauf 3: keine OCR mehr, nichts erneut versucht", ende["phase"] == "done" and ende["total"] == 0 and open(ARGS).read() == "", str(ende))

# 4. Original geaendert -> neu
time.sleep(0.05); w("A/a1.pdf", b"xy")
ende, zeilen, rc = lauf()
t = tab()
check("geaendertes Original neu erfasst", t["A/a1.pdf"][0] == "erfasst" and open(SCH + "/A/a1.pdf", "rb").read() == b"xy")
time.sleep(0.05); w("A/fehler.pdf", b"ok_jetzt")   # Datei geaendert, Name bleibt fehler* -> wieder fehler, aber neu versucht
open(ARGS, "w").close(); lauf()
check("Datei auf der Liste wird nach Aenderung neu versucht", "fehler.pdf" in open(ARGS).read())

# 5. Original geloescht -> Schatten weg
os.remove(R + "/A/unter/a3.pdf")
ende, zeilen, rc = lauf()
t = tab()
check("verwaiste OCR-Fassung und Zeile entfernt", "A/unter/a3.pdf" not in t and not os.path.exists(SCH + "/A/unter/a3.pdf"))
check("Aufraeumen im Log gemeldet", any("without original" in z for z in zeilen))

# 6. Abbruch mitten in einer Datei
w("A/bremse.pdf", b"z")
ende, zeilen, rc = lauf(cancel_nach="bremse", zmin=20)
t = tab()
check("Abbruch: Phase cancelled", ende and ende["phase"] == "cancelled", str(ende))
check("Abbruch: Datei wieder offen, keine halbe Datei", t["A/bremse.pdf"][0] == "offen" and
      not os.path.exists(SCH + "/A/bremse.pdf.ocr-tmp.pdf") and not os.path.exists(SCH + "/A/bremse.pdf"), str(t["A/bremse.pdf"]))
ende, zeilen, rc = lauf(zmin=20)
check("naechster Lauf macht weiter", tab()["A/bremse.pdf"][0] == "erfasst")

# 7. Sperre gegen Indexlauf
fd = os.open(LOCK, os.O_RDWR); fcntl.flock(fd, fcntl.LOCK_EX)
ende, zeilen, rc = lauf()
check("Sperre: Lauf verweigert", ende and ende["phase"] == "error" and "already in progress" in ende["error"], str(ende))
fcntl.flock(fd, fcntl.LOCK_UN); os.close(fd)

# 8. Absturz beim Pruefen
w("A/absturz.pdf", b"zz")
e1, _, rc1 = lauf()
check("Absturz: kein ENDE, Prozess tot", e1 is None and rc1 != 0, str(rc1))
e2, _, rc2 = lauf()
check("2. Lauf: erneuter Versuch, wieder Absturz", e2 is None)
e3, z3, rc3 = lauf()
t = tab()
check("3. Lauf: Datei als absturz vermerkt, Lauf laeuft durch", e3 and e3["phase"] == "done" and t["A/absturz.pdf"][0] == "absturz", str(t.get("A/absturz.pdf")))
e4, _, _ = lauf()
check("4. Lauf: Absturz-Datei bleibt liegen", e4 and e4["phase"] == "done")

# 9. Werkzeug fehlt
ENV2 = dict(ENV, PATH="/usr/bin:/bin")
a = dict(base=R, db=DB, ordner=SCH, kandidaten=kand(), anzahl=None, log_dir=LOGS, lock_datei=LOCK, nice=0)
p = subprocess.run([sys.executable, T + "/ocr.py"], input=(json.dumps(a) + "\n").encode(), capture_output=True, env=ENV2)
end = [l for l in p.stdout.decode().splitlines() if l.startswith("ENDE ")]
check("ocrmypdf fehlt: klare Meldung", end and "brew install ocrmypdf" in end[0], end)

# 9b. O1b: genauere Gruende
w("A/befund_abgeschnitten.pdf", b"%PDF-1.3\n" + b"x" * 5000)
w("A/befund_null.pdf", b"%PDF-1.3\n" + b"\0" * 5000)
w("A/befund_apple.pdf", b"\x00\x05\x16\x07" + b"\0" * 40 + b"Mac OS X")
w("A/befund_text.pdf", b"vti_encoding:SR|utf8-nl\r\n")
w("A/befund_html.pdf", b"<!DOCTYPE html><html><body>x</body></html>")
w("A/befund_ende.pdf", b"%PDF-1.7\n" + b"x" * 300 + b"\n%%EOF\n")
lauf()
t = tab()
check("abgeschnitten erkannt", t["A/befund_abgeschnitten.pdf"][0] == "abgeschnitten", str(t["A/befund_abgeschnitten.pdf"]))
check("ausgenullt erkannt", t["A/befund_null.pdf"][0] == "ausgenullt")
check("AppleDouble als kein PDF", t["A/befund_apple.pdf"][0] == "kein_pdf" and "AppleDouble" in t["A/befund_apple.pdf"][1])
check("Textdatei als kein PDF", t["A/befund_text.pdf"][0] == "kein_pdf" and "text file" in t["A/befund_text.pdf"][1])
check("HTML als kein PDF", "HTML" in t["A/befund_html.pdf"][1], str(t["A/befund_html.pdf"]))
check("Dateiende vorhanden -> unlesbar mit Hinweis", t["A/befund_ende.pdf"][0] == "unlesbar" and "end of file present" in t["A/befund_ende.pdf"][1])
# alte O1-Zeile "keine Seiten" wird einmalig neu geprueft
con = sqlite3.connect(DB); con.execute("UPDATE ocr_dateien SET status='unlesbar', grund='keine Seiten' WHERE path='A/befund_abgeschnitten.pdf'"); con.commit(); con.close()
lauf()
check("O1-Befund 'keine Seiten' neu geprueft", tab()["A/befund_abgeschnitten.pdf"][0] == "abgeschnitten")
# Programmpakete: erst ohne Bestandsliste in die Tabelle bringen
w("A/Tool.app/Contents/Resources/icon.pdf", b"i")
w("A/Blog.rapidweaverplugin/Contents/Resources/r.pdf", b"r")
w("A/Kovarianz.nb/attachments/M354.pdf", b"m")
lauf()
check("ohne Bestandsliste: Paket-Dateien in der Tabelle", "A/Tool.app/Contents/Resources/icon.pdf" in tab())

# 9c. O1e: zweiter Versuch und parallel
w("A/vortext.pdf", b"v"); w("A/buero.pdf", b"b"); w("A/pdfium.pdf", b"p"); w("A/immerfehl.pdf", b"i")
open(ARGS, "w").close()
ende, zeilen, rc = lauf(zmin=20)
t = tab()
check("Exit 6 -> 2. Versuch mit --force-ocr erfasst", t["A/vortext.pdf"][0] == "erfasst" and "force-ocr" in (t["A/vortext.pdf"][1] or ""), str(t["A/vortext.pdf"]))
check("office document -> 2. Versuch erfasst", t["A/buero.pdf"][0] == "erfasst")
check("PdfiumError -> Ghostscript erfasst", t["A/pdfium.pdf"][0] == "erfasst" and "ghostscript" in t["A/pdfium.pdf"][1], str(t["A/pdfium.pdf"]))
check("bleibender Fehler: Grund mit Vermerk 2. Versuch", t["A/immerfehl.pdf"][0] == "fehler" and "2nd attempt" in t["A/immerfehl.pdf"][1], str(t["A/immerfehl.pdf"]))
check("Log nennt Ghostscript", any("Ghostscript" in z for z in open(LOGS + "/" + ende["log_file"]).read().splitlines()))
# alte Fehlerzeile ohne Vermerk wird einmal wieder offen
con = sqlite3.connect(DB); con.execute("UPDATE ocr_dateien SET status='fehler', grund='Exit 6: PriorOcrFoundError' WHERE path='A/vortext.pdf'"); con.commit(); con.close()
open(ARGS, "w").close()
ende, zeilen, rc = lauf(zmin=20)
check("alte Fehler mit Abhilfe einmal neu versucht", tab()["A/vortext.pdf"][0] == "erfasst" and "vortext.pdf" in open(ARGS).read()
      and "immerfehl.pdf" not in open(ARGS).read())
for i in range(8): w(f"P/par{i}.pdf", b"x")
t0 = time.time(); ende, zeilen, rc = lauf(zmin=20, parallel=4); dauer = time.time() - t0
t = tab()
check("8 Dateien je 1 s mit 4 parallel", all(t[f"P/par{i}.pdf"][0] == "erfasst" for i in range(8)) and dauer < 6, f"{dauer:.1f} s")
check("Log nennt 4 gleichzeitig", any("4 at a time" in z for z in zeilen))
for i in range(8): w(f"P/par_b{i}.pdf", b"xy")
p_ = None
ende, zeilen, rc = lauf(zmin=20, parallel=4, cancel_nach="par_b")
t = tab()
check("Abbruch parallel: keine halben Dateien, abgebrochene wieder offen",
      ende["phase"] == "cancelled" and not any(f.endswith(".ocr-tmp.pdf") for _, _, fs in os.walk(SCH) for f in fs)
      and all(t[f"P/par_b{i}.pdf"][0] in ("offen", "erfasst") for i in range(8)), str({k: v[0] for k, v in t.items() if "par_b" in k}))
ende, zeilen, rc = lauf(zmin=20, parallel=4)
check("danach alle par_b erfasst", all(tab()[f"P/par_b{i}.pdf"][0] == "erfasst" for i in range(8)))

# 10. Server: Routen, Statusseite, gegenseitige Sperre
json.dump(dict(pdf_base_dir=R, ocr_ordner=SCH, ocr_zeitlimit_je_seite_s=0.05, ocr_zeitlimit_min_s=20,
               indexlauf_eigener_prozess=True), open(T + "/config.json", "w"))
shutil.copy(DB, T + "/see_also.db")
os.environ.update(ENV)
import types; sys.modules["fitz"] = types.ModuleType("fitz")
os.chdir(T)
import app as A
# G1c: "ohne Text" zaehlt nur, was ein Indexlauf in diesem Stand gelesen hat
_con = speicher.verbinden(A.DB_FILE); _z = speicher.zustand_lesen(_con)
_z["last_run_snapshot"] = A.scan_pdfs(A.PDF_BASE_DIR); speicher.zustand_ersetzen(_con, _z); _con.commit(); _con.close()
A.bestand_verwerfen(); A._zustand_cache.update(gen=None, state=None)
c = A.app.test_client()
st = c.get("/status/panel")
html = st.data.decode()
check("Statusseite rendert mit OCR-Bereich", st.status_code == 200 and "Run OCR" in html and "Files to review" in html)
check("Zeile OCR-Fassungen statt remasterd", "OCR versions" in html and "remasterd" not in html)
check("fehlende Dateien mit OCR-Zustand", "OCR done" in html and "no result" in html)
w("A/bremse2.pdf", b"zzzz")
os.rename(R + "/A/bremse2.pdf", R + "/A/bremse_b.pdf")
A.bestand_verwerfen()
check("G1c: neue, noch nicht gelesene Datei zaehlt nicht als 'ohne Text'", "A/bremse_b.pdf" not in A.find_missing_files())
_con = speicher.verbinden(A.DB_FILE); _z = speicher.zustand_lesen(_con)   # wie nach einem Indexlauf
_z["last_run_snapshot"] = A.scan_pdfs(A.PDF_BASE_DIR); speicher.zustand_ersetzen(_con, _z); _con.commit(); _con.close()
A.bestand_verwerfen(); A._zustand_cache.update(gen=None, state=None)
check("G1c: nach dem Lesen durch den Index zaehlt sie", "A/bremse_b.pdf" in A.find_missing_files())
r = c.post("/ocr/lauf/start", json={"anzahl": "x"}); check("ungueltige Anzahl abgelehnt", r.status_code == 400)
r = c.post("/ocr/lauf/start", json={"anzahl": None}); check("Start alle", r.json["started"])
r2 = c.post("/index/start/incremental"); check("Indexlauf waehrend OCR abgelehnt", r2.status_code == 409 and r2.json["reason"] == "ocr_running")
r3 = c.post("/ocr/lauf/start", json={"anzahl": 1}); check("zweiter OCR-Lauf abgelehnt", r3.status_code == 409)
for _ in range(200):
    s = c.get("/ocr/lauf/status").json
    if not s["is_running"]: break
    time.sleep(0.1)
check("Serverlauf fertig, Zeilen vorhanden", s["phase"] == "done" and s["zeilen"], str(s)[:300])
check("bremse_b erfasst", os.path.exists(SCH + "/A/bremse_b.pdf"))
A.INDEX_PROGRESS["is_running"] = True
r = c.post("/ocr/lauf/start", json={}); check("OCR waehrend Indexlauf abgelehnt", r.status_code == 409 and r.json["reason"] == "index_running")
A.INDEX_PROGRESS["is_running"] = False
html = c.get("/status").data.decode()
sc = A.scan_pdfs(R)
DB = T + "/see_also.db"
check("Scan ueberspringt .app und Plug-in mit Contents", not any(".app/" in p or "rapidweaverplugin" in p for p in sc), [p for p in sc if "Contents" in p])
check("Ordner mit Endung ohne Contents bleibt (.nb)", "A/Kovarianz.nb/attachments/M354.pdf" in sc)
t = tab()
check("Serverlauf raeumt Paket-Zeilen weg", "A/Tool.app/Contents/Resources/icon.pdf" not in t and "A/Blog.rapidweaverplugin/Contents/Resources/r.pdf" not in t
      and not os.path.exists(SCH + "/A/Tool.app/Contents/Resources/icon.pdf"))
check("Statusliste zeigt neue Gruende", "Truncated, no pages" in c.get("/problemfaelle").data.decode())
# Problemfaelle
os.makedirs(T + "/logs", exist_ok=True)
w("A/teil.pdf", b"%PDF-1.4\nseiten")
con = sqlite3.connect(T + "/see_also.db")
con.execute("INSERT INTO chunks(id, path, position_in_file, seite, text, text_for_embedding) VALUES (999001, 'A/teil.pdf', 0, 1, 'x', 'x')")
con.commit(); con.close()
A.LOG_DIR = T + "/logs"
open(T + "/logs/pdf_ohne_dateiende.txt", "w").write("# k\n# k\n0\t10\tA/befund_abgeschnitten.pdf\n1\t15\tA/teil.pdf\n")
A._teilweise_abgeschnitten.__globals__["_db_zugriff"]  # vorhanden
A.bestand_verwerfen()   # P1: neu angelegte Testdatei sofort im Bestand
d = A.problemfaelle_daten()
keys = [g["key"] for g in d["gruppen"]]
check("Problemfaelle: Gruppen in fester Reihenfolge", keys[:2] == ["abgeschnitten", "ausgenullt"] and keys.index("kein_pdf") < keys.index("unlesbar"), keys)
check("kein_text nicht unter 'zu entscheiden'", "kein_text" not in keys and "A/bild.pdf" in d["ohne_text"], keys)
r = c.get("/ohne-text"); h = r.data.decode()
check("Seite /ohne-text nach Bereichen", r.status_code == 200 and "<summary>A <span" in h and 'data-path="A/bild.pdf"' in h, h[-600:])
check("Problemfaelle: keine Paket-Dateien", not any(".app/" in z["path"] for g in d["gruppen"] for z in g["zeilen"]))
gt = [g for g in d["gruppen"] if g["key"] == "index_teilweise"]
check("Problemfaelle: teil.pdf mit Chunk unter 'im Index'", gt and gt[0]["zeilen"][0]["path"] == "A/teil.pdf" and "1 passages" in gt[0]["zeilen"][0]["detail"], gt)
check("Problemfaelle: abgeschnittene ohne Chunks nicht doppelt unter 'im Index'", all(z["path"] != "A/befund_abgeschnitten.pdf" for z in gt[0]["zeilen"]))
r = c.get("/problemfaelle"); h = r.data.decode()
check("Seite /problemfaelle rendert", r.status_code == 200 and "Truncated, no pages" in h and 'data-path="A/befund_abgeschnitten.pdf"' in h and "finder(this)" in h)
check("Knopf 'Befehl kopieren' mit Basis und Papierkorb", "befehlKopieren(this, &#34;" + R in h and "~/.Trash/" in h)
check("Statusseite verlinkt Problemfaelle", "/problemfaelle" in c.get("/status/panel").data.decode())
import subprocess as _sp
aufrufe = []
_orig = _sp.run
class _R:
    def __init__(self, rc): self.returncode = rc; self.stderr = "Fehler (Attrappe)"
rc_wert = [0]
A.subprocess.run = lambda cmd, **k: (aufrufe.append(cmd), _R(rc_wert[0]))[1]
r = c.post("/reveal-in-finder?path=" + "A/befund_null.pdf")
check("Finder-Knopf: AppleScript, vorderes Fenster", r.json["ok"] and aufrufe[-1][0] == "osascript"
      and aufrufe[-1][-2] == os.path.realpath(R + "/A/befund_null.pdf") and "set target of Finder window 1 to o" in aufrufe[-1], aufrufe[-1][-2:])
rc_wert[0] = 1
r = c.post("/reveal-in-finder?path=" + "A/befund_null.pdf")
check("Finder: Rueckfall auf open -R", r.json["ok"] and r.json["weg"] == "open -R" and aufrufe[-1][:2] == ["open", "-R"])
A.subprocess.run = _orig
check("status.html enthaelt OCR-JS", "startOcrLauf" in html and "/ocr/start" not in html)

print(f"\n{sum(res)} von {len(res)} ok")
shutil.rmtree(BASE, ignore_errors=True)
sys.exit(0 if all(res) else 1)

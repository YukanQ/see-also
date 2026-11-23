# Test case: Embedding text from OCR versions; OCR tag and OCR version route.
# Run: python3 ocr_embedding.py <project folder> [output file]  (or via pytest, see tests/README.md)
import sys, os, json, shutil, tempfile, time, types, hashlib, sqlite3
src = os.path.realpath(sys.argv[1]); BASE = os.path.realpath(tempfile.mkdtemp(prefix="sa_o2_"))
R = BASE + "/daten"; os.makedirs(R + "/A"); SCH = BASE + "/schatten"; os.makedirs(SCH + "/A")
open(f"{R}/A/scan1.pdf", "wb").write(b"%PDF LEER 1")
open(f"{R}/A/scan2.pdf", "wb").write(b"%PDF LEER 2")
open(f"{R}/A/normal.pdf", "wb").write(b"%PDF TEXT")
T = BASE + "/proj"; os.makedirs(T)
for f in ["app.py", "indexer.py", "embedder.py", "score_contrast.py", "extract_worker.py", "speicher.py", "ocr.py"]: shutil.copy(os.path.join(src, f), T)
shutil.copytree(os.path.join(src, "templates"), T + "/templates"); shutil.copytree(os.path.join(src, "tools"), T + "/tools")
json.dump(dict(pdf_base_dir=R, ocr_ordner=SCH, embed_batch_size=2, pdf_isoliert_lesen=False, indexlauf_eigener_prozess=False), open(T + "/config.json", "w"))
sys.modules["fitz"] = types.ModuleType("fitz"); sys.path.insert(0, T); os.chdir(T)
import numpy as np, indexer, embedder
def vec(t):
    r = np.random.default_rng(int(hashlib.md5(t.encode()).hexdigest()[:8], 16)); v = r.standard_normal(1024).astype(np.float32); return v / np.linalg.norm(v)
def extract(p, o=0):
    b = open(p, "rb").read()
    if b"LEER" in b: return []
    n = 2 if b"OCR" in b else 1
    return [dict(seite=i + 1, zeile=1, text=f"{b.decode()} s{i}", text_for_embedding=f"e {p} {i}", bbox=None) for i in range(n)]
indexer.extract_page_chunks = extract
embedder.embed_passages = lambda ts: np.array([vec(t) for t in ts], dtype=np.float32)
embedder.embed_query = lambda t: vec(t)
res = []
def check(n, ok, d=""): ok = bool(ok); res.append(ok); print("ok    " if ok else "FEHLER", n, d, flush=True)
import app as A, speicher, ocr
def letztes_log():
    return open(sorted(os.path.join(T, "logs", f) for f in os.listdir(T + "/logs") if f.startswith("index_"))[-1]).read()
def chunks(p):
    con = sqlite3.connect(T + "/see_also.db"); r = con.execute("SELECT text FROM chunks WHERE path=?", (p,)).fetchall(); con.close(); return [x[0] for x in r]

A.run_indexing_guarded("full")
check("Komplettlauf fertig", A.INDEX_PROGRESS["phase"] == "done", A.INDEX_PROGRESS.get("error"))
check("Scans ohne Chunks", chunks("A/scan1.pdf") == [] and len(chunks("A/normal.pdf")) == 1)

con = speicher.verbinden(T + "/see_also.db"); ocr.tabelle_anlegen(con)
for p in ("A/scan1.pdf", "A/scan2.pdf"):
    con.execute("INSERT INTO ocr_dateien(path, groesse, mtime, seiten, status) VALUES (?,?,?,?, 'erfasst')",
                (p, os.path.getsize(R + "/" + p), os.path.getmtime(R + "/" + p), 1))
con.commit(); con.close()
open(SCH + "/A/scan1.pdf", "wb").write(b"%PDF OCR-Fassung")      # scan2: keine Fassung vorhanden

st = A.build_status()
check("Status: 1 OCR-Fassung zum Einbetten", st["ocr_einzubetten"] == 1, st["ocr_einzubetten"])
html = A.app.test_client().get("/status/panel").data.decode()
check("Statusseite zeigt '+ 1 OCR-Fassungen zum Einbetten'", ", 1 OCR versions" in html)

A.run_indexing_guarded("incremental")
log = letztes_log()
check("Nachindizieren: Lauf fertig", A.INDEX_PROGRESS["phase"] == "done", A.INDEX_PROGRESS.get("error"))
check("scan1 aus OCR-Fassung, Chunks am Original", chunks("A/scan1.pdf") and all("OCR-Fassung" in t for t in chunks("A/scan1.pdf")), chunks("A/scan1.pdf"))
check("Log: (OCR) und Hinweiszeile", "A/scan1.pdf (OCR)" in log and "1 of them with text from the OCR version" in log, log[-500:])
check("normal.pdf nicht erneut gelesen", "normal.pdf" not in log.split("Phase: extract")[-1].split("Phase: merge")[0])
check("scan2 ohne Fassung bleibt ohne Chunks", chunks("A/scan2.pdf") == [])
st = A.build_status()
check("danach 0 zum Einbetten, scan1 nicht mehr fehlend", st["ocr_einzubetten"] == 0 and "A/scan1.pdf" not in st["missing_files"], (st["ocr_einzubetten"], st["missing_files"]))

A.run_indexing_guarded("incremental")
check("zweiter Lauf: nichts zu tun", "(1/" not in letztes_log())

docs, fehler = A.run_search("irgendwas")
d = {x["path"]: x for x in docs}
check("Suche: scan1 mit ocr=True, normal ohne", d.get("A/scan1.pdf", {}).get("ocr") is True and d["A/normal.pdf"]["ocr"] is False, docs)
c = A.app.test_client()
r = c.get("/pdf-ocr/A/scan1.pdf"); check("/pdf-ocr liefert die Fassung", r.status_code == 200 and b"OCR-Fassung" in r.data)
check("/pdf-ocr fremde Pfade 404", c.get("/pdf-ocr/../daten/A/normal.pdf").status_code == 404 and c.get("/pdf-ocr/A/fehlt.pdf").status_code == 404)
check("Suchseite hat Knopf OCR-Fassung", "openOcrVersion()" in c.get("/").data.decode())

# Fassung erneuert (Original unveraendert)
time.sleep(0.05); open(SCH + "/A/scan1.pdf", "wb").write(b"%PDF OCR-Fassung neu"); os.utime(SCH + "/A/scan1.pdf", (time.time() + 5, time.time() + 5))
A.run_indexing_guarded("incremental")
check("erneuerte Fassung neu eingebettet", any("neu" in t for t in chunks("A/scan1.pdf")), chunks("A/scan1.pdf"))

# Original geaendert -> Fassung ungueltig -> Text wieder aus Original (leer)
time.sleep(0.05); open(R + "/A/scan1.pdf", "wb").write(b"%PDF LEER geaendert"); os.utime(R + "/A/scan1.pdf", (time.time() + 9, time.time() + 9))
A.run_indexing_guarded("incremental")
check("Original geaendert: Chunks aus Original (keine), wieder fehlend", chunks("A/scan1.pdf") == [] and "A/scan1.pdf" in A.build_status()["missing_files"])
con = speicher.verbinden(T + "/see_also.db"); q = speicher.zustand_lesen(con).get("ocr_quellen", {}); con.close()
check("ocr_quellen ohne scan1", "A/scan1.pdf" not in q, q)
import subprocess
r = subprocess.run([sys.executable, "tools/check_index.py"], capture_output=True, text=True); check("Index in Ordnung", r.returncode == 0, r.stdout[-300:])
print(f"\nERGEBNIS: {sum(res)}/{len(res)} bestanden"); shutil.rmtree(BASE, ignore_errors=True); sys.exit(0 if all(res) else 1)

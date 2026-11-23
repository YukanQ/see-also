# Test case: Isolated PDF reading: crashing, hanging and broken files become problem files; the run continues.
# Run: python3 problem_files.py <project folder> [output file]  (or via pytest, see tests/README.md)
import sys, os, types, json, hashlib, shutil, tempfile, time, threading
src = os.path.abspath(sys.argv[1]); T = os.path.realpath(tempfile.mkdtemp(prefix="sa_b5b_"))
for f in ["app.py", "indexer.py", "embedder.py", "score_contrast.py", "extract_worker.py", "speicher.py", "ocr.py"]: shutil.copy(os.path.join(src, f), T)
shutil.copytree(os.path.join(src, "templates"), T + "/templates")
R = T + "/daten"; os.makedirs(R + "/Elec/u"); os.makedirs(R + "/Phys"); os.makedirs(R + "/Elec/BOOL\nLOGIC")
for n in ["Elec/a.pdf", "Elec/spricht1.pdf", "Elec/absturz1.pdf", "Elec/b.pdf", "Elec/haengt.pdf", "Elec/kaputt.pdf", "Elec/leer.pdf", "Elec/u/c.pdf", "Phys/p.pdf", "Elec/BOOL\nLOGIC/x\ny.pdf", "Elec/BOOL\nLOGIC/z.pdf"]:
    open(f"{R}/{n}", "wb").write(b"%PDF " + n.encode())
json.dump(dict(pdf_base_dir=R, pdf_zeitlimit_s=4), open(T + "/config.json", "w"))
os.environ["SEE_ALSO_TEST_ATTRAPPE"] = "1"
sys.modules["fitz"] = types.ModuleType("fitz"); sys.path.insert(0, T)
import numpy as np, indexer, embedder
def vec(t):
    r = np.random.default_rng(int(hashlib.md5(t.encode()).hexdigest()[:8], 16)); v = r.standard_normal(1024).astype(np.float32); return v / np.linalg.norm(v)
embedder.embed_passages = lambda ts: np.array([vec(t) for t in ts], dtype=np.float32)
embedder.embed_query = vec
res = []
def check(n, ok, d=""): res.append(ok); print("ok    " if ok else "FEHLER", n, d)
import app as A; import speicher as _sp; A.load_meta = lambda: list(_sp.alle_chunks(_sp.verbinden(A.DB_FILE, nur_lesen=True)))
t0 = time.monotonic(); A.run_indexing_guarded("full"); dt = time.monotonic() - t0
p = A.INDEX_PROGRESS
check("Lauf endet trotz Absturz und Haenger", p["phase"] == "done" and p["error"] is None, str((p["phase"], p["error"])))
check("Dauer ~ Zeitlimit (Haenger nach 4 s abgebrochen)", dt < 20, f"{dt:.1f} s")
m = A.load_meta()
check("lesbare Dateien im Index, auch die mit MuPDF-Meldungen und Zeilenumbruch im Namen", sorted({e["path"] for e in m}) == sorted(["Elec/a.pdf", "Elec/b.pdf", "Elec/spricht1.pdf", "Elec/u/c.pdf", "Phys/p.pdf", "Elec/BOOL\nLOGIC/x\ny.pdf", "Elec/BOOL\nLOGIC/z.pdf"]) and len(m) == 14, str(sorted({e["path"] for e in m})))
from urllib.parse import quote
cl = A.app.test_client()
check("PDF mit Zeilenumbruch im Pfad laesst sich oeffnen", cl.get("/pdf/" + quote("Elec/BOOL\nLOGIC/x\ny.pdf")).status_code == 200 and cl.get("/pdf/Elec/a.pdf").status_code == 200)
check("Chunk-Liste fuer Pfad mit Zeilenumbruch", len(cl.get("/documents/chunks?path=" + quote("Elec/BOOL\nLOGIC/z.pdf")).get_json()) == 2)
check("jeder Chunk gehoert zur richtigen Datei (Text nennt den eigenen Dateinamen)", all(e["text"].startswith(os.path.basename(e["path"]) + " S") for e in m))
check("bbox aus dem Arbeitsprozess als Liste gespeichert", m[0]["bbox"] == [0.0, 1.0, 2.0, 3.0])
st = A.load_index_state()
pr = st["problemdateien"]
check("Problemdateien erfasst mit Befund", {k: v["befund"] for k, v in pr.items()} == {"Elec/absturz1.pdf": "absturz", "Elec/haengt.pdf": "zeitlimit", "Elec/kaputt.pdf": "fehler"}, str(pr))
check("Absturz-Meldung nennt Rueckgabewert", "exit code" in pr["Elec/absturz1.pdf"]["meldung"], pr["Elec/absturz1.pdf"]["meldung"])
log = open(T + "/logs/" + sorted(f for f in os.listdir(T + "/logs") if f.startswith("index_"))[-1]).read()
check("Log: [ABSTURZ], [ZEITLIMIT], [FEHLER]", "[CRASH] Elec/absturz1.pdf" in log and "[TIME LIMIT] Elec/haengt.pdf" in log and "[READ ERROR] Elec/kaputt.pdf" in log)
s = A.build_status()
check("Status: Problemdateien getrennt, Fehlende nur leer.pdf", [f["path"] for f in s["problem_files"]] == ["Elec/absturz1.pdf", "Elec/haengt.pdf", "Elec/kaputt.pdf"] and s["missing_files"] == ["Elec/leer.pdf"], str((s["problem_files"], s["missing_files"])))
c = A.app.test_client(); panel = c.get("/status/panel").get_data(as_text=True)
check("Statusseite: Block Problemdateien vor Fehlende Dateien", panel.index("Files that could not be read: 3") < panel.index("PDFs without text: 1") and "Crash" in panel and "Time limit" in panel and "Read error" in panel)
check("neu=0 nach dem Lauf (Problemdateien nicht erneut faellig)", s["neu_seit_letzter_indizierung"] == 0)
t0 = time.monotonic(); A.run_indexing_guarded("incremental"); dt2 = time.monotonic() - t0
log = open(T + "/logs/" + sorted(f for f in os.listdir(T + "/logs") if f.startswith("index_"))[-1]).read()
check("zweiter Lauf versucht Problemdateien nicht erneut", "PDFs to process: 0" in log and dt2 < 5 and len(A.load_index_state()["problemdateien"]) == 3)
# Datei repariert (umbenannt): Problem verschwindet
os.rename(f"{R}/Elec/kaputt.pdf", f"{R}/Elec/repariert.pdf"); time.sleep(1.1)
os.utime(f"{R}/Elec/haengt.pdf", None)
A.run_indexing_guarded("incremental")
pr = A.load_index_state()["problemdateien"]
check("umbenannte Datei faellt aus der Liste, geaenderte wird neu versucht (wieder Zeitlimit)", set(pr) == {"Elec/absturz1.pdf", "Elec/haengt.pdf"} and "Elec/repariert.pdf" in {e["path"] for e in A.load_meta()}, str(pr))
# Abbruch mitten in einem haengenden PDF
time.sleep(1.1); os.utime(f"{R}/Elec/haengt.pdf", None)
def _dbsnap():
    c = _sp.verbinden(A.DB_FILE, nur_lesen=True); r = (list(_sp.alle_chunks(c)), _sp.generation(c)); c.close(); return r
good = _dbsnap()
th = threading.Thread(target=A.run_indexing_guarded, args=("incremental",)); t0 = time.monotonic(); th.start()
time.sleep(1.5); A.INDEX_CANCEL.set(); th.join(20)
check("Abbruch mitten in einer Datei wirkt in < 3 s, Index unveraendert", not th.is_alive() and A.INDEX_PROGRESS["phase"] == "cancelled" and time.monotonic() - t0 < 4 and _dbsnap() == good, f"{time.monotonic()-t0:.1f}")
A.INDEX_CANCEL.clear()
import subprocess
# nur Arbeitsprozesse dieses Testordners (andere see_also-Laeufe auf dem Rechner zaehlen nicht);
# bis 3 s Zeit fuer das Aufraeumen
for _ in range(30):
    leftover = subprocess.run(["pgrep", "-f", T + "/extract_worker.py"], capture_output=True, text=True).stdout.split()
    if not leftover:
        break
    time.sleep(0.1)
check("kein Arbeitsprozess bleibt uebrig", leftover == [], str(leftover))
# Ausschluss entfernt Problemdateien
json.dump(dict(pdf_base_dir=R, pdf_zeitlimit_s=4, ausgeschlossen=["Elec"]), open(T + "/config.json", "w"))
sys.modules.pop("app"); import app as A2
check("ausgeschlossener Bereich: Problemdateien nicht mehr angezeigt", A2.build_status()["problem_files"] == [])
A2.run_indexing_guarded("incremental")
check("... und nach dem Lauf aus dem Zustand entfernt", A2.load_index_state()["problemdateien"] == {})
print(f"\nERGEBNIS: {sum(res)}/{len(res)} bestanden"); sys.exit(0 if all(res) else 1)

# Test case: Caching of the folder scan and of the index state; status page refresh only while visible.
# Run: python3 caching.py <project folder> [output file]  (or via pytest, see tests/README.md)
import sys, os, json, shutil, tempfile, time, types
src = os.path.realpath(sys.argv[1]); BASE = os.path.realpath(tempfile.mkdtemp(prefix="sa_p1_"))
R = BASE + "/daten"; os.makedirs(R + "/A")
for n in range(5): open(f"{R}/A/d{n}.pdf", "wb").write(b"%PDF x")
T = BASE + "/proj"; os.makedirs(T)
for f in ["app.py", "indexer.py", "embedder.py", "score_contrast.py", "extract_worker.py", "speicher.py", "ocr.py"]: shutil.copy(os.path.join(src, f), T)
shutil.copytree(os.path.join(src, "templates"), T + "/templates")
json.dump(dict(pdf_base_dir=R), open(T + "/config.json", "w"))
sys.modules["fitz"] = types.ModuleType("fitz"); sys.path.insert(0, T); os.chdir(T)
import speicher; speicher.anlegen(T + "/see_also.db").close()
res = []
def check(n, ok, d=""): ok = bool(ok); res.append(ok); print("ok    " if ok else "FEHLER", n, d, flush=True)
import app as A
zaehler = [0]; orig = A.scan_pdfs
def zaehlend(b): zaehler[0] += 1; return orig(b)
A.scan_pdfs = zaehlend
c = A.app.test_client()
c.get("/"); c.get("/status/panel"); c.get("/status/panel"); c.get("/index/badge"); c.get("/problemfaelle")
check("5 Seitenaufrufe, 1 Gang ueber den Baum", zaehler[0] == 1, zaehler[0])
A.bestand_verwerfen(); c.get("/"); check("nach Verwerfen neu eingelesen", zaehler[0] == 2)
A._bestand_cache["zeit"] -= 61; c.get("/"); check("nach 60 s neu eingelesen", zaehler[0] == 3)
st = A.app.test_client().get("/status").data.decode()
check("Statusseite: Auffrischen nur sichtbar", "document.hidden" in st and "visibilitychange" in st)
# Zustand-Cache
n0 = [0]; orig_z = A.speicher.zustand_lesen
def zl(con): n0[0] += 1; return orig_z(con)
A.speicher.zustand_lesen = zl
for _ in range(5): A.load_index_state()
check("Zustand nur einmal gelesen bei gleicher Generation", n0[0] <= 1, n0[0])
con = speicher.verbinden(T + "/see_also.db"); con.execute("UPDATE zustand SET wert='7' WHERE schluessel='generation'") if con.execute("SELECT 1 FROM zustand WHERE schluessel='generation'").fetchone() else con.execute("INSERT INTO zustand VALUES ('generation','7')"); con.commit(); con.close()
A.load_index_state(); check("neue Generation -> neu gelesen", n0[0] == 1, n0[0])
print(f"\nERGEBNIS: {sum(res)}/{len(res)} bestanden"); shutil.rmtree(BASE, ignore_errors=True); sys.exit(0 if all(res) else 1)

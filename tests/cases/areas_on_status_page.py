# Test case: Areas on the status page: counts, hints, order.
# Run: python3 areas_on_status_page.py <project folder> [output file]  (or via pytest, see tests/README.md)
import sys, os, types, json, hashlib, shutil, tempfile, re, glob
src = sys.argv[1]; T = os.path.realpath(tempfile.mkdtemp(prefix="sa_kl_"))
for f in ["app.py", "indexer.py", "embedder.py", "score_contrast.py", "speicher.py", "ocr.py"]: shutil.copy(os.path.join(src, f), T)
shutil.copytree(os.path.join(src, "templates"), T + "/templates")
R = T + "/daten"
for d in ["Physik", "Scans/tief", "Leer/unter", "Privat", ".versteckt"]: os.makedirs(f"{R}/{d}")
for rel in ["Physik/p1.pdf", "Scans/tief/s.pdf", "Privat/x.pdf", ".versteckt/v.txt"]:
    open(f"{R}/{rel}", "wb").write(b"%PDF " + rel.encode())
open(f"{R}/Leer/unter/notiz.md", "w").write("x")
json.dump(dict(pdf_base_dir=R, pdf_isoliert_lesen=False, ausgeschlossen=["Privat"]), open(T + "/config.json", "w"))
sys.modules["fitz"] = types.ModuleType("fitz"); sys.path.insert(0, T)
import numpy as np, indexer, embedder
def vec(t):
    r = np.random.default_rng(int(hashlib.md5(t.encode()).hexdigest()[:8], 16)); v = r.standard_normal(1024).astype(np.float32); return v / np.linalg.norm(v)
def chunks(p, o=0):
    if "Scans" in str(p): return []
    return [dict(seite=1, zeile=1, text="t", text_for_embedding=f"E {p}", bbox=None)]
indexer.extract_page_chunks = chunks
embedder.embed_passages = lambda ts: np.array([vec(t) for t in ts], dtype=np.float32)
embedder.embed_query = vec
res = []
def check(n, ok, d=""): res.append(ok); print("ok    " if ok else "FEHLER", n, d)
import app as A
A.run_indexing_guarded("full"); c = A.app.test_client()
# danach neuer Bereich
os.makedirs(f"{R}/Neu"); open(f"{R}/Neu/n.pdf", "wb").write(b"%PDF n")
d = c.get("/suchbereich").get_json()
check("Bereiche mit Chunks unveraendert", [b["name"] for b in d["bereiche"]] == ["Physik"], str(d["bereiche"]))
w = {x["name"]: x["hinweis"] for x in d["weitere"]}
check("weitere: Leer/Neu/Scans mit Hinweis, ohne Privat und .versteckt",
      w == {"Leer": "no PDFs", "Neu": "not indexed yet", "Scans": "no text"}, str(d["weitere"]))
check("Reihenfolge alphabetisch", [x["name"] for x in d["weitere"]] == ["Leer", "Neu", "Scans"])
# Auswahl/Speichern unberuehrt von grauen Bereichen
r = c.post("/suchbereich", json={"bereiche": ["Physik", "Neu"]}).get_json()
check("graue Bereiche lassen sich nicht in den Suchbereich setzen (nur Physik -> alle)", r["ok"] and r["suchbereich"] is None, str(r))
# Nach Lauf: Neu hat Chunks, verschwindet aus weitere
A.run_indexing_guarded("incremental")
d = c.get("/suchbereich").get_json()
check("nach Lauf: Neu mit Chunks, nicht mehr grau",
      [b["name"] for b in d["bereiche"]] == ["Neu", "Physik"] and "Neu" not in {x["name"] for x in d["weitere"]}, str(d))
logs = sorted(glob.glob(T + "/logs/index_incremental_*.log"))
txt = open(logs[-1], encoding="utf-8").read()
ph = re.findall(r"^\[(\d\d:\d\d:\d\d)\] Phase: (\w+)(?:  \(memory peak[^)]*\))?$", txt, re.M)
check("Log: Phasen scan, extract, merge, embed, save in dieser Reihenfolge",
      [p for _, p in ph][:5] == ["scan", "extract", "merge", "embed", "save"], str(ph))
check("Log: Startzeile vorhanden", txt.startswith("Start incremental indexing: "))
page = c.get("/status").get_data(as_text=True)
check("Statusseite: scopeDirty und grau eingebaut", "scopeDirty" in page and "lab.className = 'grau'" in page)
m = re.findall(r"<script>([\s\S]*?)</script>", page)
open(sys.argv[2], "w").write("\n".join(m))
print(f"\nERGEBNIS: {sum(res)}/{len(res)} bestanden"); sys.exit(0 if all(res) else 1)

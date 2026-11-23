# Test case: Requests from Obsidian (/briefkasten) and the search page picking them up.
# Run: python3 obsidian_mailbox.py <project folder> [output file]  (or via pytest, see tests/README.md)
"""Briefkasten-Test: Attrappen fuer PyMuPDF und das Modell, echte Flask-Routen."""
import sys, os, types, json, hashlib, shutil, tempfile
src = sys.argv[1]
T = tempfile.mkdtemp(prefix="vai_bk_")
for f in ["app.py", "indexer.py", "embedder.py", "score_contrast.py", "speicher.py", "ocr.py"]:
    shutil.copy(os.path.join(src, f), T)
shutil.copytree(os.path.join(src, "templates"), T + "/templates")
os.makedirs(T + "/pdfs")
for n in ["a.pdf", "b.pdf", "c.pdf"]:
    open(f"{T}/pdfs/{n}", "wb").write(b"%PDF-1.4 " + n.encode())
json.dump(dict(pdf_base_dir=T + "/pdfs", phrase_max_chars=2000, search_max_results=10, pdf_isoliert_lesen=False), open(T + "/config.json", "w"))
sys.modules["fitz"] = types.ModuleType("fitz"); sys.path.insert(0, T)
import numpy as np, indexer, embedder
def vec(t):
    rng = np.random.default_rng(int(hashlib.md5(t.encode()).hexdigest()[:8], 16))
    v = rng.standard_normal(1024).astype(np.float32); return v / np.linalg.norm(v)
seen = []
indexer.extract_page_chunks = lambda p, o=0: [dict(seite=i+1, zeile=1, text=f"{os.path.basename(str(p))} S{i+1}",
    text_for_embedding=f"{os.path.basename(str(p))} S{i+1}", bbox=None) for i in range(3)]
embedder.embed_passages = lambda ts: np.array([vec(t) for t in ts], dtype=np.float32)
embedder.embed_query = lambda t: (seen.append(t), vec(t))[1]
import app as A; import speicher as _sp; A.load_meta = lambda: list(_sp.alle_chunks(_sp.verbinden(A.DB_FILE, nur_lesen=True)))
res = []
def check(name, ok, d=""):
    res.append(ok); print("ok    " if ok else "FEHLER", name, d)
c = A.app.test_client()

r = c.post("/briefkasten", json={"text": "Systemdynamik"})
check("ohne Index: 400 mit Meldung", r.status_code == 400 and "index" in r.get_json()["error"].lower())
A.run_indexing_guarded("full")
check("Index gebaut (9 Chunks)", A.INDEX_PROGRESS["phase"] == "done" and len(A.load_meta()) == 9)

s0 = c.get("/search/serial").get_json()["serial"]
r = c.post("/search", json={"phrase": "a.pdf S2"}); own = r.get_json()["documents"]
check("/search unveraendert nutzbar", r.status_code == 200 and own[0]["path"] == "a.pdf" and abs(own[0]["best_score"] - 1.0) < 1e-3, str(own[0]))
last = c.get("/search/last").get_json()
check("eigene Suche: kein Brief, Zaehler +1", last["brief"] is None and last["serial"] == s0 + 1)

r = c.post("/briefkasten", json={"text": "  a.pdf S2\r\n", "quelle": "Themen/Notiz.md", "art": "Markierung", "herkunft": "Obsidian"})
d = r.get_json()
check("Brief angenommen", r.status_code == 200 and d["ok"] and d["brief"]["treffer"] == 3 and not d["brief"]["gekuerzt"], str(d))
last = c.get("/search/last").get_json()
check("Brief = gleiche Rangliste wie dieselbe Phrase im Eingabefeld", last["documents"] == own and last["phrase"] == "a.pdf S2")
check("Brief steht als letzte Suche mit Herkunft", last["brief"]["quelle"] == "Themen/Notiz.md" and last["brief"]["herkunft"] == "Obsidian" and last["serial"] == s0 + 2)
check("Chunks nach Score sortierbar (wie bei Suche)", c.get("/documents/chunks?path=a.pdf").get_json()[0]["score"] > 0.99)
check("Kontrastfunktion hat die volle Liste", len(A.LAST_SEARCH_ALL_SCORES) == 3)

long = "Wort " * 1000
r = c.post("/briefkasten", json={"text": long, "art": "ganze Notiz"}); d = r.get_json()
check("langer Brief: gekuerzt und gemeldet", d["ok"] and d["brief"]["gekuerzt"] and d["brief"]["zeichen"] == len(long.strip()) and d["brief"]["zeichen_gesucht"] <= 2000, str(d["brief"]))
check("an das Modell geht hoechstens phrase_max_chars", len(seen[-1]) <= 2000)

for bad, name in [({"text": "   "}, "leer"), ({"text": 5}, "kein Text"), ({}, "Feld fehlt")]:
    r = c.post("/briefkasten", json=bad)
    check(f"abgelehnt: {name}", r.status_code == 400 and r.get_json()["ok"] is False)
r = c.post("/briefkasten", data="kein json", content_type="text/plain")
check("abgelehnt: kein JSON", r.status_code == 400)
check("abgelehnte Briefe aendern die letzte Suche nicht", c.get("/search/last").get_json()["brief"]["art"] == "ganze Notiz")

lst = c.get("/briefkasten").get_json()["briefe"]
check("Liste: 2 Briefe, neuester zuerst", len(lst) == 2 and lst[0]["gekuerzt"] and lst[1]["quelle"] == "Themen/Notiz.md")
for i in range(25): c.post("/briefkasten", json={"text": f"b.pdf S{i%3+1}"})
check("Liste auf 20 begrenzt", len(c.get("/briefkasten").get_json()["briefe"]) == 20)

s1 = c.get("/search/serial").get_json()["serial"]
c.post("/search/clear"); s2 = c.get("/search/serial").get_json()["serial"]
c.post("/search/clear"); s3 = c.get("/search/serial").get_json()["serial"]
last = c.get("/search/last").get_json()
check("clear: Zaehler +1, zweites clear aendert nichts (keine Endlosschleife)", s2 == s1 + 1 and s3 == s2 and last["phrase"] == "" and last["brief"] is None)
check("Cache-Control auf den neuen Routen", c.get("/search/serial").headers.get("Cache-Control") == "no-store" and c.get("/briefkasten").headers.get("Cache-Control") == "no-store")
page = c.get("/").get_data(as_text=True)
check("Startseite liefert 200 und enthaelt die Hinweiszeile", 'id="brief-note"' in page and "restoreLastSearch" in page)
open(sys.argv[2], "w").write(page)
print(f"\nERGEBNIS: {sum(res)}/{len(res)} bestanden"); sys.exit(0 if all(res) else 1)

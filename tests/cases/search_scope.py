# Test case: Search scope: area list, selection, storage in suchbereich.json, search restricted to areas.
# Run: python3 search_scope.py <project folder> [output file]  (or via pytest, see tests/README.md)
import sys, os, types, json, hashlib, shutil, tempfile, unicodedata, re
src = sys.argv[1]; T = os.path.realpath(tempfile.mkdtemp(prefix="sa_b3_"))
for f in ["app.py", "indexer.py", "embedder.py", "score_contrast.py", "speicher.py", "ocr.py"]: shutil.copy(os.path.join(src, f), T)
shutil.copytree(os.path.join(src, "templates"), T + "/templates")
R = T + "/daten"; nfd = unicodedata.normalize("NFD", "Français")
for d in ["Physik", "Technik", nfd, "Privat"]: os.makedirs(f"{R}/{d}")
for rel in ["Physik/p1.pdf", "Physik/p2.pdf", "Technik/t.pdf", f"{nfd}/f.pdf", "Privat/x.pdf", "wurzel.pdf"]:
    open(f"{R}/{rel}", "wb").write(b"%PDF " + rel.encode())
json.dump(dict(pdf_base_dir=R, pdf_isoliert_lesen=False, ausgeschlossen=["Privat"]), open(T + "/config.json", "w"))
sys.modules["fitz"] = types.ModuleType("fitz"); sys.path.insert(0, T)
import numpy as np, indexer, embedder
def vec(t):
    r = np.random.default_rng(int(hashlib.md5(t.encode()).hexdigest()[:8], 16)); v = r.standard_normal(1024).astype(np.float32); return v / np.linalg.norm(v)
indexer.extract_page_chunks = lambda p, o=0: [dict(seite=i+1, zeile=1, text=f"{os.path.basename(str(p))} S{i+1}", text_for_embedding=f"E {os.path.basename(str(p))} S{i+1}", bbox=None) for i in range(2)]
embedder.embed_passages = lambda ts: np.array([vec(t) for t in ts], dtype=np.float32)
embedder.embed_query = vec
res = []
def check(n, ok, d=""): res.append(ok); print("ok    " if ok else "FEHLER", n, d)
import app as A
A.run_indexing_guarded("full"); c = A.app.test_client()
d = c.get("/suchbereich").get_json()
names = [b["name"] for b in d["bereiche"]]
check("Liste: alle Bereiche ausser Privat, Wurzel als eigener Eintrag, NFC, alle gewaehlt", names == [A.ROOT_AREA_LABEL, "Français", "Physik", "Technik"] and all(b["gewaehlt"] for b in d["bereiche"]) and d["alle"] and [b["chunks"] for b in d["bereiche"]] == [2, 2, 4, 2], str(d))
full = c.post("/search", json={"phrase": "E t.pdf S1"}).get_json()
check("ohne Eingrenzung: 5 Dokumente, kein Hinweis", len(full["documents"]) == 5 and full["scope"] is None and full["documents"][0]["path"] == "Technik/t.pdf")
r = c.post("/suchbereich", json={"bereiche": ["Physik", "Fran" + "çais"]}).get_json()
check("Auswahl setzen (NFD-Eingabe akzeptiert)", r["ok"] and r["suchbereich"]["bereiche"] == ["Français", "Physik"] and r["suchbereich"]["von"] == 4 and r["suchbereich"]["chunks"] == 6, str(r))
check("gespeichert in suchbereich.json", json.load(open(T + "/suchbereich.json"))["bereiche"] == ["Français", "Physik"])
s = c.post("/search", json={"phrase": "E t.pdf S1"}).get_json()
paths = [x["path"] for x in s["documents"]]
check("Suche nur in Physik und Français", sorted(paths) == sorted(["Physik/p1.pdf", "Physik/p2.pdf", f"{nfd}/f.pdf"]) and s["scope"]["bereiche"] == ["Français", "Physik"], str(paths))
check("Kontrast-Liste nur aus dem Suchbereich", len(A.LAST_SEARCH_ALL_SCORES) == 3)
scores_full = {x["path"]: x["best_score"] for x in full["documents"]}
check("Scores im Suchbereich unveraendert", all(abs(scores_full[x["path"]] - x["best_score"]) < 1e-9 for x in s["documents"]))
check("/search/last liefert den Suchbereich", c.get("/search/last").get_json()["scope"]["bereiche"] == ["Français", "Physik"])
b = c.post("/briefkasten", json={"text": "E t.pdf S1", "herkunft": "Obsidian"}).get_json()
check("Briefkasten beachtet den Suchbereich und meldet ihn", b["ok"] and b["brief"]["treffer"] == 3 and b["brief"]["suchbereich"]["bereiche"] == ["Français", "Physik"])
check("Dokumentliste bleibt vollstaendig (ohne Ausgeschlossene)", len(c.get("/documents").get_json()) == 5)
check("leere Auswahl abgelehnt", c.post("/suchbereich", json={"bereiche": []}).status_code == 400)
check("unbekannte Bereiche allein abgelehnt", c.post("/suchbereich", json={"bereiche": ["Gibtsnicht"]}).status_code == 400)
check("falsches Format abgelehnt", c.post("/suchbereich", json={"bereiche": "Physik"}).status_code == 400 and c.post("/suchbereich", json={}).status_code == 400)
# Neustart: Auswahl bleibt
sys.modules.pop("app"); import app as A2; c2 = A2.app.test_client()
d = c2.get("/suchbereich").get_json()
check("nach Neustart: Auswahl erhalten", not d["alle"] and [b["name"] for b in d["bereiche"] if b["gewaehlt"]] == ["Français", "Physik"])
# Bereich spaeter ausgeschlossen -> faellt aus Auswahl, Rest bleibt
json.dump(dict(pdf_base_dir=R, pdf_isoliert_lesen=False, ausgeschlossen=["Privat", "Physik"]), open(T + "/config.json", "w"))
sys.modules.pop("app"); import app as A3; c3 = A3.app.test_client()
s = c3.post("/search", json={"phrase": "E f.pdf S1"}).get_json()
check("ausgeschlossener Bereich faellt aus dem Suchbereich", [x["path"] for x in s["documents"]] == [f"{nfd}/f.pdf"] and s["scope"]["bereiche"] == ["Français"], str(s))
json.dump(dict(pdf_base_dir=R, pdf_isoliert_lesen=False, ausgeschlossen=["Privat", "Physik", "Français"]), open(T + "/config.json", "w"))
sys.modules.pop("app"); import app as A4; c4 = A4.app.test_client()
s = c4.post("/search", json={"phrase": "E f.pdf S1"}).get_json()
check("bleibt nichts uebrig: wieder alle Bereiche statt leerer Suche", len(s["documents"]) == 2 and s["scope"] is None, str(s))
r = c4.post("/suchbereich", json={"bereiche": None}).get_json()
check("alle wieder: null gespeichert", r["ok"] and r["suchbereich"] is None and json.load(open(T + "/suchbereich.json"))["bereiche"] is None)
page = c4.get("/status").get_data(as_text=True)
check("Statusseite enthaelt den Kasten Suchbereich", 'id="scope-box"' in page and "scopeSave" in page)
open(sys.argv[2], "w").write(page + c4.get("/").get_data(as_text=True))
print(f"\nERGEBNIS: {sum(res)}/{len(res)} bestanden"); sys.exit(0 if all(res) else 1)

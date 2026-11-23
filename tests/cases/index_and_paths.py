# Test case: Folder scan (exclusions, symbolic links), full and incremental index runs, chunk IDs, PDF view route.
# Run: python3 index_and_paths.py <project folder> [output file]  (or via pytest, see tests/README.md)
import sys, os, types, json, hashlib, shutil, tempfile, time, unicodedata
src = sys.argv[1]; T = os.path.realpath(tempfile.mkdtemp(prefix="sa_b1_"))
for f in ["app.py", "indexer.py", "embedder.py", "score_contrast.py", "speicher.py", "ocr.py"]: shutil.copy(os.path.join(src, f), T)
shutil.copytree(os.path.join(src, "templates"), T + "/templates")
D = T + "/daten"; P = T + "/privat"
nfd = unicodedata.normalize("NFD", "Urlaub Ausflüge")
for d in ["Physik", "Technik", nfd, "Leer"]: os.makedirs(f"{D}/{d}/unter")
os.makedirs(P)
for rel in ["Physik/a.pdf", "Physik/unter/b.pdf", "Technik/c.pdf", f"{nfd}/d.pdf", "wurzel.pdf"]:
    open(f"{D}/{rel}", "wb").write(b"%PDF " + rel.encode())
open(f"{P}/geheim.pdf", "wb").write(b"%PDF geheim")
os.symlink(P, f"{D}/_temp_link")                       # symbolischer Link auf Ordner
os.symlink(f"{P}/geheim.pdf", f"{D}/Physik/link.pdf")   # symbolischer Link auf Datei
open(f"{D}/_temp Alias", "wb").write(b"book\0\0mark")   # Finder-Alias: gewoehnliche Datei
def cfg(**kw):
    json.dump(dict(pdf_base_dir=D, pdf_isoliert_lesen=False, **kw), open(T + "/config.json", "w"), ensure_ascii=False)
cfg()
sys.modules["fitz"] = types.ModuleType("fitz"); sys.path.insert(0, T)
import numpy as np, indexer, embedder
def vec(t):
    r = np.random.default_rng(int(hashlib.md5(t.encode()).hexdigest()[:8], 16)); v = r.standard_normal(1024).astype(np.float32); return v / np.linalg.norm(v)
indexer.extract_page_chunks = lambda p, o=0: [dict(seite=i+1, zeile=1, text=f"{os.path.basename(str(p))} S{i+1}",
    text_for_embedding=f"E {os.path.basename(str(p))} S{i+1}", bbox=None) for i in range(2)]
embedder.embed_passages = lambda ts: np.array([vec(t) for t in ts], dtype=np.float32)
embedder.embed_query = vec
res = []
def check(n, ok, d=""): res.append(ok); print("ok    " if ok else "FEHLER", n, d)
import importlib
import app as A; import speicher as _sp; A.load_meta = lambda: list(_sp.alle_chunks(_sp.verbinden(A.DB_FILE, nur_lesen=True)))
c = A.app.test_client()
scan = A.scan_pdfs(D)
check("Scan ohne Ausschluss: 5 PDFs, keine Links, kein Alias", sorted(scan) == sorted(["Physik/a.pdf", "Physik/unter/b.pdf", "Technik/c.pdf", f"{nfd}/d.pdf", "wurzel.pdf"]), str(sorted(scan)))
A.run_indexing_guarded("full")
check("Komplettlauf ok, 10 Chunks", A.INDEX_PROGRESS["phase"] == "done" and len(A.load_meta()) == 10)
check("PDF-Anzeige: normale Datei 200", c.get("/pdf/Physik/a.pdf").status_code == 200)
check("PDF-Anzeige: symbolischer Link auf Datei 403", c.get("/pdf/Physik/link.pdf").status_code == 403)
check("PDF-Anzeige: Pfad durch Ordnerlink 403", c.get("/pdf/_temp_link/geheim.pdf").status_code == 403)
check("PDF-Anzeige: ../ aus der Wurzel 403/404", c.get("/pdf/../privat/geheim.pdf").status_code in (403, 404))
q_tech = "E c.pdf S1"
r = c.post("/search", json={"phrase": q_tech}).get_json()
check("Suche findet Technik", r["documents"][0]["path"] == "Technik/c.pdf")

# Ausschluss Technik + Urlaub (NFC geschrieben, NFD auf der Platte), Neustart des Servers simulieren
cfg(ausgeschlossen=["Technik", "Urlaub Ausflüge/", "  "])
for m in ["app"]: sys.modules.pop(m)
import app as A; import speicher as _sp; A.load_meta = lambda: list(_sp.alle_chunks(_sp.verbinden(A.DB_FILE, nur_lesen=True)))
c = A.app.test_client()
check("Ausschlussliste gelesen (NFC, Schraegstrich, Leereintrag ignoriert)", A.EXCLUDED_AREAS == {"Technik", "Urlaub Ausflüge"}, str(A.EXCLUDED_AREAS))
scan = A.scan_pdfs(D)
check("Scan: Technik und Urlaub (NFD) fehlen", sorted(scan) == ["Physik/a.pdf", "Physik/unter/b.pdf", "wurzel.pdf"], str(sorted(scan)))
r = c.post("/search", json={"phrase": q_tech}).get_json()
paths = [d["path"] for d in r["documents"]]
check("vor dem naechsten Lauf: Suche zeigt Technik/Urlaub nicht mehr", "Technik/c.pdf" not in paths and f"{nfd}/d.pdf" not in paths and len(paths) == 3, str(paths))
check("Kontrast-Liste ohne Ausgeschlossene", len(A.LAST_SEARCH_ALL_SCORES) == 3)
docs = [d["path"] for d in c.get("/documents").get_json()]
check("Dokumentliste ohne Ausgeschlossene", sorted(docs) == ["Physik/a.pdf", "Physik/unter/b.pdf", "wurzel.pdf"])
check("Chunk-Liste eines ausgeschlossenen Dokuments 404", c.get("/documents/chunks?path=Technik/c.pdf").status_code == 404)
tid = [e["id"] for e in A.load_meta() if e["path"] == "Technik/c.pdf"][0]
check("Chunk-Anzeige eines ausgeschlossenen Chunks: 'existiert nicht'", A.get_chunk_meta(tid) is None)
check("PDF-Anzeige ausgeschlossen 403 (auch NFD/NFC)", c.get("/pdf/Technik/c.pdf").status_code == 403 and c.get("/pdf/" + unicodedata.normalize("NFC", "Urlaub Ausflüge") + "/d.pdf").status_code == 403)
st = A.build_status()
check("Status: 3 PDFs, 6 sichtbare Chunks, Liste der Ausschluesse, 2 entfernt", st["anzahl_pdfs"] == 3 and st["anzahl_chunks"] == 6 and st["ausgeschlossen"] == ["Technik", "Urlaub Ausflüge"] and st["entfernt_seit_letzter_indizierung"] == 2, str({k: st[k] for k in ["anzahl_pdfs", "anzahl_chunks", "ausgeschlossen", "entfernt_seit_letzter_indizierung"]}))
check("Statusseite zeigt die Liste", "Excluded areas" in c.get("/status").get_data(as_text=True) and "Technik" in c.get("/status/panel").get_data(as_text=True))
A.run_indexing_guarded("incremental")
m = A.load_meta()
check("naechster Lauf entfernt die Chunks der ausgeschlossenen Bereiche", A.INDEX_PROGRESS["phase"] == "done" and sorted({e["path"] for e in m}) == ["Physik/a.pdf", "Physik/unter/b.pdf", "wurzel.pdf"] and len(m) == 6)
log = open(T + "/logs/" + sorted(os.listdir(T + "/logs"))[-1]).read()
check("Log nennt die Entfernung je Datei", log.count("retired") == 2, log[-400:])
check("Zustand kennt Ausgeschlossene nicht mehr (keine Endlosschleife 'entfernt')", A.build_status()["entfernt_seit_letzter_indizierung"] == 0)
# Wieder hereinnehmen
cfg(); sys.modules.pop("app"); import app as A; import speicher as _sp; A.load_meta = lambda: list(_sp.alle_chunks(_sp.verbinden(A.DB_FILE, nur_lesen=True)))
A.run_indexing_guarded("incremental")
check("wieder aufgenommen: 10 Chunks", len(A.load_meta()) == 10 and A.build_status()["neu_seit_letzter_indizierung"] == 0)
# alle ausgeschlossen
cfg(ausgeschlossen=["Physik", "Technik", "Urlaub Ausflüge", "Leer"]); sys.modules.pop("app"); import app as A; import speicher as _sp; A.load_meta = lambda: list(_sp.alle_chunks(_sp.verbinden(A.DB_FILE, nur_lesen=True)))
c = A.app.test_client()
r = c.post("/search", json={"phrase": "x"})
check("nur noch Datei in der Wurzel sichtbar, Suche geht", r.status_code == 200 and [d["path"] for d in r.get_json()["documents"]] == ["wurzel.pdf"])
print(f"\nERGEBNIS: {sum(res)}/{len(res)} bestanden"); sys.exit(0 if all(res) else 1)

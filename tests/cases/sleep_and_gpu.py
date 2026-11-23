# Test case: Sleep detection during runs, clearing the GPU cache every N batches.
# Run: python3 sleep_and_gpu.py <project folder> [output file]  (or via pytest, see tests/README.md)
import sys, os, json, shutil, tempfile, time, types, hashlib
src = os.path.realpath(sys.argv[1]); BASE = os.path.realpath(tempfile.mkdtemp(prefix="sa_s5_"))
R = BASE + "/daten"; os.makedirs(R + "/A")
for n in range(6): open(f"{R}/A/d{n}.pdf", "wb").write(("%%PDF %d" % n).encode())
T = BASE + "/proj"; os.makedirs(T)
for f in ["app.py", "indexer.py", "embedder.py", "score_contrast.py", "extract_worker.py", "speicher.py", "ocr.py"]: shutil.copy(os.path.join(src, f), T)
shutil.copytree(os.path.join(src, "templates"), T + "/templates"); shutil.copytree(os.path.join(src, "tools"), T + "/tools")
json.dump(dict(pdf_base_dir=R, embed_batch_size=2, gpu_leeren_alle_stapel=2, pdf_isoliert_lesen=False, indexlauf_eigener_prozess=False), open(T + "/config.json", "w"))
sys.modules["fitz"] = types.ModuleType("fitz"); sys.path.insert(0, T); os.chdir(T)
import numpy as np, indexer, embedder
def vec(t):
    r = np.random.default_rng(int(hashlib.md5(t.encode()).hexdigest()[:8], 16)); v = r.standard_normal(1024).astype(np.float32); return v / np.linalg.norm(v)
indexer.extract_page_chunks = lambda p, o=0: [dict(seite=i, zeile=1, text=f"t {p} {i}", text_for_embedding=f"e {p} {i}", bbox=None) for i in range(3)]
res = []
def check(n, ok, d=""): ok = bool(ok); res.append(ok); print("ok    " if ok else "FEHLER", n, d, flush=True)
try:
    import torch
    _mps = bool(torch.backends.mps.is_available())
except Exception:
    _mps = False
check(f"gpu_vorrat_leeren: kein Fehler, {_mps} (MPS {'da' if _mps else 'nicht da'})", embedder.gpu_vorrat_leeren() is _mps)
import app as A
geleert = []; embedder.gpu_vorrat_leeren = lambda: geleert.append(1) or True
# Ruhezustand simulieren: ab dem 3. Stapel laeuft die Wanduhr 10 min voraus
aufrufe = [0]; echt_time = time.time
def embed(ts):
    aufrufe[0] += 1
    if aufrufe[0] == 3: A.time.time = lambda: echt_time() + 600
    return np.array([vec(t) for t in ts], dtype=np.float32)
embedder.embed_passages = embed
A.EMBED_LOG_INTERVAL_S = 0
A.run_indexing_guarded("full"); A.time.time = echt_time
check("Lauf fertig", A.INDEX_PROGRESS["phase"] == "done", str(A.INDEX_PROGRESS.get("error")))
log = open(sorted(os.path.join(T, "logs", f) for f in os.listdir(T + "/logs") if f.startswith("index_full"))[-1]).read()
check("GPU-Vorrat alle 2 Stapel geleert (9 Stapel -> 4x, nicht nach dem letzten)", len(geleert) == 4, str(len(geleert)))
check("Log nennt die Einstellung", "Clear GPU cache: every 2 batches" in log)
check("Ruhezustand wird gemeldet", "Mac slept about 10 min" in log and "Asleep during the run: 10 min" in log, log[-600:])
check("Meldung nur einmal", log.count("Mac slept") == 1)
check("caffeinate nur auf macOS (hier nicht gestartet, kein Fehler)", "caffeinate nicht gestartet" not in log)
import subprocess
r = subprocess.run([sys.executable, "tools/messwerte.py"], capture_output=True, text=True)
check("messwerte zeigt Ruhezustand", "of which the Mac was asleep: 10 min" in r.stdout, r.stdout[-400:] + r.stderr[-300:])
r = subprocess.run([sys.executable, "tools/check_index.py"], capture_output=True, text=True); check("Index in Ordnung", r.returncode == 0)
print(f"\nERGEBNIS: {sum(res)}/{len(res)} bestanden"); sys.exit(0 if all(res) else 1)

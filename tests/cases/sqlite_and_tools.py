# Test case: SQLite storage without JSON, tools check_index and messwerte.
# Run: python3 sqlite_and_tools.py <project folder> [output file]  (or via pytest, see tests/README.md)
import sys, os, json, shutil, tempfile, time, subprocess, types
src = os.path.realpath(sys.argv[1]); BASE = os.path.realpath(tempfile.mkdtemp(prefix="sa_s4_"))
py = sys.executable; R = BASE + "/daten"
for d in ["A/u", "B"]: os.makedirs(f"{R}/{d}")
for n in ["A/a1.pdf", "A/a2.pdf", "A/u/a3.pdf", "B/b1.pdf"]: open(f"{R}/{n}", "wb").write(b"%PDF " + n.encode())
T = BASE + "/proj"; os.makedirs(T)
for f in ["app.py", "indexer.py", "embedder.py", "score_contrast.py", "extract_worker.py", "speicher.py", "ocr.py"]: shutil.copy(os.path.join(src, f), T)
shutil.copytree(os.path.join(src, "templates"), T + "/templates"); shutil.copytree(os.path.join(src, "tools"), T + "/tools")
def cfg(**kw):
    p = T + "/config.json"; c = json.load(open(p)) if os.path.exists(p) else dict(pdf_base_dir=R, embed_batch_size=2)
    c.update(kw); c = {k: v for k, v in c.items() if v is not None}; json.dump(c, open(p, "w"))
cfg()
ENV = dict(os.environ, SEE_ALSO_TEST_ATTRAPPE="1")
res = []
def check(n, ok, d=""): ok = bool(ok); res.append(ok); print("ok    " if ok else "FEHLER", n, d, flush=True)
def lauf(mode, cancel_bei=None, embed_s="0"):
    p = subprocess.Popen([py, "app.py", "--indexlauf", mode], cwd=T, env=dict(ENV, SEE_ALSO_TEST_EMBED_S=embed_s),
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    ende = None; gesendet = False
    for raw in p.stdout:
        kind, _, payload = raw.decode().rstrip("\n").partition(" ")
        if kind == "PROGRESS" and cancel_bei and not gesendet and json.loads(payload).get("phase") == cancel_bei:
            p.stdin.write(b"CANCEL\n"); p.stdin.flush(); gesendet = True
        if kind == "ENDE": ende = json.loads(payload)
    p.stdin.close(); rc = p.wait(); return rc, ende, p.stderr.read().decode()
def run(*a): return subprocess.run([py, *a], cwd=T, capture_output=True, text=True)
sys.path.insert(0, T); import speicher
def stand():
    c = speicher.verbinden(T + "/see_also.db", nur_lesen=True)
    r = (list(speicher.alle_chunks(c)), sorted((i, bytes(v)) for i, v in c.execute("SELECT id, vektor FROM vektoren")),
         speicher.zustand_lesen(c), speicher.generation(c))
    c.close(); return r

# 1. Frische Installation
rc, ende, err = lauf("incremental")
check("ohne Datenbank: Nachindizieren verweigert, nichts angelegt", ende and ende["phase"] == "error" and "Nachindizieren setzt" in (ende["error"] or "") and not os.path.exists(T + "/see_also.db"), str(ende))
rc, ende, err = lauf("full")
check("ohne Datenbank: Komplettlauf legt sie an", ende and ende["phase"] == "done" and os.path.exists(T + "/see_also.db") and len(stand()[0]) == 8, f"{ende} {err[-300:]}")
r = run("tools/check_index.py"); check("check_index: alles in Ordnung", r.returncode == 0 and "all fine" in r.stdout, r.stdout[-300:])
r = run("tools/messwerte.py"); check("messwerte: liest see_also.db", r.returncode == 0 and "Total chunks: 8" in r.stdout, r.stdout[-300:] + r.stderr[-300:])
log = open(sorted(os.path.join(T, "logs", f) for f in os.listdir(T + "/logs") if f.startswith("index_full"))[-1]).read()
check("Log: Speicher-Spitze, Lese-Prozess, festgeschrieben", "memory peak so far" in log and "Reader process started" in log and "Committed to see_also.db" in log)
check("Fehlermeldungen des Lese-Prozesses in fester Datei (keine temporaere Datei)", os.path.exists(T + "/logs/lese_prozess_stderr.log") and "tempfile" not in open(T + "/app.py").read())

# 2. Atomaritaet
time.sleep(1.1); os.utime(f"{R}/A/a1.pdf"); open(f"{R}/B/neu.pdf", "wb").write(b"%PDF neu")
vorher = stand()
rc, ende, err = lauf("incremental", cancel_bei="embed", embed_s="0.3")
check("Abbruch in der Einbettung: unveraendert", ende and ende["phase"] == "cancelled" and stand() == vorher, str(ende))
for punkt in ("save", "swap", "swap_kill"):
    cfg(debug_inject_error=punkt); rc, ende, err = lauf("incremental")
    check(f"Fehler/Kill '{punkt}': unveraendert", stand() == vorher and (ende is None if punkt == "swap_kill" else ende["phase"] == "error"), f"rc={rc} {ende}")
cfg(debug_inject_error=None); rc, ende, err = lauf("incremental"); nach = stand()
check("danach normaler Lauf kommt an", ende and ende["phase"] == "done" and nach[3] == vorher[3] + 1 and any(e["path"] == "B/neu.pdf" for e in nach[0]))
r = run("tools/check_index.py"); check("check_index nach den Abbruechen: in Ordnung", r.returncode == 0)

# 3. Werkzeuge
r = run("tools/backup_index.py", BASE + "/bk"); bk = os.listdir(BASE + "/bk")
check("Sicherung: geprueft, eine eigenstaendige Datei", r.returncode == 0 and "quick_check: ok" in r.stdout and len(bk) == 1
      and os.path.exists(f"{BASE}/bk/{bk[0]}/see_also.db") and not os.path.exists(f"{BASE}/bk/{bk[0]}/see_also.db-wal"), r.stdout + r.stderr[-300:])
r = run("tools/check_index.py", f"{BASE}/bk/{bk[0]}")
for f in ["meta.json", "index.npz", "id_order.json", "index_state.json", "meta.json.prev"]: open(f"{T}/{f}", "w").write("alt")

# 4. Server
sys.modules["fitz"] = types.ModuleType("fitz"); os.chdir(T)
import app as A
st = A.build_status()
check("Status: alte JSON-Dateien werden gemeldet", st["alte_json_dateien"] == ["meta.json", "index.npz", "id_order.json", "index_state.json"], str(st["alte_json_dateien"]))
check("Statusseite zeigt den Hinweis", "json_archivieren.py" in A.app.test_client().get("/status").get_data(as_text=True))
if os.path.exists(T + "/tools/json_archivieren.py"):
    # Migrationswerkzeug der JSON-Zeit; nur in der Arbeitskopie, nicht im oeffentlichen Repository
    r = run("tools/json_archivieren.py"); check("Archivieren: zeigt erst nur an", r.returncode == 0 and "Nur angezeigt" in r.stdout and os.path.exists(T + "/meta.json"))
    r = run("tools/json_archivieren.py", "--anwenden"); arch = os.listdir(T + "/archiv")
    check("Archivieren: verschoben, inkl. *.prev", r.returncode == 0 and not os.path.exists(T + "/meta.json") and not os.path.exists(T + "/meta.json.prev")
          and sorted(os.listdir(f"{T}/archiv/{arch[0]}")) == sorted(["meta.json", "index.npz", "id_order.json", "index_state.json", "meta.json.prev"]))
else:
    for f in ["meta.json", "index.npz", "id_order.json", "index_state.json", "meta.json.prev"]: os.remove(f"{T}/{f}")
check("Status: danach kein Hinweis mehr", A.build_status()["alte_json_dateien"] == [])
gefangen = {}
class FakePopen:
    def __init__(self, *a, **k): gefangen.update(k.get("env") or {}); raise RuntimeError("nur Umgebung pruefen")
orig = A.subprocess.Popen; A.subprocess.Popen = FakePopen
A.run_indexing_in_subprocess("incremental"); A.subprocess.Popen = orig
check("Lauf-Prozess: Warnung zu loky unterdrueckt", "ignore::UserWarning:multiprocessing.resource_tracker" in gefangen.get("PYTHONWARNINGS", ""))
check("Speichermessung: auf Linux kein Absturz, sinnvoller Wert", 0 < A._spitze_gb() < 50)
cfg(speicher="json")
r = subprocess.run([py, "-c", "import sys,types;sys.modules['fitz']=types.ModuleType('fitz');import app"], cwd=T, capture_output=True, text=True)
check("config speicher=json: klare Meldung beim Start", r.returncode != 0 and "JSON storage was removed" in r.stderr, r.stderr[-200:])
print(f"\nERGEBNIS: {sum(res)}/{len(res)} bestanden"); sys.exit(0 if all(res) else 1)

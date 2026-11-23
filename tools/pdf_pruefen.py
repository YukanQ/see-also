#!/usr/bin/env python3
"""pdf_pruefen.py - test-reads the PDFs of one area the way the index run does,
each file in its own isolated worker process. If PyMuPDF crashes on a file (an
error in C code that Python cannot catch) or hangs, only the worker is ended and
the check continues with the next file. Changes neither the index nor the PDFs.
Does not embed (no model).

Run in the project folder (see_also may run; better not index at the same time):
    venv/bin/python3 tools/pdf_pruefen.py Electronics
    venv/bin/python3 tools/pdf_pruefen.py Electronics --time-limit 120

Result: a summary in the terminal, every file with its finding in
logs/pdf_check_<area>_<time>.tsv (columns: finding, seconds, chunks, path, message).
Findings: ok, empty (no text layer), error (Python error; the index run skips the
file), CRASH and TIMEOUT (the index run records these as problem files and skips them).
"""
import json
import os
import select
import subprocess
import sys
import tempfile
import time
from datetime import datetime

WORKER = r'''
import sys, os, json, time
sys.path.insert(0, sys.argv[1])
overlap = int(sys.argv[2])
PROTO = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
os.dup2(2, 1)
sys.stdout = sys.stderr
if os.environ.get("SEE_ALSO_PRUEF_ATTRAPPE"):
    import types; sys.modules["fitz"] = types.ModuleType("fitz")
import indexer
if os.environ.get("SEE_ALSO_PRUEF_ATTRAPPE"):
    def ex(p, o=0):
        n = os.path.basename(str(p))
        if n.startswith("absturz"): os.kill(os.getpid(), 9)  # SIGKILL: kein Absturzdialog von macOS
        if n.startswith("haengt"): time.sleep(3600)
        if n.startswith("kaputt"): raise ValueError("format error: kaputt")
        if n.startswith("leer"): return []
        if n.startswith("spricht"):
            print("MuPDF error: format error: cmsOpenProfileFromMem failed"); os.write(1, b"C-Ebene\n")
        if n.startswith("laut"):
            sys.stderr.write("warning: MuPDF Warnung\n" * 50000); sys.stderr.flush()
        return [{"text": n}] * 3
    indexer.extract_page_chunks = ex
for line in sys.stdin:
    if not line.strip():
        continue
    path = json.loads(line)
    PROTO.write("START\n"); PROTO.flush()
    t = time.monotonic()
    try:
        n = len(indexer.extract_page_chunks(path, overlap))
        PROTO.write("ERG " + json.dumps({"ok": True, "n": n, "s": time.monotonic() - t}) + "\n"); PROTO.flush()
    except Exception as e:
        PROTO.write("ERG " + json.dumps({"ok": False, "msg": f"{type(e).__name__}: {e}"[:300], "s": time.monotonic() - t}) + "\n"); PROTO.flush()
'''


def main():
    argv = sys.argv[1:]
    limit = 120.0
    schalter = next((a for a in ("--time-limit", "--zeitlimit") if a in argv), None)
    if schalter:
        k = argv.index(schalter)
        limit = float(argv[k + 1])
        del argv[k:k + 2]
    args = [a for a in argv if not a.startswith("--")]
    if len(args) != 1:
        print(__doc__)
        sys.exit(2)
    proj = os.getcwd()
    cfg = json.load(open(os.path.join(proj, "config.json"), encoding="utf-8"))
    base = cfg["pdf_base_dir"]
    area = args[0].strip("/")
    top = os.path.join(base, area)
    if not os.path.isdir(top):
        print(f"Folder {top} does not exist.")
        sys.exit(1)
    files = []
    for root, dirs, names in os.walk(top, followlinks=False):
        for n in names:
            full = os.path.join(root, n)
            if n.lower().endswith(".pdf") and not os.path.islink(full):
                files.append(full)
    files.sort()
    overlap = str(int(cfg.get("cross_page_overlap_chars", 0)))
    print(f"[{datetime.now():%H:%M:%S}] {len(files)} PDFs in {top}, time limit {limit:.0f} s per file\n")

    results = []
    worker = None
    i = 0
    t_all = time.monotonic()

    pending = {}   # eigener Zeilenpuffer je Arbeitsprozess (select + gepufferte Pipe vertragen sich nicht)

    def start_worker():
        # stderr in eine temporaere Datei: MuPDF schreibt viele Warnungen, eine
        # nicht geleerte Pipe liefe voll und der Arbeitsprozess bliebe stehen
        # (das war der Fehler der ersten Fassung: jede weitere Datei lief dann
        # ins Zeitlimit).
        err = tempfile.TemporaryFile()
        p = subprocess.Popen([sys.executable, "-c", WORKER, proj, overlap],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err,
                             text=True)
        p.err_file = err
        pending[p.pid] = b""
        return p

    def stderr_tail(p):
        try:
            p.err_file.seek(0, os.SEEK_END)
            size = p.err_file.tell()
            p.err_file.seek(max(0, size - 4000))
            lines = p.err_file.read().decode("utf-8", "replace").strip().splitlines()
            return " | ".join(lines[-3:])[:300]
        except Exception:
            return ""

    def ts():
        return datetime.now().strftime("%H:%M:%S")

    current = {"rel": None, "t0": 0.0, "warned": 0}

    def read_line(proc, timeout):
        # Nur Protokollzeilen; fremde Zeilen (falls doch etwas durchrutscht) ueberlesen.
        while True:
            line = read_raw_line(proc, timeout)
            if line is None or line == "" or line.startswith("START") or line.startswith("ERG "):
                return line

    def read_raw_line(proc, timeout):
        fd = proc.stdout.fileno()
        deadline = time.monotonic() + timeout
        while b"\n" not in pending[proc.pid]:
            rest = deadline - time.monotonic()
            if rest <= 0:
                return None
            r, _, _ = select.select([fd], [], [], min(rest, 5.0))
            if not r:
                # Lebenszeichen: eine Datei braucht lange
                if current["rel"]:
                    el = time.monotonic() - current["t0"]
                    if el >= 15 and el - current["warned"] >= 15:
                        current["warned"] = el
                        print(f"  [{ts()}] reading for {el:.0f} s: {current['rel']}", flush=True)
                continue
            chunk = os.read(fd, 65536)
            if not chunk:                       # Prozess beendet (Absturz)
                return ""
            pending[proc.pid] += chunk
        line, _, rest_bytes = pending[proc.pid].partition(b"\n")
        pending[proc.pid] = rest_bytes
        return line.decode("utf-8", "replace") + "\n"

    while i < len(files):
        if worker is None or worker.poll() is not None:
            worker = start_worker()
        path = files[i]
        rel = os.path.relpath(path, base)
        current.update(rel=rel, t0=time.monotonic(), warned=0)
        worker.stdin.write(json.dumps(path) + "\n")
        worker.stdin.flush()
        line = read_line(worker, 60)       # START (Import von fitz beim ersten Mal)
        if not line or not line.startswith("START"):
            results.append(("CRASH", 0.0, 0, rel, "worker process did not start"))
            worker.kill(); worker = None; i += 1
            continue
        t0 = time.monotonic()
        line = read_line(worker, limit)
        dt = time.monotonic() - t0
        if line is None:
            worker.kill(); worker.wait(); worker = None
            results.append(("TIMEOUT", dt, 0, rel, f"stopped after {limit:.0f} s"))
        elif not line.startswith("ERG "):
            try:
                worker.wait(timeout=10)
            except subprocess.TimeoutExpired:
                worker.kill(); worker.wait()
            results.append(("CRASH", dt, 0, rel, f"exit code {worker.returncode}; {stderr_tail(worker)}"))
            worker = None
        else:
            d = json.loads(line[4:])
            if d["ok"]:
                results.append(("ok" if d["n"] else "empty", d["s"], d["n"], rel, ""))
            else:
                results.append(("error", d["s"], 0, rel, d["msg"]))
        i += 1
        last = results[-1]
        if last[0] in ("CRASH", "TIMEOUT", "error"):
            print(f"  [{ts()}] {last[0]}: {last[3]}  {last[4]}", flush=True)
        if i % 25 == 0 or i == len(files):
            print(f"  [{ts()}] {i}/{len(files)} checked, {time.monotonic() - t_all:.0f} s", flush=True)
    if worker is not None and worker.poll() is None:
        worker.stdin.close(); worker.wait()

    os.makedirs(os.path.join(proj, "logs"), exist_ok=True)
    out = os.path.join(proj, "logs", f"pdf_check_{area.replace('/', '_')}_{datetime.now():%Y%m%d-%H%M%S}.tsv")
    with open(out, "w", encoding="utf-8") as f:
        f.write("finding\tseconds\tchunks\tpath\tmessage\n")
        for r in results:
            # Zeilenumbrueche/Tabs im Pfad sichtbar machen, damit die Tabelle lesbar bleibt
            pfad = r[3].replace("\n", "\\n").replace("\t", "\\t")
            f.write(f"{r[0]}\t{r[1]:.1f}\t{r[2]}\t{pfad}\t{r[4]}\n")
    counts = {}
    for r in results:
        counts[r[0]] = counts.get(r[0], 0) + 1
    print("\nResult: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    print(f"Total chunks (not embedded): {sum(r[2] for r in results)}")
    slow = sorted((r for r in results if r[0] in ("ok", "empty")), key=lambda r: -r[1])[:5]
    if slow:
        print("Slowest: " + "; ".join(f"{r[3]} {r[1]:.1f} s" for r in slow))
    bad = [r for r in results if r[0] in ("CRASH", "TIMEOUT")]
    if bad:
        print("\nProblem files (the index run would record and skip them):")
        for r in bad:
            print(f"  {r[0]:9s} {r[3]}   {r[4]}")
    print(f"\nList: {out}")


if __name__ == "__main__":
    main()

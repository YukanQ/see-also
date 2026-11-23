#!/usr/bin/env python3
"""tools/pdf_dateiende.py - finds PDFs without an end-of-file marker in the whole collection.

Read-only. A complete PDF ends with the marker %%EOF. If it is missing from the
last 16 KB, the file is very probably truncated (an interrupted copy, say). Such
files can still have pages and chunks - then only the end is lost, and the index
is incomplete for this file without anything showing it.

Run in the project folder:
    venv/bin/python3 tools/pdf_dateiende.py

Output: a summary in the terminal, the full list in logs/pdf_missing_eof.txt
(chunks, size, path). The page "Files to review" reads this list.

Limits: a PDF with more than 16 KB appended after %%EOF would be reported by
mistake (rare). A truncated PDF that happens to contain an earlier %%EOF in its
last 16 KB (incremental save) is not noticed. Application bundles (.app etc.)
are skipped, as in the scan.
"""
import json
import os
import sqlite3
import sys
import time

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUNDLE = {".app", ".pkg", ".mpkg", ".bundle", ".plugin", ".framework", ".kext", ".appex",
          ".xpc", ".prefpane", ".qlgenerator", ".mdimporter", ".saver", ".component", ".wdgt"}


def paket(ordner, name):
    e = os.path.splitext(name)[1].lower()
    return bool(e) and (e in BUNDLE or os.path.isdir(os.path.join(ordner, name, "Contents")))


def main():
    cfg = json.load(open(os.path.join(PROJ, "config.json"), encoding="utf-8"))
    base = cfg["pdf_base_dir"]
    con = sqlite3.connect(os.path.join(PROJ, "see_also.db"))
    chunks = dict(con.execute("SELECT path, COUNT(*) FROM chunks GROUP BY path"))
    con.close()

    t0 = time.monotonic()
    gesamt = 0
    ohne_ende = []      # (chunks, groesse, rel)
    kein_pdf = 0
    for wurzel, ordner, dateien in os.walk(base, followlinks=False):
        ordner[:] = [d for d in ordner if not paket(wurzel, d)]
        for f in dateien:
            if not f.lower().endswith(".pdf"):
                continue
            voll = os.path.join(wurzel, f)
            if os.path.islink(voll):
                continue
            gesamt += 1
            try:
                groesse = os.path.getsize(voll)
                with open(voll, "rb") as h:
                    kopf = h.read(1024)
                    h.seek(max(0, groesse - 16384))
                    schluss = h.read()
            except OSError:
                continue
            if groesse == 0:
                continue
            if b"%PDF" not in kopf:
                kein_pdf += 1
                continue
            if b"%%EOF" not in schluss:
                rel = os.path.relpath(voll, base)
                ohne_ende.append((chunks.get(rel, 0), groesse, rel))
    dauer = time.monotonic() - t0

    mit = [x for x in ohne_ende if x[0] > 0]
    ohne = [x for x in ohne_ende if x[0] == 0]
    print(f"PDFs checked: {gesamt} in {dauer:.0f} s")
    print(f"without end of file: {len(ohne_ende)}")
    print(f"  with chunks in the index (partly readable, end missing): {len(mit)}")
    print(f"  without chunks (lost entirely, or a scan):              {len(ohne)}")
    print(f"Files ending in .pdf that are not PDFs: {kein_pdf}")
    if mit:
        print("\nExamples with chunks:")
        for c, g, p in sorted(mit, reverse=True)[:10]:
            print(f"  {c:5d} chunks {g:>12,d} B  {p}")
    os.makedirs(os.path.join(PROJ, "logs"), exist_ok=True)
    ziel = os.path.join(PROJ, "logs", "pdf_missing_eof.txt")
    with open(ziel, "w", encoding="utf-8") as out:
        out.write(f"# PDFs without end of file (%%EOF), {time.strftime('%Y-%m-%d %H:%M')}, {len(ohne_ende)} files\n")
        out.write("# chunks\tsize\tpath (relative to " + base + ")\n")
        for c, g, p in sorted(ohne_ende, key=lambda x: (-x[0], x[2])):
            out.write(f"{c}\t{g}\t{p}\n")
    print(f"\nList: {ziel}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

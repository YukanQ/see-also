#!/usr/bin/env python3
"""messwerte.py - collects the figures of the index and the last index run. Read-only.

Run in the project folder (the server may be running):
    venv/bin/python3 tools/messwerte.py

Prints: chunks and PDFs, overall and per area (first folder level), file
sizes, key figures of the last index run (from logs/). Memory and search time
are measured by hand (terminal line at start, Activity Monitor, time curl).
Reads log files in the current English format and in the earlier German one.
"""
import collections
import glob
import os
import re
from datetime import datetime

def mb(p):
    return os.path.getsize(p) / 1e6 if os.path.exists(p) else float("nan")

def bereich(path):
    parts = path.split("/")
    return parts[0] if len(parts) > 1 else "(top level)"

per_area = collections.Counter()
docs_area = collections.defaultdict(set)
import sys
sys.path.insert(0, os.getcwd())
import speicher
con = speicher.verbinden("see_also.db", nur_lesen=True)
for path, n in con.execute("SELECT path, COUNT(*) FROM chunks GROUP BY path"):
    per_area[bereich(path)] += n
    docs_area[bereich(path)].add(path)
gen = speicher.generation(con)
con.close()
n_chunks = sum(per_area.values())
n_docs = sum(len(v) for v in docs_area.values())
print(f"Total chunks: {n_chunks}   PDFs with chunks: {n_docs}   (see_also.db, generation {gen})")
wal = mb('see_also.db-wal')
print(f"see_also.db {mb('see_also.db'):.0f} MB   see_also.db-wal " + ("-" if wal != wal else f"{wal:.0f} MB"))
print("\nPer area:  chunks  PDFs with chunks")
for a, n in sorted(per_area.items(), key=lambda x: -x[1]):
    print(f"  {a:40s} {n:7d} {len(docs_area[a]):6d}")

logs = sorted(glob.glob("logs/index_*.log"), key=os.path.getmtime)
if logs:
    txt = open(logs[-1], encoding="utf-8").read()
    def find(rx):
        m = re.search(rx, txt)
        return next((g for g in m.groups() if g is not None), "?") if m else "?"
    start = find(r"Start \w+(?:-Indizierung| indexing): (\S+)")
    end = find(r"(?:Ende|End): (\S+)")
    try:
        dur = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
        dur_s = f"{dur/60:.1f} min"
    except ValueError:
        dur_s = "?"
    embeds = re.findall(r"\[EMBED\] (\d+)/(\d+), ([\d.]+) [Cc]hunks/s.*?(?:seit Start (\d+) s|(\d+) s since start)", txt)
    print(f"\nLast run: {os.path.basename(logs[-1])}")
    n_pdfs = find(r"(?:Zu verarbeitende PDFs|PDFs to process): (\d+)")
    n_neu = find(r"(?:Block C: (\d+) Chunks neu|Embedding (\d+) new chunks)")
    print(f"  Total duration: {dur_s}   PDFs processed: {n_pdfs}")
    emb = (embeds[-1][3] or embeds[-1][4]) + " s, " + embeds[-1][2] + " chunks/s" if embeds else "-"
    print(f"  Newly embedded: {n_neu}   "
          f"embedding: {emb}")
    schlaf = find(r"(?:Ruhezustand waehrend des Laufs|Asleep during the run): (\d+ min)")
    if schlaf != "?":
        print(f"  of which the Mac was asleep: {schlaf}")
    spitze = find(r"(?:Speicher-Spitze des Laufs|Memory peak of the run): ([\d.]+ GB)")
    fest = find(r"(?:In see_also.db festgeschrieben|Committed to see_also.db) \(([\d.]+ s)\)")
    if spitze != "?" or fest != "?":
        print(f"  Memory peak of the run: {spitze}   commit: {fest}")

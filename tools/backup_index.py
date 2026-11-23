#!/usr/bin/env python3
"""backup_index.py - backs up see_also.db.

Uses SQLite's backup function: it yields a consistent state even while the
server reads or an index run is in progress (then the state before or after
its commit is saved, never half of it). Simply copying the file during a run
could give an unusable copy.

Run in the project folder:  venv/bin/python3 tools/backup_index.py [target folder]
Default target: backups/<date-time>/ (in .gitignore). Saved are see_also.db
and config.json. The backup is checked afterwards (quick_check, as many chunks
as vectors). Nothing is changed or deleted.
"""
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime


def main():
    if not os.path.exists("see_also.db"):
        sys.exit("Error: see_also.db not found. Run this in the project folder.")
    base = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.getcwd(), "backups")
    ziel = os.path.join(base, datetime.now().strftime("%Y%m%d-%H%M%S"))
    os.makedirs(ziel)
    t0 = time.monotonic()
    quelle = sqlite3.connect("see_also.db")
    quelle.execute("PRAGMA busy_timeout=30000")
    kopie = sqlite3.connect(os.path.join(ziel, "see_also.db"))
    quelle.backup(kopie, pages=256)          # in Abschnitten; andere Leser werden nicht lange blockiert
    kopie.execute("PRAGMA journal_mode=DELETE")   # Sicherung als eine einzige, eigenstaendige Datei
    n_q = quelle.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    quelle.close()
    ok = kopie.execute("PRAGMA quick_check").fetchone()[0]
    n_k = kopie.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    n_v = kopie.execute("SELECT COUNT(*) FROM vektoren").fetchone()[0]
    kopie.close()
    if os.path.exists("config.json"):
        shutil.copy2("config.json", os.path.join(ziel, "config.json"))
    print(f"quick_check: {ok}   chunks: {n_k} (source {n_q})   vectors: {n_v}")
    if ok != "ok" or n_k != n_v:
        sys.exit("Error: the backup is not in order.")
    groesse = os.path.getsize(os.path.join(ziel, "see_also.db")) / 1e6
    print(f"Backed up to: {ziel}  ({groesse:.0f} MB, {time.monotonic() - t0:.0f} s)")
    if n_k != n_q:
        print("Note: an index run committed during the backup; the backup holds a consistent state.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""check_index.py - checks see_also.db (the index). Read-only.

Run in the project folder (the server may be running):
    venv/bin/python3 tools/check_index.py [project folder]

Checks: database intact (quick_check), as many chunks as vectors and each
vector belongs to a chunk, next_chunk_id above all IDs, vectors normalized,
no NaN. The former switch --nur-db is still accepted and ignored.
"""
import sys
from pathlib import Path

import numpy as np


def pruefe_nur_db(root) -> int:
    """S3: see_also.db fuer sich pruefen."""
    sys.path.insert(0, str(root))
    import speicher
    fails = []

    def check(name, ok, detail=""):
        print(("ok    " if ok else "ERROR "), name, detail)
        if not ok:
            fails.append(name)

    dbp = root / "see_also.db"
    if not dbp.exists():
        check("see_also.db exists", False)
        return 1
    con = speicher.verbinden(str(dbp), nur_lesen=True)
    check("database intact (quick_check)", con.execute("PRAGMA quick_check").fetchone()[0] == "ok")
    n_c = speicher.anzahl_chunks(con)
    n_v = con.execute("SELECT COUNT(*) FROM vektoren").fetchone()[0]
    check("as many chunks as vectors", n_c == n_v, f"(chunks {n_c}, vectors {n_v})")
    ohne = con.execute("SELECT COUNT(*) FROM chunks c LEFT JOIN vektoren v ON v.id = c.id WHERE v.id IS NULL").fetchone()[0]
    waise = con.execute("SELECT COUNT(*) FROM vektoren v LEFT JOIN chunks c ON c.id = v.id WHERE c.id IS NULL").fetchone()[0]
    check("every chunk has a vector, no vector without a chunk", ohne == 0 and waise == 0,
          f"(without vector {ohne}, orphaned {waise})")
    luecken = con.execute("SELECT COUNT(*) FROM (SELECT path, COUNT(*) n, MAX(position_in_file) m FROM chunks "
                          "GROUP BY path) WHERE m <> n - 1").fetchone()[0]
    print(f"info   files with gaps in position_in_file: {luecken} (expected 0; for information only)")
    state = speicher.zustand_lesen(con)
    max_id = con.execute("SELECT MAX(id) FROM chunks").fetchone()[0] or -1
    check("next_chunk_id above all IDs", state.get("next_chunk_id", 0) > max_id,
          f"(max {max_id}, next {state.get('next_chunk_id')})")
    ids, m = speicher.vektoren_laden(con)
    norms = np.linalg.norm(m, axis=1) if len(m) else np.ones(1)
    check("vectors normalized", bool(np.all(np.abs(norms - 1) < 1e-3)),
          f"(length {norms.min():.4f} to {norms.max():.4f})")
    check("no NaN", not np.isnan(m).any())
    n_docs = con.execute("SELECT COUNT(DISTINCT path) FROM chunks").fetchone()[0]
    print(f"info   PDFs with chunks: {n_docs}, generation {speicher.generation(con)}, "
          f"problem files {len(state.get('problemdateien') or {})}")
    con.close()
    print()
    print("RESULT:", "all fine" if not fails else "failed: " + ", ".join(fails))
    return 1 if fails else 0


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    root = Path(args[0]) if args else Path.cwd()
    return pruefe_nur_db(root)


if __name__ == "__main__":
    sys.exit(main())

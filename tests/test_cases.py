"""Runs every test case in tests/cases/ in its own Python process.

Each case builds a small sample collection in a temporary folder, uses stand-ins
for the embedding model and, where needed, for PyMuPDF and ocrmypdf, and prints
one line per check ("ok" or "FEHLER"). A case passes when it exits with 0 and no
check failed. Network access is blocked for the whole run (tests/_offline).

    pytest                      # all cases
    pytest -k ocr               # cases with "ocr" in the name
    pytest -v -k caching -s     # one case, showing its checks
"""
import os
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
CASES = sorted((ROOT / "tests" / "cases").glob("*.py"))
OFFLINE = ROOT / "tests" / "_offline"


@pytest.mark.parametrize("case", CASES, ids=lambda p: p.stem)
def test_case(case, tmp_path):
    env = dict(os.environ)
    env.pop("SEE_ALSO_DEBUG", None)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(OFFLINE), env.get("PYTHONPATH")]))
    env["HF_HUB_OFFLINE"] = "1"
    env["TRANSFORMERS_OFFLINE"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    r = subprocess.run([sys.executable, str(case), str(ROOT), str(tmp_path / "page.html")],
                       cwd=tmp_path, capture_output=True, text=True, timeout=900, env=env)
    out = r.stdout + r.stderr
    print(out)
    failed = [line for line in out.splitlines() if line.startswith(("FEHLER", "FAIL"))]
    assert r.returncode == 0 and not failed, "\n".join(failed) or out[-4000:]


def test_offline_guard_active(tmp_path):
    """The guard itself works: a connection to the outside is refused."""
    env = dict(os.environ, PYTHONPATH=str(OFFLINE))
    code = ("import socket\n"
            "try:\n    socket.create_connection(('huggingface.co', 443), timeout=3)\n"
            "except OSError as e:\n    print('blocked', e)\n")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd=tmp_path)
    assert "blocked" in r.stdout and ("network access blocked" in r.stdout or "lookup blocked" in r.stdout), \
        r.stdout + r.stderr

# Tests

The tests check see_also without the embedding model, without PyTorch, without
PyMuPDF and ocrmypdf, and without a network connection. Each case in `cases/`
builds a small sample collection in a temporary folder, uses stand-ins where
needed, and prints one line per check (`ok` or `FEHLER` – the check texts are
still German). A case passes when all its checks pass.

## Run

In the project folder:

```
venv/bin/python3 -m pip install pytest      # once; flask and numpy are already there
venv/bin/python3 -m pytest                  # all cases, about 1–2 minutes
venv/bin/python3 -m pytest -k ocr           # only cases with "ocr" in the name
venv/bin/python3 -m pytest -v -s -k caching # one case, showing every check
```

A single case also runs on its own:

```
venv/bin/python3 tests/cases/caching.py .
```

Without the venv, `python3 -m pip install -r tests/requirements.txt` is enough.

## What "offline" means here

`tests/_offline/sitecustomize.py` is loaded into every Python process of the
suite and refuses every network connection except to this computer (the test
server on 127.0.0.1). A case that tried to download the model or call Hugging
Face would fail. `test_offline_guard_active` checks the guard itself.

This proves that the code paths under test need no network. Whether the real
model loads offline on your computer is a separate check (see the README,
"Offline model").

## Notes

- `config_and_messages` starts the real server once on port 5001. If see_also is
  running, that part is skipped with a note; the rest of the case runs.
- GitHub runs the suite on Linux and macOS with Python 3.11 and 3.14 on every push
  (`.github/workflows/tests.yml`).

## Cases

| Case | What it checks |
|---|---|
| `areas_on_status_page` | Areas on the status page: counts, hints, order |
| `caching` | Caching of the folder scan and of the index state; refresh only while visible |
| `config_and_messages` | English config keys with German aliases, debug switch, English messages, tools |
| `contrast_view` | Score contrast view from the search page |
| `embedder_lock` | Model loaded once, never called twice at the same time |
| `index_and_paths` | Folder scan, full and incremental runs, chunk IDs, PDF view route |
| `index_run_process` | Index run in its own process: progress, cancel, crash |
| `isolated_reading` | Same result with and without the isolated reader process |
| `noisy_pdfs` | Many MuPDF warnings do not block reading |
| `obsidian_mailbox` | Requests from Obsidian and the search page picking them up |
| `ocr_embedding` | Text from OCR versions in the index, OCR tag and OCR version route |
| `ocr_run` | OCR run: order, statuses, retries, cancel, crash, reasons, review pages |
| `problem_files` | Crashing, hanging and broken PDFs become problem files; the run continues |
| `search_scope` | Search scope: selection, storage, restricted search |
| `sleep_and_gpu` | Sleep detection during runs, clearing the GPU cache |
| `sqlite_and_tools` | SQLite storage, tools `check_index` and `messwerte` |

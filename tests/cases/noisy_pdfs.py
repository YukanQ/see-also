# Test case: Many MuPDF warnings do not block the reader process.
# Run: python3 noisy_pdfs.py <project folder> [output file]  (or via pytest, see tests/README.md)
import sys, os, types, json, shutil, tempfile, hashlib, time
src=os.path.abspath(sys.argv[1]); T=os.path.realpath(tempfile.mkdtemp())
for f in ["app.py","indexer.py","embedder.py","score_contrast.py","extract_worker.py", "speicher.py", "ocr.py"]: shutil.copy(os.path.join(src,f),T)
shutil.copytree(os.path.join(src,"templates"),T+"/templates"); os.makedirs(T+"/d/X")
for n in ["laut1.pdf","laut2.pdf","z.pdf"]: open(f"{T}/d/X/{n}","wb").write(b"%PDF")
json.dump(dict(pdf_base_dir=T+"/d", pdf_zeitlimit_s=10), open(T+"/config.json","w"))
os.environ["SEE_ALSO_TEST_ATTRAPPE"]="1"; sys.modules["fitz"]=types.ModuleType("fitz"); sys.path.insert(0,T)
import numpy as np, embedder
embedder.embed_passages=lambda ts: np.ones((len(ts),1024),np.float32)/32
import app as A; import speicher as _sp; A.load_meta = lambda: list(_sp.alle_chunks(_sp.verbinden(A.DB_FILE, nur_lesen=True)))
t0=time.monotonic(); A.run_indexing_guarded("full")
print("ok" if A.INDEX_PROGRESS["phase"]=="done" and len(A.load_meta())==6 and not A.load_index_state().get("problemdateien") else "FEHLER", "viele MuPDF-Warnungen blockieren nicht", f"{time.monotonic()-t0:.1f}s")

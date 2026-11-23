# Test case: Same chunks and vectors with and without the isolated reader process.
# Run: python3 isolated_reading.py <project folder> [output file]  (or via pytest, see tests/README.md)
import sys, os, types, json, shutil, tempfile, hashlib, subprocess
src=os.path.abspath(sys.argv[1]); out={}
for iso in (True, False):
    T=os.path.realpath(tempfile.mkdtemp())
    for f in ["app.py","indexer.py","embedder.py","score_contrast.py","extract_worker.py", "speicher.py", "ocr.py"]: shutil.copy(os.path.join(src,f),T)
    shutil.copytree(os.path.join(src,"templates"),T+"/templates")
    d="/tmp/iso_eq_daten"; os.makedirs(d+"/A",exist_ok=True)
    for n in ["Ä1.pdf","b.pdf","leer.pdf"]: p=f"{d}/A/{n}"; open(p,"wb").write(b"%PDF"); os.utime(p,(1e9,1e9))
    json.dump(dict(pdf_base_dir=d, pdf_isoliert_lesen=iso), open(T+"/config.json","w"))
    code=r'''
import sys,os,types,hashlib,json
os.environ["SEE_ALSO_TEST_ATTRAPPE"]="1"; sys.modules["fitz"]=types.ModuleType("fitz"); sys.path.insert(0,os.getcwd())
import numpy as np, embedder, indexer, runpy
if not json.load(open("config.json"))["pdf_isoliert_lesen"]:
    g = {"__file__": os.path.abspath("extract_worker.py")}
    src = open("extract_worker.py").read().split("for line in sys.stdin:")[0]
    sys.argv=["x","0"]; exec(compile(src,"w","exec"), g)
    indexer.extract_page_chunks = g["indexer"].extract_page_chunks
def vec(t):
    r=np.random.default_rng(int(hashlib.md5(t.encode()).hexdigest()[:8],16)); v=r.standard_normal(1024).astype(np.float32); return v/np.linalg.norm(v)
embedder.embed_passages=lambda ts: np.array([vec(t) for t in ts],dtype=np.float32)
import app as A; A.run_indexing_guarded("full"); print(A.INDEX_PROGRESS["phase"])
'''
    r=subprocess.run([sys.executable,"-c",code],cwd=T,capture_output=True,text=True)
    if not os.path.exists(T+"/see_also.db"): print(r.stdout[-800:], r.stderr[-1500:]); sys.exit(1)
    import sqlite3; c=sqlite3.connect(T+"/see_also.db")
    out[iso]=(c.execute("SELECT id,path,position_in_file,seite,zeile,text,text_for_embedding,bbox FROM chunks ORDER BY id").fetchall(),
              c.execute("SELECT id,vektor FROM vektoren ORDER BY id").fetchall(), r.stdout.strip().splitlines()[-1] if r.stdout else r.stderr[-500:])
print(out[True][2], out[False][2])
print("ok" if out[True][:2]==out[False][:2] else "FEHLER", "Chunks und Vektoren identisch mit und ohne Arbeitsprozess", len(out[True][0]))

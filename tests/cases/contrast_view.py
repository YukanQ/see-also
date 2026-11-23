# Test case: Score contrast view (score_contrast.py) from the search page.
# Run: python3 contrast_view.py <project folder> [output file]  (or via pytest, see tests/README.md)
import sys, os, types, json, subprocess, tempfile, shutil, re, random
src = os.path.realpath(sys.argv[1]); sys.modules["fitz"] = types.ModuleType("fitz")
T = tempfile.mkdtemp(); [shutil.copy(os.path.join(src, f), T) for f in ["app.py","indexer.py","embedder.py","score_contrast.py","speicher.py", "ocr.py"]]
shutil.copytree(os.path.join(src,"templates"), T+"/templates")
json.dump({"pdf_base_dir": T}, open(T+"/config.json","w")); sys.path.insert(0, T); os.chdir(T)
import app as A
res=[]
def check(n, ok, d=""): res.append(bool(ok)); print("ok    " if ok else "FEHLER", n, d)
c = A.app.test_client()
check("ohne Suche: Meldung", c.post("/scores/bars").status_code == 400)
for name, sc in [("normal", sorted((random.Random(1).gauss(0.78, 0.03) for _ in range(9104)), reverse=True)), ("flach", [0.5]*50)]:
    A.LAST_SEARCH_ALL_SCORES = list(sc)
    r = c.post("/scores/bars").get_json()
    json.dump(sc, open("/tmp/k.json","w"))
    subprocess.run([sys.executable, "score_contrast.py", "/tmp/k.json", "--out", "/tmp/k.html", "--no-open"], capture_output=True)
    alt = open("/tmp/k.html").read()
    strip = lambda h: re.sub(r"<title>.*?</title>", "", h)
    check(f"{name}: Inhalt wie bisher als Unterprozess", r["ok"] and strip(r["html"]) == strip(alt).replace("k.json", "letzte Suche"), "" )
print(f"ERGEBNIS: {sum(res)}/{len(res)}"); sys.exit(0 if all(res) else 1)

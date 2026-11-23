# Test case: The model is loaded once and never called twice at the same time.
# Run: python3 embedder_lock.py <project folder> [output file]  (or via pytest, see tests/README.md)
import sys, threading, time, types
sys.path.insert(0, sys.argv[1])
import numpy as np, embedder as E
active = [0]; maxactive = [0]; loads = [0]
class FakeModel:
    def encode(self, xs, **kw):
        active[0] += 1; maxactive[0] = max(maxactive[0], active[0])
        time.sleep(0.05)
        active[0] -= 1
        return np.ones((len(xs), 1024), np.float32)
def fake_load():
    if E._model is None:
        loads[0] += 1; time.sleep(0.2); E._model = FakeModel()
    return E._model
E._get_model_locked = fake_load
ts = [threading.Thread(target=E.embed_passages, args=(["a"] * 32,)) for _ in range(4)] + \
     [threading.Thread(target=E.embed_query, args=("q",)) for _ in range(8)]
[t.start() for t in ts]; [t.join() for t in ts]
print("ok" if maxactive[0] == 1 else "FEHLER", "nie zwei Modellaufrufe gleichzeitig", maxactive[0])
print("ok" if loads[0] == 1 else "FEHLER", "Modell nur einmal geladen", loads[0])
print("ok" if E.embed_query("x").shape == (1024,) and E.embed_passages([]).shape == (0, 1024) else "FEHLER", "Rueckgabeformen unveraendert")

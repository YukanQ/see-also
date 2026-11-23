"""
embedder.py - Block C: Embeddings via multilingual-e5-large.

Bekanntes Risiko aus der Erfahrung mit See-Also-I (vault-suggest): Dort
wurde die Embedding-Berechnung ueber einen separaten Subprozess
(subprocess.Popen) ausgefuehrt, um eine bekannte MPS/Metal-Kontext-
Korruption bei langlebigen Prozessen zu vermeiden. See-Also-II berechnet
Embeddings hier zunaechst DIREKT im Hintergrund-Thread des Flask-Prozesses -
bewusste Vereinfachung fuer den ersten funktionierenden Stand, KEIN Beleg,
dass das Risiko hier nicht ebenfalls auftritt. Zeigen sich beim Testen
aehnliche Symptome (Abstuerze, falsche/korrupte Vektoren nach mehreren
Laeufen), waere der Umzug in einen Subprozess der naechste Schritt.

Nachtrag 13.11.2026: Das Risiko ist eingetreten. Absturzbericht vom Referenz-Mac:
Absturz (Speicherzugriffsfehler) in libtorch_cpu bei
MetalShaderLibrary::exec_unary_kernel, im Thread einer Webanfrage
(process_request_thread), waehrend ein Indexlauf lief. Deutung (Annahme,
nicht im torch-Code nachgeprueft): Zwei Threads benutzten das MPS-Modell
gleichzeitig (Einbettung des Laufs und Einbettung einer Suchanfrage), und
der interne Zwischenspeicher fuer Metal-Kernels ist nicht threadsicher.
Abhilfe hier: Alle Zugriffe auf das Modell laufen nacheinander (MODEL_LOCK).
Eine Suche waehrend eines Indexlaufs wartet dadurch bis zum Ende des
aktuellen Stapels (rund 3 s bei 32 Chunks).
"""

from __future__ import annotations

import os
import threading

import numpy as np

MODEL_NAME = "intfloat/multilingual-e5-large"
EMBEDDING_DIM = 1024

_model = None

# Nur ein Thread zugleich darf das Modell laden oder benutzen (siehe oben).
MODEL_LOCK = threading.RLock()

# P1b (19.11.2026): Offline-Betrieb. Auch mit Modell im Zwischenspeicher fragt
# huggingface_hub beim Laden im Netz nach einer neueren Fassung ("You are
# sending unauthenticated requests to the HF Hub"). see_also arbeitet lokal:
# Liegt das Modell im Zwischenspeicher, wird vor dem ersten Import
# HF_HUB_OFFLINE=1 gesetzt. "auto" (Vorgabe) = so; True = immer offline;
# False = nie (z. B. um eine neue Modellfassung zu holen). app.py setzt den
# Wert aus config.json ("modell_offline"). Eine von aussen gesetzte
# Umgebungsvariable HF_HUB_OFFLINE hat Vorrang.
OFFLINE = "auto"
OFFLINE_GESETZT = False


def _hf_cache_dir() -> str:
    if os.environ.get("HF_HUB_CACHE"):
        return os.environ["HF_HUB_CACHE"]
    home = os.environ.get("HF_HOME") or os.path.join(os.path.expanduser("~"), ".cache", "huggingface")
    return os.path.join(home, "hub")


def modell_im_zwischenspeicher() -> bool:
    snap = os.path.join(_hf_cache_dir(), "models--" + MODEL_NAME.replace("/", "--"), "snapshots")
    try:
        return any(os.scandir(snap))
    except OSError:
        return False


def _offline_einstellen() -> None:
    global OFFLINE_GESETZT
    if "HF_HUB_OFFLINE" in os.environ or OFFLINE is False:
        return
    if OFFLINE is True or (OFFLINE == "auto" and modell_im_zwischenspeicher()):
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        OFFLINE_GESETZT = True


def get_model():
    global _model
    with MODEL_LOCK:
        return _get_model_locked()


def _get_model_locked():
    global _model
    if _model is None:
        # Import erst hier, nicht am Modulkopf - vermeidet, dass jede
        # einfache Anfrage (z.B. /documents) den schweren Torch/Transformers-
        # Import bezahlen muss, falls das Modul aus irgendeinem Grund
        # importiert wird, ohne dass Embeddings gebraucht werden.
        _offline_einstellen()            # muss vor dem ersten Import stehen
        from sentence_transformers import SentenceTransformer
        import torch
        device = "mps" if torch.backends.mps.is_available() else "cpu"
        _model = SentenceTransformer(MODEL_NAME, device=device)
    return _model


def embed_passages(texts: list[str]) -> np.ndarray:
    if not texts:
        return np.zeros((0, EMBEDDING_DIM), dtype=np.float32)
    prefixed = [f"passage: {t}" for t in texts]
    with MODEL_LOCK:
        model = _get_model_locked()
        return model.encode(prefixed, normalize_embeddings=True, convert_to_numpy=True)


def embed_query(text: str) -> np.ndarray:
    prefixed = f"query: {text}"
    with MODEL_LOCK:
        model = _get_model_locked()
        return model.encode([prefixed], normalize_embeddings=True, convert_to_numpy=True)[0]


def gpu_vorrat_leeren() -> bool:
    """S5: Gibt den Vorrat an GPU-Puffern frei, den PyTorch auf Apple Silicon
    (MPS) fuer spaetere Stapel aufhebt. Vermutete Ursache dafuer, dass ein
    grosser Lauf auf rund 12 GB waechst (Bibliothek, 16.11.2026), ein kleiner
    nur auf 7 GB. Aendert keine Ergebnisse; kostet etwas Zeit beim naechsten
    Stapel. Gibt False zurueck, wenn es nichts zu leeren gibt (kein MPS)."""
    with MODEL_LOCK:
        try:
            import torch
            if torch.backends.mps.is_available() and hasattr(torch, "mps"):
                torch.mps.empty_cache()
                return True
        except Exception:
            pass
    return False

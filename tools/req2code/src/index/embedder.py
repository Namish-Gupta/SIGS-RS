"""Text -> dense vectors, via sentence-transformers.

Model: ``all-MiniLM-L6-v2``. Chosen because it is small (~80MB), fast on CPU,
and produces 384-dimensional vectors -- meaning the entire eTour corpus is a
matrix of roughly 1200x384 floats, about 1.8MB. That is what makes plain numpy
viable as the "vector database" (see vector_store.py).

A code-aware model is a later ablation row, not a starting point. Establish that
the pipeline works with the boring model first; swapping models is a one-line
change once everything around it is correct.

OFFLINE: the model is downloaded once into ``models/`` and loaded from there
afterwards.

CACHING IS PER-TEXT, NOT PER-CORPUS. Each text is keyed by the hash of its own
content, so editing one method re-embeds one node instead of all 1210. Measured
on eTour: a full rebuild is ~2.7s, a single-file update ~45ms -- 62x cheaper.
That difference is what puts this inside an editor's interaction budget, so the
cache granularity is a load-bearing design decision rather than an optimisation.

The keying still invalidates correctly: change node_doc.py and every text
changes, so every key changes, so everything re-embeds. Stale vectors from an
older document builder cannot survive.
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import numpy as np

MODEL_NAME = "all-MiniLM-L6-v2"
EMBEDDING_DIM = 384

#: Local model store. Committed to .gitignore; re-downloadable.
MODEL_DIR = Path("models")

#: Where cached embedding matrices live.
CACHE_DIR = Path("models/.embcache")

_model = None


def _load_model(model_name: str = MODEL_NAME):
    """Load the sentence-transformer once per process.

    Deferred import: sentence-transformers pulls in torch, which takes a couple
    of seconds. Modules that only need `text_key` should not pay that cost.
    """
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer

        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        _model = SentenceTransformer(
            model_name, cache_folder=str(MODEL_DIR), device="cpu"
        )
    return _model


def text_key(text: str, model_name: str = MODEL_NAME) -> str:
    """Content hash of ONE text -- the per-node cache key.

    Includes the model name so switching models cannot silently reuse vectors
    from a different embedding space.
    """
    digest = hashlib.sha256(model_name.encode())
    digest.update(b"\0")
    digest.update(text.encode("utf-8", errors="replace"))
    return digest.hexdigest()[:20]


def _store_path(cache_name: str) -> Path:
    return CACHE_DIR / f"{cache_name}.npz"


def _load_store(cache_name: str) -> dict[str, np.ndarray]:
    """Read a ``key -> vector`` store off disk. Missing or corrupt file -> empty.

    Corruption is treated as a cache miss rather than an error: the cache is
    derived data, and refusing to start because a derived file is damaged would
    be a worse failure than paying to recompute it.
    """
    path = _store_path(cache_name)
    if not path.exists():
        return {}
    try:
        with np.load(path, allow_pickle=False) as data:
            keys = data["keys"]
            vecs = data["vecs"]
        if vecs.ndim != 2 or vecs.shape[1] != EMBEDDING_DIM:
            return {}
        return {str(k): vecs[i] for i, k in enumerate(keys)}
    except (OSError, ValueError, KeyError):
        return {}


def _save_store(cache_name: str, store: dict[str, np.ndarray]) -> None:
    """Persist the store. Written to a temp file and renamed, so an interrupted
    write leaves the previous cache intact rather than a truncated one.

    NOTE: `np.savez` appends ``.npz`` to any path that does not already end in
    it, so passing a ``.tmp`` filename writes somewhere else entirely and the
    rename below silently finds nothing. Handing it an open file object is what
    stops numpy renaming the target out from under us.
    """
    if not store:
        return
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _store_path(cache_name)
    tmp = path.with_name(path.name + ".tmp")
    keys = np.array(list(store.keys()))
    vecs = np.stack(list(store.values())).astype(np.float32)
    try:
        with open(tmp, "wb") as fh:
            np.savez(fh, keys=keys, vecs=vecs)
        os.replace(tmp, path)
    except OSError as exc:
        # A cache that cannot be written is a performance problem, not a
        # correctness one -- so don't crash. But don't hide it either: a silent
        # failure here looks exactly like a working cache that never hits.
        print(f"[embedder] could not write {path}: {exc}", file=sys.stderr)
        tmp.unlink(missing_ok=True)


def embed_texts(
    texts: list[str],
    model_name: str = MODEL_NAME,
    cache_name: str | None = None,
    show_progress: bool = False,
) -> np.ndarray:
    """Embed a list of texts into an L2-normalised matrix.

    Args:
        texts: Documents to embed -- requirement texts or node documents.
        model_name: sentence-transformers model id.
        cache_name: Optional store label ("nodes", "reqs"). When given, only
            texts whose content hash is absent from the store are embedded.
        show_progress: Show the encoding progress bar.

    Returns:
        Array of shape ``(len(texts), EMBEDDING_DIM)``, dtype float32, with each
        row L2-normalised.

    Why normalise here rather than at search time: once every vector is unit
    length, cosine similarity is exactly the dot product. That collapses the
    entire search step into one matrix multiply (see vector_store.py) with no
    per-query division and no risk of normalising twice.
    """
    if not texts:
        return np.zeros((0, EMBEDDING_DIM), dtype=np.float32)

    if cache_name is None:
        return _encode(texts, model_name, show_progress)

    store = _load_store(cache_name)
    keys = [text_key(t, model_name) for t in texts]

    # De-duplicate misses: the same text appearing twice is embedded once.
    missing: dict[str, str] = {}
    for key, text in zip(keys, texts, strict=True):
        if key not in store:
            missing.setdefault(key, text)

    if missing:
        fresh = _encode(list(missing.values()), model_name, show_progress)
        for key, vec in zip(missing.keys(), fresh, strict=True):
            store[key] = vec
        _save_store(cache_name, store)

    return np.stack([store[k] for k in keys]).astype(np.float32)


def _encode(texts: list[str], model_name: str, show_progress: bool) -> np.ndarray:
    model = _load_model(model_name)
    return model.encode(
        texts,
        batch_size=64,
        convert_to_numpy=True,
        normalize_embeddings=True,  # unit length -> dot product == cosine
        show_progress_bar=show_progress,
    ).astype(np.float32)


def is_offline() -> bool:
    """Whether HF is pinned offline. The demo asserts this indirectly by working."""
    return os.environ.get("HF_HUB_OFFLINE") == "1"


def model_is_cached(model_dir: Path = MODEL_DIR) -> bool:
    """Whether a downloaded model is already present in `model_dir`."""
    return any(model_dir.glob("models--*/snapshots/*"))


def pin_offline_if_cached(model_dir: Path = MODEL_DIR) -> bool:
    """Pin HuggingFace offline when the model is already on disk. Returns the state.

    Call this from an entry point BEFORE sentence-transformers is imported.

    Without it, the loader revalidates a model it already has by making ~20 HEAD
    requests to huggingface.co -- measured at 15.2s of a 15.5s startup, versus
    3.4s offline. That is slow, it makes a flaky network or an HF outage able to
    stall a run, and it quietly contradicts this project's stated offline
    guarantee: the demo is supposed to make no network calls, and it was making
    twenty.

    Guarded on the model being present so the documented first-run download
    still works. An explicit HF_HUB_OFFLINE in the environment always wins, so a
    caller can still force either behaviour.
    """
    if "HF_HUB_OFFLINE" in os.environ:
        return is_offline()
    if model_is_cached(model_dir):
        os.environ["HF_HUB_OFFLINE"] = "1"
        return True
    return False

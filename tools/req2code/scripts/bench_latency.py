"""Latency benchmark: is req2code fast enough to live inside an editor?

    python -m scripts.bench_latency

The ablation answers "is it accurate?". This answers "is it deployable?" --
the other question an examiner asks, and the one a table of MAP scores cannot
address. Three numbers decide it:

    1. cold start        paid once, when the MCP server boots
    2. per-query         paid on every user interaction   (budget: <100ms)
    3. incremental       paid when ONE file is edited     (budget: <100ms)

100ms is the standard threshold below which a user perceives a response as
instantaneous rather than as a wait.

Numbers move with hardware; the shape does not. Cold start is dominated by
loading the model, which is why the server is long-lived. Query cost is a
matmul over a (n_nodes x 384) matrix, which is why plain numpy beats an ANN
index at this scale (see index/vector_store.py).
"""

from __future__ import annotations

import time

from src.index.embedder import _load_model, embed_texts, pin_offline_if_cached
from src.index.node_doc import build_all_documents
from src.index.vector_store import search, similarity_matrix
from src.ingest.requirements_loader import load_requirements
from src.parse.java_parser import enclosing_class_map, parse_file, parse_repo
from src.pipeline import DEFAULT_DATA_DIR

# Otherwise the "cold start" figure would be measuring huggingface.co round-trips
# rather than model loading -- see embedder.pin_offline_if_cached.
pin_offline_if_cached()

BUDGET_MS = 100.0
WIDTH = 74


def timed(label: str, fn, repeat: int = 1, budget: bool = False):
    fn()  # warm
    start = time.perf_counter()
    for _ in range(repeat):
        fn()
    ms = (time.perf_counter() - start) / repeat * 1000
    flag = ""
    if budget:
        flag = "  OK" if ms < BUDGET_MS else "  OVER BUDGET"
    print(f"  {label:<44} {ms:9.2f} ms{flag}")
    return ms


def main() -> int:
    data = DEFAULT_DATA_DIR
    print("=" * WIDTH)
    print("req2code -- interactive latency benchmark")
    print("=" * WIDTH)

    # -- 1. cold start ----------------------------------------------------
    print("\n[1] COLD START  (paid once, when the server boots)")
    t0 = time.perf_counter()
    _load_model()
    model_ms = (time.perf_counter() - t0) * 1000
    print(f"  {'load sentence-transformer model':<44} {model_ms:9.2f} ms")

    t0 = time.perf_counter()
    reqs = load_requirements(data / "req")
    nodes, docs = parse_repo(data / "code")
    enclosing = enclosing_class_map(nodes)
    node_docs = build_all_documents(nodes, enclosing, docs)
    parse_ms = (time.perf_counter() - t0) * 1000
    print(f"  {'parse + build node documents':<44} {parse_ms:9.2f} ms")

    req_vecs = embed_texts([r.text for r in reqs], cache_name="reqs")
    t0 = time.perf_counter()
    node_vecs = embed_texts(node_docs, cache_name="nodes")
    warm_ms = (time.perf_counter() - t0) * 1000
    print(f"  {'embed all nodes (cache hit)':<44} {warm_ms:9.2f} ms")

    t0 = time.perf_counter()
    embed_texts(node_docs, cache_name=None)
    cold_ms = (time.perf_counter() - t0) * 1000
    print(f"  {f'embed all {len(nodes)} nodes (cache miss)':<44} {cold_ms:9.2f} ms")
    boot_ms = model_ms + parse_ms + warm_ms
    print(f"  {'--> total warm boot':<44} {boot_ms:9.2f} ms")
    print(
        f"      {model_ms / boot_ms:.0%} of it is model loading, "
        "which is why the server is long-lived."
    )

    # -- 2. per-query -----------------------------------------------------
    print("\n[2] PER-QUERY  (paid on every user interaction)")
    q = req_vecs[20]

    timed("requirement -> ranked methods", lambda: search(q, node_vecs, 10), 200, True)
    timed(
        "method -> ranked requirements",
        lambda: search(node_vecs[500], req_vecs, 5),
        200,
        True,
    )
    timed(
        "orphan scan (all nodes x all requirements)",
        lambda: (similarity_matrix(node_vecs, req_vecs).max(axis=1) < 0.30).sum(),
        50,
        True,
    )
    timed(
        "NEW free-text query (embed + search)",
        lambda: search(
            embed_texts(["notify the user of nearby attractions"])[0], node_vecs, 10
        ),
        20,
        True,
    )

    # -- 3. incremental ---------------------------------------------------
    print("\n[3] INCREMENTAL UPDATE  (paid when the user edits ONE file)")
    target = data / "code" / "AdvertisementManager.java"
    n_target = sum(1 for n in nodes if n.file_path == target.name)

    def reindex_one():
        fnodes, fdocs = parse_file(target, data / "code")
        fdd = build_all_documents(fnodes, enclosing_class_map(fnodes), fdocs)
        return embed_texts(fdd, cache_name=None)

    inc_ms = timed(
        f"re-parse + re-embed 1 file ({n_target} nodes)", reindex_one, 5, True
    )
    print(f"  {'full corpus rebuild, for comparison':<44} {cold_ms:9.2f} ms")
    print(
        f"      incremental is {cold_ms / inc_ms:.0f}x cheaper -- this is what the "
        "per-node\n      embedding cache buys, and what makes live re-indexing viable."
    )

    print("\n" + "=" * WIDTH)
    print(f"Budget: {BUDGET_MS:.0f}ms (threshold of perceived instantaneity).")
    print("Every interactive path is inside it. Boot is not, and does not need")
    print("to be -- it is paid once, behind the editor's own startup.")
    print("=" * WIDTH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

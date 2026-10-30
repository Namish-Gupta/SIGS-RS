"""The Review 1 demo. One command, offline, under 60 seconds.

    python -m scripts.run_demo

Prints one table covering all three novelty claims on a deliberately narrow
slice of eTour:

    (a) one requirement -> ranked method-level code nodes      [claim 1]
    (b) the same requirement through the TF-IDF file baseline  [the comparison]
    (c) one flagged orphan code node                           [claim 2]
    (d) one cached LLM justification                           [claim 3]

Design rule for this script: a complete narrow slice, not a half-built system.
The whole pipeline on one requirement is a far better demonstration than half
the pipeline on all fifty-eight -- it proves the architecture works end to end,
and scaling it up is then just a parameter.

Everything here reads from disk. No network calls, no model downloads at demo
time, no API keys.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from src.index.embedder import pin_offline_if_cached
from src.pipeline import DEFAULT_DATA_DIR, load_corpus

# Make the offline guarantee unconditional rather than incidental. Called at
# import, before sentence-transformers loads, so a warm model never revalidates
# over the network. See embedder.pin_offline_if_cached.
pin_offline_if_cached()

#: Requirement featured in the demo. UC20 ("InsertBanner") is chosen because its
#: top-ranked node is a genuine gold link with substantial Javadoc, and because a
#: cached justification exists for it. Override with --req-id.
DEMO_REQ_ID = "UC20.txt"

WIDTH = 100


def _rule(char: str = "-") -> str:
    return char * WIDTH


def _truncate(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--req-id", type=str, default=DEMO_REQ_ID)
    args = parser.parse_args()

    started = time.perf_counter()

    # ---- 1. Load the corpus ------------------------------------------------
    print(_rule("="))
    print("req2code -- Requirement-to-Code Traceability via AST-Aware Retrieval")
    print(_rule("="))
    print("\n[1] Corpus")
    corpus = load_corpus(args.data_dir)
    n_files = len({n.file_path for n in corpus.nodes})
    print(f"    requirements      : {len(corpus.requirements)}")
    print(f"    java files        : {n_files}")
    n_docs = len(corpus.docs)
    print(f"    AST nodes parsed  : {len(corpus.nodes)}  ({n_docs} with Javadoc)")
    print(f"    gold links        : {sum(len(v) for v in corpus.gold.values())}")

    reqs = {r.req_id: r for r in corpus.requirements}
    if args.req_id not in reqs:
        print(f"\n    Unknown requirement {args.req_id!r}.")
        return 1
    req = reqs[args.req_id]
    gold = corpus.gold.get(req.req_id, set())

    # ---- 2. The node document (the highest-leverage step) ------------------
    from src.index.node_doc import split_identifier

    print("\n[2] Identifier splitting -- what bridges prose and code")
    for example in ("insertBanner", "getMaxBanner", "IDBRefreshmentPoint"):
        print(f"    {example:22} -> {' '.join(split_identifier(example))}")

    # ---- 3. The requirement -----------------------------------------------
    print(f"\n[3] Requirement {req.req_id}")
    for line in req.text.splitlines()[:2]:
        if line.strip():
            print(f"    {_truncate(line, WIDTH - 6)}")
    print(f"    gold files: {', '.join(sorted(gold))}")

    # ---- 4. Our method: node-level retrieval ------------------------------
    from src.eval.ablation import ABLATION_RUNS, run_one
    from src.eval.metrics import (
        aggregate_to_file_level,
        rows_to_ranked_lists,
        truncate_to_k,
    )

    by_id = {n.node_id: n for n in corpus.nodes}
    node_to_file = corpus.node_to_file
    configs = {c.run_id: c for c in ABLATION_RUNS}

    e1_rows = run_one(configs["E1"], corpus, top_k=args.top_k)
    e1_ranked = rows_to_ranked_lists([r for r in e1_rows if r.req_id == req.req_id])
    e1_scores = {r.artifact_id: r.score for r in e1_rows if r.req_id == req.req_id}

    print(f"\n[4] E1 -- OUR METHOD: embeddings over AST nodes (top {args.top_k})")
    print(f"    {'#':<3} {'score':<7} {'method':<40} {'location':<32} hit")
    print(f"    {_rule()[: WIDTH - 4]}")
    for rank, node_id in enumerate(e1_ranked.get(req.req_id, [])[: args.top_k], 1):
        node = by_id[node_id]
        enclosing = corpus.enclosing.get(node_id, "")
        label = f"{enclosing}.{node.name}()" if enclosing else f"{node.name}()"
        loc = f"{node.file_path}:{node.start_line}-{node.end_line}"
        hit = "GOLD" if node.file_path in gold else ""
        print(
            f"    {rank:<3} {e1_scores[node_id]:<7.3f} {_truncate(label, 40):<40} "
            f"{_truncate(loc, 32):<32} {hit}"
        )

    # ---- 5. The baseline: TF-IDF over whole files --------------------------
    b0_rows = run_one(configs["B0"], corpus, top_k=args.top_k)
    b0_ranked = rows_to_ranked_lists([r for r in b0_rows if r.req_id == req.req_id])
    b0_scores = {r.artifact_id: r.score for r in b0_rows if r.req_id == req.req_id}

    print(f"\n[5] B0 -- BASELINE: TF-IDF over whole files (top {args.top_k})")
    print(f"    {'#':<3} {'score':<7} {'file':<73} hit")
    print(f"    {_rule()[: WIDTH - 4]}")
    for rank, artifact in enumerate(b0_ranked.get(req.req_id, [])[: args.top_k], 1):
        hit = "GOLD" if artifact in gold else ""
        score = b0_scores[artifact]
        print(f"    {rank:<3} {score:<7.3f} {_truncate(artifact, 73):<73} {hit}")
    print(
        "\n    The baseline can only point at files. E1 points at the exact method\n"
        "    and line range -- that is novelty claim #1."
    )

    # ---- 6. Metrics side by side -----------------------------------------
    from src.eval.metrics import evaluate

    print("\n[6] Whole-corpus comparison (58 requirements, file-level scoring)")
    header = f"{'run':<5} {'representation':<16} {'granularity':<12} {'MAP':<8} R@10"
    print(f"    {header}")
    print(f"    {_rule()[: WIDTH - 4]}")
    for run_id in ("B0", "B1", "E0", "E1"):
        cfg = configs[run_id]
        rows = run_one(cfg, corpus, top_k=10)
        file_rows = truncate_to_k(aggregate_to_file_level(rows, node_to_file), 10)
        m = evaluate(file_rows, corpus.gold)
        marker = "  <-- our method" if run_id == "E1" else ""
        print(
            f"    {run_id:<5} {cfg.representation:<16} {cfg.granularity:<12} "
            f"{m['MAP']:<8.3f} {m['R@10']:<8.3f}{marker}"
        )

    # ---- 7. Orphan detection ---------------------------------------------
    from src.retrieve.orphans import (
        DEFAULT_THRESHOLD,
        best_requirement_score,
        find_orphans,
        score_distribution,
    )

    best = best_requirement_score(corpus.node_vectors, corpus.req_vectors)
    dist = score_distribution(best)
    orphans = find_orphans(
        corpus.nodes,
        corpus.node_vectors,
        corpus.req_vectors,
        threshold=DEFAULT_THRESHOLD,
    )

    n_methods = sum(1 for n in corpus.nodes if n.kind == "method")
    tight = find_orphans(
        corpus.nodes, corpus.node_vectors, corpus.req_vectors, threshold=dist["p5"]
    )

    print("\n[7] Orphan detection -- code no requirement claims (novelty claim #2)")
    print(
        f"    best-match cosine distribution over {n_methods} methods:\n"
        f"      min={dist['min']:.3f} p5={dist['p5']:.3f} p25={dist['p25']:.3f} "
        f"median={dist['median']:.3f} max={dist['max']:.3f}"
    )
    print(
        f"\n    threshold {DEFAULT_THRESHOLD:.2f} flags {len(orphans)} methods "
        f"({len(orphans) / n_methods:.0%}) -- too many to review by hand."
    )
    print(
        f"    threshold {dist['p5']:.2f} (the 5th percentile) flags {len(tight)} "
        f"({len(tight) / n_methods:.0%}) -- a reviewable set."
    )
    print(
        "    The 0.30 default is an untuned guess, NOT a result. The defensible\n"
        "    presentation is the ranked list plus this distribution, letting the\n"
        "    reader pick the cutoff. Calibrating it is open work."
    )
    print("\n    most-orphaned methods:")
    for node, score in orphans[:3]:
        enclosing = corpus.enclosing.get(node.node_id, "")
        label = f"{enclosing}.{node.name}()" if enclosing else f"{node.name}()"
        plumbing = (
            node.name.startswith(("get", "set", "is", "to"))
            or node.end_line - node.start_line <= 2
        )
        tag = "  [likely plumbing]" if plumbing else "  [worth review]"
        print(
            f"      {score:.3f}  {_truncate(label, 44):<44} "
            f"{node.file_path}:{node.start_line}{tag}"
        )
    print(
        "\n    Honest caveat: most orphans in any real codebase are getters, logging,\n"
        "    and framework glue that legitimately implement no requirement. The claim\n"
        "    is that this surfaces a small reviewable set -- not that every flagged\n"
        "    node is a defect."
    )

    # ---- 8. Cached justification ----------------------------------------
    from src.justify.justifier import get_justification, load_cache

    print("\n[8] Trace justification (novelty claim #3) -- read from committed cache")
    cache = load_cache()
    shown = False
    for node_id in e1_ranked.get(req.req_id, []):
        text = get_justification(req, by_id[node_id])
        if text:
            entry = next((e for e in cache.values() if e.get("node_id") == node_id), {})
            print(f"    {req.req_id} -> {node_id}")
            print(f"    model: {entry.get('model', 'unknown')}")
            print()
            for line in _wrap(text, WIDTH - 6):
                print(f"      {line}")
            shown = True
            break
    if not shown:
        print("    (no cached justification for this requirement's top nodes)")
        print("    Generate with: python -m scripts.generate_justifications")

    # ---- footer ----------------------------------------------------------
    elapsed = time.perf_counter() - started
    print("\n" + _rule("="))
    print("METHODOLOGY NOTE -- stated openly, not hidden:")
    print("  eTour gold links map requirements to FILES, but we retrieve METHODS.")
    print("  All metrics above aggregate node scores to file level by max (a file")
    print("  scores as well as its best method) so E1 and B0 are directly")
    print("  comparable. Node-level output is reported qualitatively, in [4].")
    print(f"\nCompleted in {elapsed:.1f}s, fully offline.")
    print(_rule("="))
    return 0


def _wrap(text: str, width: int) -> list[str]:
    """Minimal greedy word wrap. Avoids a textwrap import for four lines of use."""
    words, lines, current = text.split(), [], ""
    for word in words:
        if current and len(current) + 1 + len(word) > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines


if __name__ == "__main__":
    raise SystemExit(main())

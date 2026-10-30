"""Generate LLM justifications and cache them to disk. ONLINE -- run manually.

    python -m scripts.generate_justifications --top-n 5

This is the ONLY script in the project that makes a network call. It is never
invoked by the demo or the ablation: it writes `src/justify/cache/justifications.json`,
that file is committed, and everything downstream reads it back offline.

Requires the `anthropic` package and credentials:
    pip install anthropic
    setx ANTHROPIC_API_KEY "sk-ant-..."     # or: ant auth login

Without either, the script exits with a clear message and changes nothing --
the committed cache remains valid.
"""

from __future__ import annotations

import argparse
import os

from src.contracts import CodeNode, Requirement
from src.justify.justifier import (
    CACHE_FILE,
    PROMPT_VERSION,
    build_prompt,
    get_justification,
    load_cache,
    store_justification,
)
from src.pipeline import DEFAULT_DATA_DIR, load_corpus

#: Model used for justification generation. Logged into every cache entry so the
#: provenance of committed text is never in doubt.
MODEL = "claude-opus-5"


#: Framework callbacks and trivial accessors. These are excluded from
#: justification because they are the "boring plumbing" case: a Swing
#: `actionPerformed` handler may sit inside a gold file, but explaining why a
#: generic event callback satisfies a requirement produces a vacuous rationale.
#: Excluded from *justification selection only* -- they remain fully eligible for
#: retrieval and evaluation, so this does not touch any reported metric.
_UNINTERESTING_PREFIXES = ("actionPerformed", "get", "set", "is", "main", "run")


def _pick_pairs(corpus, top_n: int) -> list[tuple[Requirement, CodeNode]]:
    """Choose (requirement, top-ranked node) pairs worth justifying.

    Picks requirements whose top-1 node lands inside a genuine gold file, so the
    demo shows a justification for a trace that is actually correct. A
    justification of a wrong trace is also interesting, but not what Review 1
    should lead with.

    Among correct traces, prefers nodes with substantial Javadoc: those give the
    LLM real material to reason over, and a rationale grounded in the code's own
    documentation is the most convincing form of the claim.
    """
    from src.eval.ablation import ABLATION_RUNS, run_one
    from src.eval.metrics import rows_to_ranked_lists

    config = next(c for c in ABLATION_RUNS if c.run_id == "E1")
    ranked = rows_to_ranked_lists(run_one(config, corpus, top_k=1))

    by_id = {n.node_id: n for n in corpus.nodes}
    reqs = {r.req_id: r for r in corpus.requirements}
    node_to_file = corpus.node_to_file

    candidates: list[tuple[int, str, Requirement, CodeNode]] = []
    for req_id, node_ids in ranked.items():
        gold = corpus.gold.get(req_id, set())
        if not node_ids or not gold:
            continue
        node = by_id[node_ids[0]]
        if node_to_file[node.node_id] not in gold:
            continue
        if node.kind not in ("method", "constructor"):
            continue
        if node.name.startswith(_UNINTERESTING_PREFIXES):
            continue
        doc_len = len(corpus.docs.get(node.node_id, ""))
        if doc_len == 0:
            continue
        candidates.append((doc_len, req_id, reqs[req_id], node))

    # Richest documentation first; req_id as a tiebreak so selection is stable.
    candidates.sort(key=lambda c: (-c[0], c[1]))
    return [(req, node) for _, _, req, node in candidates[:top_n]]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=str, default=str(DEFAULT_DATA_DIR))
    parser.add_argument("--top-n", type=int, default=5)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the prompts that would be sent, make no network call.",
    )
    args = parser.parse_args()

    corpus = load_corpus(args.data_dir)
    pairs = _pick_pairs(corpus, args.top_n)
    print(f"Selected {len(pairs)} (requirement, node) pairs.")

    # Dry run first: it makes no network call, so it must not require the SDK.
    if args.dry_run:
        for req, node in pairs:
            print(f"\n{'=' * 70}\n{req.req_id} -> {node.node_id}\n{'=' * 70}")
            print(build_prompt(req, node)[:1200])
        return 0

    try:
        import anthropic
    except ImportError:
        print(
            "The `anthropic` package is not installed.\n"
            "  pip install anthropic\n"
            f"The committed cache at {CACHE_FILE} is unaffected."
        )
        return 1

    if not os.environ.get("ANTHROPIC_API_KEY"):
        # Not fatal: the SDK also resolves `ant auth login` profiles.
        print("ANTHROPIC_API_KEY not set; relying on an `ant auth login` profile.")

    client = anthropic.Anthropic()
    before = len(load_cache())
    written = 0

    for req, node in pairs:
        if get_justification(req, node) is not None:
            print(f"  cached, skipping: {req.req_id} -> {node.name}")
            continue

        try:
            response = client.beta.messages.create(
                model=MODEL,
                max_tokens=1024,
                betas=["server-side-fallback-2026-07-01"],
                # Opus 5's safety classifiers can decline a request; `default`
                # re-runs it on Anthropic's recommended fallback rather than
                # handing us a refusal. Cheap insurance for an unattended script.
                fallbacks="default",
                messages=[{"role": "user", "content": build_prompt(req, node)}],
            )
        except anthropic.APIError as exc:
            print(f"  FAILED {req.req_id} -> {node.name}: {exc}")
            continue

        # A refusal is a successful HTTP 200 with an empty/partial content list,
        # so stop_reason must be checked before indexing into content.
        if response.stop_reason == "refusal":
            print(f"  refused: {req.req_id} -> {node.name}")
            continue

        text = "".join(b.text for b in response.content if b.type == "text").strip()
        if not text:
            print(f"  empty response: {req.req_id} -> {node.name}")
            continue

        store_justification(req, node, text, model=response.model)
        written += 1
        print(f"  wrote: {req.req_id} -> {node.name}")

    print(
        f"\n{written} new justifications ({before} -> {len(load_cache())} total), "
        f"prompt {PROMPT_VERSION}, model {MODEL}.\n"
        f"Commit {CACHE_FILE} so the demo runs offline."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

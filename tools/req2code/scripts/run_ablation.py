"""Run the full ablation table and write results.

    python -m scripts.run_ablation

Not part of the Review 1 demo -- this is the evaluation run that produces the
numbers for the report. Slower than the demo and allowed to be.

Writes:
    results/{run_id}.csv    raw ranked rows, frozen schema (see contracts.py)
    results/ablation.md     the summary table, pasteable into the report
    results/config.json     every RunConfig plus versions, timings and seeds
"""

from __future__ import annotations

import argparse
from pathlib import Path

from src.eval.ablation import run_ablation
from src.index.embedder import pin_offline_if_cached
from src.pipeline import DEFAULT_DATA_DIR, load_corpus

# Reported numbers must not depend on network reachability -- see
# embedder.pin_offline_if_cached.
pin_offline_if_cached()

DEFAULT_RESULTS_DIR = Path("results")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS_DIR)
    parser.add_argument("--top-k", type=int, default=10)
    args = parser.parse_args()

    print("Loading corpus...")
    corpus = load_corpus(args.data_dir, verbose=True)
    print(
        f"  {len(corpus.requirements)} requirements, {len(corpus.nodes)} nodes, "
        f"{sum(len(v) for v in corpus.gold.values())} gold links"
    )

    print("\nRunning ablation...")
    table = run_ablation(corpus, args.results_dir, top_k=args.top_k)

    best = max(table, key=lambda r: table[r]["MAP"])
    print(f"\nBest MAP: {best} ({table[best]['MAP']:.3f})")
    print(f"Wrote {args.results_dir}/ablation.md and config.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

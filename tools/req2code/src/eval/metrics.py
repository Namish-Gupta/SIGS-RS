"""Retrieval metrics, evaluated at file level.

Why file level, when the entire point of the project is node-level retrieval?

Because the gold links are file-level. Scoring node-level predictions against
file-level truth would be comparing different things, and any number produced
that way would be indefensible under questioning. So:

    1. Retrieve at node level (that is the contribution).
    2. Aggregate node scores up to file level using MAX -- a file scores as well
       as its single best-matching method.
    3. Compute metrics at file level, where they are directly comparable to the
       TF-IDF baseline and to published results on eTour.
    4. Report node-level output qualitatively, in the demo table.

Max-aggregation, rather than mean, because a requirement is typically satisfied
by one or two methods inside a large class. Averaging over the class's other
thirty methods would bury exactly the signal we are trying to demonstrate.

This tradeoff must appear in the demo output and the README. It is a limitation
stated openly, not one hidden.
"""

from __future__ import annotations

from collections.abc import Sequence

from src.contracts import ResultRow


def aggregate_to_file_level(
    rows: Sequence[ResultRow], node_to_file: dict[str, str]
) -> list[ResultRow]:
    """Collapse node-level results into file-level results by max score.

    Args:
        rows: Node-level rows for one or more runs.
        node_to_file: `CodeNode.node_id` -> `CodeNode.file_path`.

    Returns:
        File-level rows, re-ranked densely from 1 within each (run_id, req_id).

    Re-ranking after aggregation is essential: carrying the old node ranks
    through would leave gaps (ranks 1, 4, 9...) and quietly corrupt any
    rank-sensitive metric such as MAP or MRR.
    """
    best: dict[tuple[str, str], dict[str, float]] = {}
    for row in rows:
        file_path = node_to_file.get(row.artifact_id, row.artifact_id)
        key = (row.run_id, row.req_id)
        bucket = best.setdefault(key, {})
        if file_path not in bucket or row.score > bucket[file_path]:
            bucket[file_path] = row.score

    out: list[ResultRow] = []
    for (run_id, req_id), bucket in best.items():
        ranked = sorted(bucket.items(), key=lambda kv: -kv[1])
        for rank, (file_path, score) in enumerate(ranked, start=1):
            out.append(
                ResultRow(
                    run_id=run_id,
                    req_id=req_id,
                    artifact_id=file_path,
                    score=score,
                    rank=rank,
                )
            )
    return out


def truncate_to_k(rows: Sequence[ResultRow], k: int) -> list[ResultRow]:
    """Keep only the top-k rows per (run_id, req_id).

    This is what makes the node-vs-file comparison fair, and it is not optional.

    Node-level runs retrieve top-k *nodes*, but several nodes collapse into the
    same file during aggregation -- measured on eTour, 10 nodes yield only ~5.8
    distinct files (as few as 2). A file-level run offers exactly 10. Comparing
    R@10 across those two would be comparing candidate-list lengths, not
    retrieval quality, and would understate the node-level runs badly.

    So node-level runs fetch a deep pool of nodes upstream (see
    NODE_FETCH_MULTIPLIER in eval/ablation.py), aggregate to files, and are then
    truncated here. Every run is evaluated on exactly k FILE candidates.
    """
    kept: list[ResultRow] = []
    for row in rows:
        if row.rank <= k:
            kept.append(row)
    return kept


def rows_to_ranked_lists(rows: Sequence[ResultRow]) -> dict[str, list[str]]:
    """Group rows into ``req_id -> [artifact_id, ...]`` ordered by rank."""
    grouped: dict[str, list[tuple[int, str]]] = {}
    for row in rows:
        grouped.setdefault(row.req_id, []).append((row.rank, row.artifact_id))
    return {
        req_id: [artifact for _, artifact in sorted(pairs)]
        for req_id, pairs in grouped.items()
    }


def precision_at_k(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    """Fraction of the top-k retrieved artifacts that are in the gold set."""
    if k <= 0:
        return 0.0
    top = ranked[:k]
    if not top:
        return 0.0
    return sum(1 for a in top if a in relevant) / len(top)


def recall_at_k(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    """Fraction of the gold set that appears in the top-k retrieved artifacts.

    The headline metric for traceability recovery: an engineer would rather see
    a few false positives than miss a real link, so recall is weighted more
    heavily than precision in this domain.
    """
    if not relevant:
        return 0.0
    return sum(1 for a in ranked[:k] if a in relevant) / len(relevant)


def average_precision(ranked: Sequence[str], relevant: set[str]) -> float:
    """Average precision for a single query -- the per-query term inside MAP.

    Rewards putting correct links near the top, not merely finding them.

    Divides by ``len(relevant)`` rather than by the number of hits found, so a
    system that retrieves 2 of 6 gold links cannot score 1.0. That is the
    standard definition and the conservative one.
    """
    if not relevant:
        return 0.0
    hits = 0
    precision_sum = 0.0
    for i, artifact in enumerate(ranked, start=1):
        if artifact in relevant:
            hits += 1
            precision_sum += hits / i
    return precision_sum / len(relevant)


def mean_average_precision(
    ranked_per_req: dict[str, Sequence[str]], gold: dict[str, set[str]]
) -> float:
    """MAP across all requirements. The single number the ablation table sorts on.

    Requirements with no gold links are excluded rather than scored 0.0.
    Including them would drag every configuration down by the same constant --
    flattering nothing, but making the numbers non-comparable with published
    eTour results.
    """
    scores = [
        average_precision(ranked_per_req.get(req_id, []), relevant)
        for req_id, relevant in gold.items()
        if relevant
    ]
    return sum(scores) / len(scores) if scores else 0.0


def evaluate(
    rows: Sequence[ResultRow], gold: dict[str, set[str]], ks: Sequence[int] = (5, 10)
) -> dict[str, float]:
    """Compute the full metric set for one run's file-level rows.

    With no gold links -- an unlabelled repository -- every metric is 0.0 rather
    than an error. Retrieval still works there (see eval/gold_loader.py); it just
    cannot be scored, and returning zeros keeps that an honest "unmeasured"
    rather than a crash in a caller that only wanted the rankings.
    """
    ranked = rows_to_ranked_lists(rows)
    graded = {r: g for r, g in gold.items() if g}

    metrics: dict[str, float] = {"MAP": mean_average_precision(ranked, gold)}
    for k in ks:
        if not graded:
            metrics[f"P@{k}"] = 0.0
            metrics[f"R@{k}"] = 0.0
            continue
        metrics[f"P@{k}"] = sum(
            precision_at_k(ranked.get(r, []), g, k) for r, g in graded.items()
        ) / len(graded)
        metrics[f"R@{k}"] = sum(
            recall_at_k(ranked.get(r, []), g, k) for r, g in graded.items()
        ) / len(graded)
    return metrics

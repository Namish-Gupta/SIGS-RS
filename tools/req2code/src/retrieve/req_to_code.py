"""Forward direction: requirement -> ranked code nodes.

The primary retrieval path, and the one the demo table leads with.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from src.contracts import CodeNode, Requirement, ResultRow
from src.index.vector_store import _top_k


def rank_from_scores(
    score_matrix: np.ndarray,
    requirements: Sequence[Requirement],
    artifact_ids: Sequence[str],
    run_id: str,
    top_k: int = 10,
) -> list[ResultRow]:
    """Turn a ``(n_requirements, n_artifacts)`` score matrix into ranked rows.

    Shared by every configuration in the ablation -- TF-IDF and embedding runs
    differ only in how the matrix is produced, so ranking lives here once. That
    is what guarantees the baseline and our method are ranked and truncated
    identically, which is a fairness requirement, not a convenience.
    """
    rows: list[ResultRow] = []
    for i, req in enumerate(requirements):
        for rank, (j, score) in enumerate(_top_k(score_matrix[i], top_k), start=1):
            rows.append(
                ResultRow(
                    run_id=run_id,
                    req_id=req.req_id,
                    artifact_id=artifact_ids[j],
                    score=score,
                    rank=rank,
                )
            )
    return rows


def trace_requirements_to_code(
    requirements: Sequence[Requirement],
    nodes: Sequence[CodeNode],
    req_vectors: np.ndarray,
    node_vectors: np.ndarray,
    run_id: str,
    top_k: int = 10,
    alpha: float = 1.0,
    beta: float = 0.0,
    req_token_sets: list[set[str]] | None = None,
    node_token_sets: list[set[str]] | None = None,
) -> list[ResultRow]:
    """Rank code nodes against each requirement.

    Args:
        requirements: Queries.
        nodes: The corpus being searched.
        req_vectors: Pre-embedded requirements, row-aligned with `requirements`.
        node_vectors: Pre-embedded node documents, row-aligned with `nodes`.
        run_id: Ablation configuration id, written into every row ("E1", ...).
        top_k: Candidates to keep per requirement.
        alpha: Weight on the semantic signal.
        beta: Weight on the lexical signal. 0 disables it (and skips the work).
        req_token_sets / node_token_sets: Required only when beta > 0.

    Returns:
        `ResultRow`s with `artifact_id` set to `CodeNode.node_id`.

    Vectors are passed in rather than computed here so that a six-run ablation
    embeds the corpus once instead of six times.
    """
    from src.index.vector_store import similarity_matrix

    scores = similarity_matrix(req_vectors, node_vectors) * alpha

    if beta > 0:
        if req_token_sets is None or node_token_sets is None:
            raise ValueError("beta > 0 requires req_token_sets and node_token_sets")
        from src.retrieve.scorer import jaccard_matrix

        scores = scores + beta * jaccard_matrix(req_token_sets, node_token_sets)

    return rank_from_scores(
        scores, requirements, [n.node_id for n in nodes], run_id, top_k
    )

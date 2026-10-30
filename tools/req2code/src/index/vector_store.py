"""Similarity search over the node embedding matrix.

Why plain numpy and not FAISS:

FAISS is an approximate-nearest-neighbour library. Approximation is a tradeoff
you make when exact search is too slow -- which happens somewhere north of a
million vectors. eTour yields ~1200 nodes. A ``(1200, 384) @ (384, 58)`` matmul
is well under a millisecond, it is *exact*, and it removes a dependency that is a
genuine install hazard on Windows.

Using FAISS here would be slower to set up, slower to explain, and would give
worse answers. Revisit only if node count exceeds ~50k.
"""

from __future__ import annotations

import numpy as np


def search(
    query_vec: np.ndarray, matrix: np.ndarray, top_k: int = 10
) -> list[tuple[int, float]]:
    """Return the `top_k` most similar rows of `matrix` to `query_vec`.

    Args:
        query_vec: Shape ``(dim,)``, L2-normalised.
        matrix: Shape ``(n, dim)``, rows L2-normalised.
        top_k: How many results to return.

    Returns:
        ``(row_index, score)`` pairs, best first. Scores are cosine similarities
        in [-1, 1] -- in practice [0, 1] for this kind of text.
    """
    if matrix.shape[0] == 0:
        return []
    scores = matrix @ query_vec
    return _top_k(scores, top_k)


def search_batch(
    query_matrix: np.ndarray, matrix: np.ndarray, top_k: int = 10
) -> list[list[tuple[int, float]]]:
    """Search many queries at once -- all 58 requirements in a single matmul.

    Batching matters more than any micro-optimisation inside `search`: one
    ``(58, 384) @ (384, 1200)`` product is dramatically faster than 58 separate
    ones, because it saturates BLAS instead of paying Python loop overhead.
    """
    if matrix.shape[0] == 0 or query_matrix.shape[0] == 0:
        return [[] for _ in range(query_matrix.shape[0])]
    all_scores = query_matrix @ matrix.T  # (n_queries, n_docs)
    return [_top_k(row, top_k) for row in all_scores]


def similarity_matrix(query_matrix: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    """Full ``(n_queries, n_docs)`` cosine matrix.

    Used when the caller needs every score rather than a top-k -- notably the
    reverse direction (code->requirement) and orphan detection, which both need
    each node's *best* score against all requirements, and the hybrid scorer,
    which re-ranks with a lexical term before truncating.
    """
    if matrix.shape[0] == 0 or query_matrix.shape[0] == 0:
        return np.zeros((query_matrix.shape[0], matrix.shape[0]), dtype=np.float32)
    return query_matrix @ matrix.T


def _top_k(scores: np.ndarray, top_k: int) -> list[tuple[int, float]]:
    """Top-k by score, descending.

    `argpartition` is O(n) versus O(n log n) for a full sort. At this scale the
    difference is unmeasurable, but it costs one line and is the correct habit.
    """
    k = min(top_k, scores.shape[0])
    if k <= 0:
        return []
    idx = np.argpartition(-scores, k - 1)[:k]
    idx = idx[np.argsort(-scores[idx])]
    return [(int(i), float(scores[i])) for i in idx]

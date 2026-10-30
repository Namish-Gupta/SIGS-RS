"""Orphan detection: code that no requirement claims.

The other half of novelty claim #2, and the part with no equivalent in standard
traceability toolkits. An orphan is a code node whose best-matching requirement
still scores below a threshold -- nothing in the specification appears to ask
for it.

Orphans are usually one of:
    - dead code
    - an undocumented feature (a real finding -- scope crept and nobody wrote it
      down)
    - infrastructure that legitimately implements no requirement (getters,
      logging, framework glue)

That third category is the honest caveat: most orphans in any real codebase are
boring plumbing. Say so. The claim worth making is "this surfaces a small
reviewable set of unclaimed behaviour", not "everything flagged is a defect".
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from src.contracts import CodeNode

#: Default cutoff. NOT a tuned result -- see `find_orphans`.
DEFAULT_THRESHOLD = 0.30


def best_requirement_score(
    node_vectors: np.ndarray, req_vectors: np.ndarray
) -> np.ndarray:
    """For each node, its single best cosine against any requirement."""
    from src.index.vector_store import similarity_matrix

    scores = similarity_matrix(node_vectors, req_vectors)
    if scores.shape[1] == 0:
        return np.zeros(scores.shape[0], dtype=np.float32)
    return scores.max(axis=1)


def find_orphans(
    nodes: Sequence[CodeNode],
    node_vectors: np.ndarray,
    req_vectors: np.ndarray,
    threshold: float = DEFAULT_THRESHOLD,
    kinds: tuple[str, ...] = ("method",),
) -> list[tuple[CodeNode, float]]:
    """Flag nodes whose best requirement match falls below `threshold`.

    Args:
        nodes: The full node corpus.
        node_vectors: Row-aligned with `nodes`.
        req_vectors: All requirement embeddings.
        threshold: Cosine below which a node counts as unclaimed.
        kinds: Which node kinds to consider. Defaults to methods only --
            a class-level node aggregates all its methods' vocabulary and so
            almost never scores low, which would make the orphan list dull.

    Returns:
        ``(node, best_score)`` pairs, lowest score first -- most orphaned first.

    On the threshold: 0.30 is a starting guess, not a result. Cosine similarities
    from MiniLM cluster in a narrow band, so the honest presentation is the
    *ranked list* -- which is what this returns -- with the threshold used only
    to decide how much of it to show. Report which value was used.
    """
    best = best_requirement_score(node_vectors, req_vectors)
    out = [
        (node, float(best[i]))
        for i, node in enumerate(nodes)
        if node.kind in kinds and best[i] < threshold
    ]
    return sorted(out, key=lambda pair: pair[1])


def score_distribution(scores: np.ndarray) -> dict[str, float]:
    """Percentiles of the best-match distribution, for choosing a threshold honestly.

    Printing this alongside the orphan list is what turns "we picked 0.3" into
    "0.3 is the 5th percentile of the observed distribution".
    """
    if scores.size == 0:
        return {}
    return {
        "min": float(scores.min()),
        "p5": float(np.percentile(scores, 5)),
        "p25": float(np.percentile(scores, 25)),
        "median": float(np.median(scores)),
        "p75": float(np.percentile(scores, 75)),
        "max": float(scores.max()),
    }

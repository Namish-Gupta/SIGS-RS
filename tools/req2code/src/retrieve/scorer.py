"""The hybrid scoring function.

    score = alpha * cosine(req, node_doc) + beta * jaccard(req_tokens, node_identifiers)

Two signals, deliberately kept separate:

- **cosine** is semantic. It catches "notify the user" ~ `sendAlert`, where the
  words differ but the meaning does not. This is what the baseline cannot do.
- **jaccard** is lexical. It catches exact domain-vocabulary overlap -- when a
  requirement says "RefreshmentPoint" and the method is called
  `insertRefreshmentPoint`, that is a near-certain link, and an embedding model
  may well dilute it among semantically-adjacent neighbours.

Start at alpha=1, beta=0 -- i.e. pure embeddings (run E1). Introducing beta is
run E3, one row of the ablation table.

Discipline on beta: try at most two values. This is a study of whether a lexical
signal helps *at all*, not a hyperparameter search. Sweeping twenty values and
reporting the best one on the same data you evaluated on is overfitting, and any
examiner will spot it. Two values, both reported, whatever the outcome.
"""

from __future__ import annotations

import numpy as np


def jaccard(a: set[str], b: set[str]) -> float:
    """Jaccard similarity: ``|a & b| / |a | b|``. Empty inputs score 0.0."""
    if not a or not b:
        return 0.0
    union = len(a | b)
    return len(a & b) / union if union else 0.0


def hybrid_score(
    cosine_sim: float,
    req_tokens: set[str],
    node_identifiers: set[str],
    alpha: float = 1.0,
    beta: float = 0.0,
) -> float:
    """Blend the semantic and lexical signals into a single score.

    Both terms are already on comparable [0, 1] scales, so no normalisation is
    needed before blending -- which is precisely why Jaccard was chosen over a
    raw overlap count.
    """
    if beta == 0.0:
        return alpha * cosine_sim
    return alpha * cosine_sim + beta * jaccard(req_tokens, node_identifiers)


def jaccard_matrix(
    req_token_sets: list[set[str]], node_token_sets: list[set[str]]
) -> np.ndarray:
    """Pairwise Jaccard for every (requirement, node) pair.

    Computed in one pass rather than inside the ranking loop. With 58x1210 pairs
    a Python double loop is ~70k set operations -- fast enough, and far clearer
    than the sparse-matrix trick that would be needed to vectorise it.
    """
    out = np.zeros((len(req_token_sets), len(node_token_sets)), dtype=np.float32)
    for i, req_tokens in enumerate(req_token_sets):
        if not req_tokens:
            continue
        for j, node_tokens in enumerate(node_token_sets):
            out[i, j] = jaccard(req_tokens, node_tokens)
    return out

"""Reverse direction: code node -> ranked requirements.

Half of novelty claim #2. Standard traceability tools run only forward, which
answers "where is this requirement implemented?" but never "why does this code
exist?" -- the question an engineer inheriting a codebase actually asks.

Mechanically this is cheap: the similarity matrix from the forward direction,
transposed. That is worth stating rather than hiding -- with a symmetric
similarity measure, bidirectionality is nearly free, and the contribution is in
*asking* the reverse question and acting on the answer (see orphans.py), not in
the arithmetic.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from src.contracts import CodeNode, Requirement, ResultRow
from src.index.vector_store import _top_k


def trace_code_to_requirements(
    nodes: Sequence[CodeNode],
    requirements: Sequence[Requirement],
    node_vectors: np.ndarray,
    req_vectors: np.ndarray,
    run_id: str,
    top_k: int = 5,
) -> list[ResultRow]:
    """Rank requirements against each code node.

    Returns:
        `ResultRow`s where `req_id` holds the *node* id and `artifact_id` holds
        the requirement id.

    Note the field-role inversion above. It is slightly awkward, and the
    alternative -- a second CSV schema -- is worse: it would double the parsing
    surface in eval/ for no gain. Reverse results are written to their own file
    so the inversion is never ambiguous in practice.
    """
    from src.index.vector_store import similarity_matrix

    scores = similarity_matrix(node_vectors, req_vectors)  # (n_nodes, n_reqs)

    rows: list[ResultRow] = []
    for i, node in enumerate(nodes):
        for rank, (j, score) in enumerate(_top_k(scores[i], top_k), start=1):
            rows.append(
                ResultRow(
                    run_id=run_id,
                    req_id=node.node_id,  # inverted: see docstring
                    artifact_id=requirements[j].req_id,
                    score=score,
                    rank=rank,
                )
            )
    return rows

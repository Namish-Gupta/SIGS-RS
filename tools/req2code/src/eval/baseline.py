"""TF-IDF + cosine baseline -- the classic VSM approach to traceability recovery.

This is what the literature has done since roughly 2003, and it is the number
our method has to beat. It must be implemented fairly and without handicap: a
strawman baseline invalidates the entire comparison, and a weak baseline is the
first thing a reviewer probes.

Fairness checklist, all honoured here:
    - Same requirement text as the embedding runs (no extra cleaning on either
      side)
    - Same corpus of artifacts
    - Same top_k
    - Same ranking code path (`rank_from_scores`) and the same evaluation code

The baseline runs at two granularities so the ablation can separate two effects
that are otherwise confounded:
    B0: TF-IDF over whole files  -- the classic setup
    B1: TF-IDF over AST nodes    -- isolates whether node granularity alone helps

B1 is the row that makes the work defensible. Without it, a reviewer can say
"your gains just come from chopping files into smaller pieces" and there is no
answer. With it, there is a number.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer

from src.contracts import CodeNode, Requirement, ResultRow


def tfidf_score_matrix(
    requirements: Sequence[Requirement], documents: Sequence[tuple[str, str]]
) -> np.ndarray:
    """Cosine similarity between every requirement and every document, via TF-IDF.

    Fits the vectoriser on the *documents*, then transforms the queries. Fitting
    on both would leak corpus statistics into the query representation.

    sklearn's default preprocessing is kept deliberately. Bolting our
    identifier-splitting onto the baseline would erase the very gap the ablation
    is meant to measure -- that trick is our contribution, not shared
    infrastructure. (Measuring what splitting is worth to TF-IDF specifically is
    a legitimate *extra* row, not a change to B0/B1.)

    TfidfVectorizer L2-normalises its output by default, so the dot product here
    is already cosine.
    """
    texts = [text for _, text in documents]
    vectorizer = TfidfVectorizer(
        lowercase=True,
        stop_words="english",
        sublinear_tf=True,  # damp term frequency; standard for retrieval
        min_df=1,
    )
    doc_matrix = vectorizer.fit_transform(texts)
    query_matrix = vectorizer.transform([r.text for r in requirements])
    return (query_matrix @ doc_matrix.T).toarray().astype(np.float32)


def tfidf_baseline(
    requirements: Sequence[Requirement],
    documents: Sequence[tuple[str, str]],
    run_id: str,
    top_k: int = 10,
) -> list[ResultRow]:
    """Rank documents against requirements by TF-IDF cosine similarity.

    Args:
        requirements: Queries.
        documents: ``(artifact_id, text)`` pairs. Passing pre-built pairs rather
            than `CodeNode`s is what lets this one function serve both B0 (ids
            are file paths, text is whole-file source) and B1 (ids are node ids,
            text is node text).
        run_id: "B0" or "B1".
        top_k: Candidates per requirement.
    """
    from src.retrieve.req_to_code import rank_from_scores

    scores = tfidf_score_matrix(requirements, documents)
    return rank_from_scores(
        scores, requirements, [a for a, _ in documents], run_id, top_k
    )


def file_level_documents(nodes: Sequence[CodeNode]) -> list[tuple[str, str]]:
    """Reassemble whole-file documents from parsed nodes, for B0 and E0.

    Concatenating node text is preferable to re-reading the file from disk: it
    guarantees the file-level and node-level runs see exactly the same
    characters, so the only variable between them is granularity.
    """
    by_file: dict[str, list[str]] = {}
    for node in nodes:
        by_file.setdefault(node.file_path, []).append(node.text)
    return [(path, "\n".join(chunks)) for path, chunks in sorted(by_file.items())]


def node_level_documents(nodes: Sequence[CodeNode]) -> list[tuple[str, str]]:
    """``(node_id, raw node text)`` pairs, for B1."""
    return [(node.node_id, node.text) for node in nodes]

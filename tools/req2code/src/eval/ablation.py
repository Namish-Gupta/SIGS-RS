"""The ablation runner. This table is the project.

Each row isolates exactly one variable from the row above it, so that any gain
can be attributed to a specific cause rather than to "our system". Reviewers do
not accept "our pipeline scores 0.62"; they accept "node granularity contributed
+0.04, semantics contributed +0.11, and here is the run that shows it".

    Run  Representation           Granularity  Isolates
    B0   TF-IDF                   file         classic baseline
    B1   TF-IDF                   AST node     does node granularity alone help?
    E0   embeddings               file         does semantics alone help?
    E1   embeddings               AST node     our core method
    E2   E1 + query expansion     AST node     does requirement rewriting help?
    E3   E2 + identifier overlap  AST node     does lexical signal add on top?

B0 vs E1 is the headline claim. B1 and E0 are what make it survive scrutiny --
they are the two obvious "isn't it just...?" objections, answered pre-emptively.

All six are evaluated at FILE level after max-aggregation (see metrics.py), so
every number in the table is comparable to every other.
"""

from __future__ import annotations

import json
import platform
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from src.contracts import ResultRow, write_results
from src.pipeline import Corpus

#: Metrics reported, in table column order.
METRIC_NAMES = ("MAP", "P@5", "R@5", "P@10", "R@10")

#: How many nodes a node-level run fetches per requirement, as a multiple of the
#: evaluated top_k.
#:
#: This exists to make the node-vs-file comparison fair. Node scores are
#: max-aggregated up to files before scoring, and multiple nodes routinely
#: collapse into one file -- measured on eTour, the top 10 nodes yield only ~5.8
#: distinct files. Fetching a deeper pool and truncating to k FILES afterwards
#: (see metrics.truncate_to_k) means every run is judged on exactly k file
#: candidates. Without this, node-level runs are silently penalised on recall for
#: reasons that have nothing to do with retrieval quality.
#:
#: 20x is chosen to be comfortably deep -- the worst observed case needed ~5
#: nodes per distinct file, so 20x leaves large headroom. It costs nothing: the
#: full similarity matrix is already computed either way.
NODE_FETCH_MULTIPLIER = 20


@dataclass(frozen=True)
class RunConfig:
    """One row of the ablation table.

    Frozen and fully explicit: this object is what gets logged alongside the
    results, and it is what makes "every number traceable to a logged config"
    true rather than aspirational.
    """

    run_id: str
    representation: str  # "tfidf" | "embedding"
    granularity: str  # "file" | "node"
    query_expansion: bool = False
    beta: float = 0.0  # identifier-overlap weight; 0 disables
    isolates: str = ""


ABLATION_RUNS: tuple[RunConfig, ...] = (
    RunConfig("B0", "tfidf", "file", isolates="classic baseline"),
    RunConfig("B1", "tfidf", "node", isolates="does node granularity alone help?"),
    RunConfig("E0", "embedding", "file", isolates="does semantics alone help?"),
    RunConfig("E1", "embedding", "node", isolates="our core method"),
    RunConfig(
        "E2",
        "embedding",
        "node",
        query_expansion=True,
        isolates="does requirement rewriting help?",
    ),
    RunConfig(
        "E3",
        "embedding",
        "node",
        query_expansion=True,
        beta=0.3,
        isolates="does lexical signal add on top?",
    ),
)


def run_one(config: RunConfig, corpus: Corpus, top_k: int = 10) -> list[ResultRow]:
    """Execute a single configuration and return its NODE- or FILE-level rows.

    Rows come back at whatever granularity the config specifies; aggregation to
    file level and truncation to `top_k` files happens in `run_ablation`,
    uniformly for all runs.

    Node-level configs deliberately fetch `top_k * NODE_FETCH_MULTIPLIER` nodes so
    that after aggregation they can still offer `top_k` distinct files. See
    NODE_FETCH_MULTIPLIER.
    """
    fetch_k = top_k * NODE_FETCH_MULTIPLIER if config.granularity == "node" else top_k
    from src.eval.baseline import (
        file_level_documents,
        node_level_documents,
        tfidf_baseline,
    )
    from src.index.embedder import embed_texts
    from src.index.node_doc import tokenize_requirement
    from src.retrieve.query_expansion import expand_all
    from src.retrieve.req_to_code import rank_from_scores, trace_requirements_to_code

    if config.representation == "tfidf":
        documents = (
            file_level_documents(corpus.nodes)
            if config.granularity == "file"
            else node_level_documents(corpus.nodes)
        )
        return tfidf_baseline(corpus.requirements, documents, config.run_id, fetch_k)

    # --- embedding runs ---------------------------------------------------
    if config.query_expansion:
        expanded = expand_all(corpus.requirements)
        req_vectors = embed_texts(expanded, cache_name="reqs-expanded")
        req_texts = expanded
    else:
        req_vectors = corpus.req_vectors
        req_texts = [r.text for r in corpus.requirements]

    if config.granularity == "file":
        # Aggregate node documents into one document per file, then embed those.
        # Embedding the concatenation (rather than max-pooling node vectors) is
        # the honest file-level analogue: it is what you would do if you had
        # never thought of node granularity, which is exactly what E0 tests.
        by_file: dict[str, list[str]] = {}
        for node, document in zip(corpus.nodes, corpus.node_documents, strict=True):
            by_file.setdefault(node.file_path, []).append(document)
        file_ids = sorted(by_file)
        file_docs = [" ".join(by_file[f]) for f in file_ids]
        file_vectors = embed_texts(file_docs, cache_name="files")
        scores = req_vectors @ file_vectors.T
        return rank_from_scores(
            np.asarray(scores), corpus.requirements, file_ids, config.run_id, fetch_k
        )

    req_token_sets = node_token_sets = None
    if config.beta > 0:
        req_token_sets = [set(tokenize_requirement(t)) for t in req_texts]
        node_token_sets = [set(d.split()) for d in corpus.node_documents]

    return trace_requirements_to_code(
        corpus.requirements,
        corpus.nodes,
        req_vectors,
        corpus.node_vectors,
        run_id=config.run_id,
        top_k=fetch_k,
        beta=config.beta,
        req_token_sets=req_token_sets,
        node_token_sets=node_token_sets,
    )


def run_ablation(
    corpus: Corpus,
    results_dir: str | Path = "results",
    top_k: int = 10,
    configs: tuple[RunConfig, ...] = ABLATION_RUNS,
    verbose: bool = True,
) -> dict[str, dict[str, float]]:
    """Execute every configuration and return ``run_id -> {metric: value}``.

    Writes each run's raw rows to ``results/{run_id}.csv`` *before* computing
    metrics, so a buggy metric can be recomputed from the CSVs without re-running
    the pipeline.
    """
    from src.eval.metrics import aggregate_to_file_level, evaluate, truncate_to_k

    results_dir = Path(results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)
    node_to_file = corpus.node_to_file

    table: dict[str, dict[str, float]] = {}
    for config in configs:
        rows = run_one(config, corpus, top_k=top_k)
        write_results(rows, results_dir / f"{config.run_id}.csv")

        # Uniform aggregation: file-level runs pass through unchanged (their
        # artifact_ids are already file paths and are absent from node_to_file),
        # node-level runs collapse by max. One code path, no special-casing.
        # Then truncate so every run is judged on exactly top_k FILE candidates.
        file_rows = truncate_to_k(aggregate_to_file_level(rows, node_to_file), top_k)
        table[config.run_id] = evaluate(file_rows, corpus.gold)
        if verbose:
            metrics = " ".join(
                f"{k}={table[config.run_id][k]:.3f}" for k in METRIC_NAMES
            )
            print(f"  {config.run_id}  {metrics}")

    _write_summary(table, configs, results_dir, corpus, top_k)
    return table


def _write_summary(
    table: dict[str, dict[str, float]],
    configs: tuple[RunConfig, ...],
    results_dir: Path,
    corpus: Corpus,
    top_k: int,
) -> None:
    """Write ablation.md and config.json.

    config.json is what makes "every number traceable to a committed script and a
    logged config" real. Written every run, without exception -- the run you
    forget to log is the one you will need to reproduce.
    """
    from src.index.embedder import MODEL_NAME

    lines = [
        "# Ablation results",
        "",
        f"Corpus: {len(corpus.requirements)} requirements, {len(corpus.nodes)} AST "
        f"nodes across {len({n.file_path for n in corpus.nodes})} files, "
        f"{sum(len(v) for v in corpus.gold.values())} gold links.",
        "",
        "All runs evaluated at **file level** after max-aggregation of node scores,",
        "because eTour gold links are requirement-to-file while we retrieve methods.",
        "",
        "| Run | Representation | Granularity | Isolates | "
        + " | ".join(METRIC_NAMES)
        + " |",
        "|---|---|---|---|" + "---|" * len(METRIC_NAMES),
    ]
    for config in configs:
        metrics = table.get(config.run_id, {})
        cells = " | ".join(f"{metrics.get(m, float('nan')):.3f}" for m in METRIC_NAMES)
        lines.append(
            f"| {config.run_id} | {config.representation} | {config.granularity} "
            f"| {config.isolates} | {cells} |"
        )

    best = max(table, key=lambda r: table[r]["MAP"]) if table else "n/a"
    lines += [
        "",
        f"Best MAP: **{best}**.",
        "",
        f"Generated by `scripts/run_ablation.py` with top_k={top_k}, "
        f"model `{MODEL_NAME}`.",
        "",
    ]
    (results_dir / "ablation.md").write_text("\n".join(lines), encoding="utf-8")

    config_log = {
        "python": sys.version,
        "platform": platform.platform(),
        "model": MODEL_NAME,
        "top_k": top_k,
        "numpy": np.__version__,
        "corpus": {
            "requirements": len(corpus.requirements),
            "nodes": len(corpus.nodes),
            "files": len({n.file_path for n in corpus.nodes}),
            "gold_links": sum(len(v) for v in corpus.gold.values()),
            "nodes_with_javadoc": len(corpus.docs),
        },
        "runs": [asdict(c) for c in configs],
        "results": table,
        "timings_seconds": corpus.timings,
    }
    (results_dir / "config.json").write_text(
        json.dumps(config_log, indent=2), encoding="utf-8"
    )

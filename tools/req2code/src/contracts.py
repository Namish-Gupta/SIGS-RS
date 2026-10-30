"""Frozen data contracts shared across the whole pipeline.

This module is the interface boundary between the two halves of the project:
ingest/ + eval/ + baseline on one side, parse/ + index/ + retrieve/ on the
other. Both sides import from here and neither owns it.

Nothing in this file may change without agreement from both authors. Everything
else in src/ is free to churn; these three shapes are not. That is the whole
point -- freezing them is what lets the two halves be built in parallel without
blocking on each other.

The dataclasses are `frozen=True` so an object cannot be mutated after it is
constructed. That is deliberate: a Requirement or CodeNode is a fact extracted
from the corpus, not a scratch variable. Freezing also makes them hashable, so
they can go in sets and dict keys (useful for de-duplication during parsing).
"""

from __future__ import annotations

import csv
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

# --------------------------------------------------------------------------
# Contract 1: the two artifact types
# --------------------------------------------------------------------------

#: Kinds of AST node we trace to. Deliberately small -- the novelty claim is
#: "method-level, not file-level", so `method` is the one that matters and the
#: others exist to give it enclosing context.
NodeKind = Literal["method", "constructor", "class", "interface"]


@dataclass(frozen=True)
class Requirement:
    """A single requirement, as written by a human.

    In eTour these are use-case documents: one file per requirement, English
    prose. `req_id` is the stable identifier used in the gold-link file, so it
    must match that file exactly (do not strip extensions or re-case it) or
    evaluation will silently score zero.
    """

    req_id: str
    text: str
    source_path: str


@dataclass(frozen=True)
class CodeNode:
    """One AST node -- the unit we actually retrieve.

    This is the core of novelty claim #1. A traditional traceability tool would
    represent a whole `.java` file here; we represent each method separately so
    a trace can point at `TourGuide.findNearbyAttractions()` rather than at
    `TourGuide.java`.

    Attributes:
        node_id: Stable unique id. Convention: ``{file_path}::{name}#{start_line}``.
            Line number is included because Java permits overloads -- two methods
            can share a name and signature-less identity within one file.
        file_path: Path relative to the repo root. Required because gold links
            are file-level, so evaluation aggregates nodes back up by this key.
        kind: See `NodeKind`.
        name: The raw identifier as written in source (``findNearbyAttractions``).
            Not split here -- splitting happens in index/node_doc.py, because the
            unsplit name is also what we show the user in the demo table.
        signature: Return type + parameter list, as source text.
        start_line: 1-based, inclusive.
        end_line: 1-based, inclusive.
        text: The raw source slice for this node. Kept for display and for the
            LLM justification prompt. NOTE: this is *not* what gets embedded --
            see index/node_doc.py for why raw source is a poor embedding input.
    """

    node_id: str
    file_path: str
    kind: NodeKind
    name: str
    signature: str
    start_line: int
    end_line: int
    text: str


# --------------------------------------------------------------------------
# Contract 2: the results CSV schema
# --------------------------------------------------------------------------

#: Column order for every results file this project writes. Every run of every
#: ablation configuration produces rows in exactly this shape, which is what
#: makes the ablation table a single groupby rather than six bespoke parsers.
RESULTS_COLUMNS: tuple[str, ...] = ("run_id", "req_id", "artifact_id", "score", "rank")


@dataclass(frozen=True)
class ResultRow:
    """One retrieved candidate for one query.

    Attributes:
        run_id: Which ablation configuration produced this row -- "B0", "E1", etc.
            Matches the run ids in the ablation table in the README.
        req_id: The querying requirement's `Requirement.req_id`.
        artifact_id: What was retrieved. This is intentionally loose: for
            file-level runs (B0, E0) it is a `CodeNode.file_path`; for node-level
            runs (B1, E1-E3) it is a `CodeNode.node_id`. The `run_id` tells you
            which to expect, so one schema covers both granularities.
        score: Similarity score. Comparable within a run_id, NOT across run_ids
            (TF-IDF cosine and embedding cosine are not on the same scale).
        rank: 1-based position within this (run_id, req_id) group. Rank 1 is the
            best candidate.
    """

    run_id: str
    req_id: str
    artifact_id: str
    score: float
    rank: int


def write_results(rows: Iterable[ResultRow], path: str | Path) -> int:
    """Write result rows to CSV in the frozen column order.

    Implemented (not stubbed) because it *is* the contract -- if both halves of
    the project wrote their own CSV writer they would drift, which is exactly
    what freezing the schema is meant to prevent.

    Returns the number of rows written, so callers can log/assert on it.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(RESULTS_COLUMNS)
        for row in rows:
            writer.writerow(
                [row.run_id, row.req_id, row.artifact_id, f"{row.score:.6f}", row.rank]
            )
            count += 1
    return count

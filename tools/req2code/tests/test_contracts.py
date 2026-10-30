"""Smoke tests for the frozen contracts.

Deliberately narrow: these test the interface both halves of the project agree
on, not any pipeline logic (there is none yet). Their job is to fail loudly if
someone edits contracts.py without realising the other half depends on it.
"""

from __future__ import annotations

import csv

from src.contracts import (
    RESULTS_COLUMNS,
    CodeNode,
    Requirement,
    ResultRow,
    write_results,
)


def test_requirement_is_constructible_and_frozen():
    req = Requirement(
        req_id="UC1",
        text="The system shall notify the user.",
        source_path="data/etour/UC1.txt",
    )
    assert req.req_id == "UC1"

    # Frozen: a Requirement is an extracted fact, not a scratch variable.
    try:
        req.req_id = "UC2"  # type: ignore[misc]
    except Exception:
        pass
    else:
        raise AssertionError("Requirement should be immutable")


def test_code_node_is_constructible():
    node = CodeNode(
        node_id="src/TourGuide.java::findNearby#42",
        file_path="src/TourGuide.java",
        kind="method",
        name="findNearby",
        signature="List<Site> findNearby(double lat, double lon)",
        start_line=42,
        end_line=57,
        text="List<Site> findNearby(double lat, double lon) { ... }",
    )
    assert node.kind == "method"
    assert node.end_line >= node.start_line


def test_results_csv_matches_frozen_schema(tmp_path):
    """The column order is the contract.

    If this test needs changing, both authors agree first.
    """
    rows = [
        ResultRow(
            run_id="E1",
            req_id="UC1",
            artifact_id="src/A.java::foo#10",
            score=0.8123,
            rank=1,
        ),
        ResultRow(
            run_id="E1",
            req_id="UC1",
            artifact_id="src/B.java::bar#20",
            score=0.7011,
            rank=2,
        ),
    ]
    out = tmp_path / "results" / "E1.csv"
    written = write_results(rows, out)

    assert written == 2
    with out.open(encoding="utf-8") as fh:
        parsed = list(csv.reader(fh))

    assert tuple(parsed[0]) == RESULTS_COLUMNS
    assert parsed[1] == ["E1", "UC1", "src/A.java::foo#10", "0.812300", "1"]
    assert len(parsed) == 3  # header + 2 rows

"""Unit tests for the retrieve module.

Covers:
  - scorer.jaccard
  - scorer.hybrid_score
  - scorer.jaccard_matrix
  - orphans.score_distribution
  - orphans.find_orphans

These modules had no dedicated tests. The tests here are purely in-process
(no corpus, no embeddings model, no disk I/O) so they run instantly offline.
"""

from __future__ import annotations

import numpy as np
import pytest

from src.contracts import CodeNode
from src.retrieve.orphans import find_orphans, score_distribution
from src.retrieve.scorer import hybrid_score, jaccard, jaccard_matrix

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_node(name: str, kind: str = "method", start_line: int = 1) -> CodeNode:
    return CodeNode(
        node_id=f"src/Fake.java::{name}#{start_line}",
        file_path="src/Fake.java",
        kind=kind,
        name=name,
        signature=f"void {name}()",
        start_line=start_line,
        end_line=start_line + 5,
        text=f"void {name}() {{ }}",
    )


# ---------------------------------------------------------------------------
# scorer.jaccard
# ---------------------------------------------------------------------------


class TestJaccard:
    def test_identical_sets(self):
        assert jaccard({"a", "b", "c"}, {"a", "b", "c"}) == pytest.approx(1.0)

    def test_disjoint_sets(self):
        assert jaccard({"a", "b"}, {"c", "d"}) == pytest.approx(0.0)

    def test_partial_overlap(self):
        # |intersection| = 1, |union| = 3  ->  1/3
        result = jaccard({"a", "b"}, {"b", "c"})
        assert result == pytest.approx(1 / 3)

    def test_empty_first(self):
        assert jaccard(set(), {"a", "b"}) == 0.0

    def test_empty_second(self):
        assert jaccard({"a"}, set()) == 0.0

    def test_both_empty(self):
        assert jaccard(set(), set()) == 0.0

    def test_single_element_match(self):
        assert jaccard({"x"}, {"x"}) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# scorer.hybrid_score
# ---------------------------------------------------------------------------


class TestHybridScore:
    def test_pure_semantic_default(self):
        # beta defaults to 0 -> result is just alpha * cosine
        score = hybrid_score(cosine_sim=0.8, req_tokens={"a"}, node_identifiers={"b"})
        assert score == pytest.approx(0.8)

    def test_alpha_scaling(self):
        score = hybrid_score(
            cosine_sim=0.5, req_tokens=set(), node_identifiers=set(), alpha=2.0
        )
        assert score == pytest.approx(1.0)

    def test_lexical_signal_added(self):
        # cosine=0.6, jaccard=1.0 (identical sets), alpha=1, beta=0.2
        # expected = 1.0*0.6 + 0.2*1.0 = 0.8
        score = hybrid_score(
            cosine_sim=0.6,
            req_tokens={"notify", "user"},
            node_identifiers={"notify", "user"},
            alpha=1.0,
            beta=0.2,
        )
        assert score == pytest.approx(0.8)

    def test_beta_zero_skips_jaccard(self):
        # Even with matching tokens, beta=0 means jaccard is ignored
        score = hybrid_score(
            cosine_sim=0.4,
            req_tokens={"x"},
            node_identifiers={"x"},
            alpha=1.0,
            beta=0.0,
        )
        assert score == pytest.approx(0.4)

    def test_disjoint_tokens_with_beta(self):
        # jaccard=0 when tokens are disjoint -> beta term contributes nothing
        score = hybrid_score(
            cosine_sim=0.7,
            req_tokens={"req_token"},
            node_identifiers={"node_token"},
            alpha=1.0,
            beta=0.5,
        )
        assert score == pytest.approx(0.7)


# ---------------------------------------------------------------------------
# scorer.jaccard_matrix
# ---------------------------------------------------------------------------


class TestJaccardMatrix:
    def test_shape(self):
        req_sets = [{"a", "b"}, {"c"}]
        node_sets = [{"a"}, {"b", "c"}, {"d"}]
        mat = jaccard_matrix(req_sets, node_sets)
        assert mat.shape == (2, 3)

    def test_known_values(self):
        # req[0] vs node[0]: {"a","b"} & {"a"} / {"a","b"} | {"a"} = 1/2
        # req[0] vs node[1]: {"a","b"} & {"c"} / {"a","b","c"} = 0/3 = 0
        req_sets = [{"a", "b"}]
        node_sets = [{"a"}, {"c"}]
        mat = jaccard_matrix(req_sets, node_sets)
        assert mat[0, 0] == pytest.approx(0.5)
        assert mat[0, 1] == pytest.approx(0.0)

    def test_empty_req_row_is_zeros(self):
        req_sets = [set(), {"a"}]
        node_sets = [{"a"}, {"b"}]
        mat = jaccard_matrix(req_sets, node_sets)
        assert (mat[0] == 0).all()

    def test_dtype_is_float32(self):
        mat = jaccard_matrix([{"a"}], [{"a"}])
        assert mat.dtype == np.float32


# ---------------------------------------------------------------------------
# orphans.score_distribution
# ---------------------------------------------------------------------------


class TestScoreDistribution:
    def test_empty_array_returns_empty_dict(self):
        assert score_distribution(np.array([], dtype=np.float32)) == {}

    def test_keys_present(self):
        dist = score_distribution(np.array([0.1, 0.3, 0.5, 0.7, 0.9], dtype=np.float32))
        assert set(dist.keys()) == {"min", "p5", "p25", "median", "p75", "max"}

    def test_min_max_correct(self):
        arr = np.array([0.2, 0.8, 0.5], dtype=np.float32)
        dist = score_distribution(arr)
        assert dist["min"] == pytest.approx(0.2, abs=1e-5)
        assert dist["max"] == pytest.approx(0.8, abs=1e-5)

    def test_single_element(self):
        dist = score_distribution(np.array([0.42], dtype=np.float32))
        assert dist["min"] == pytest.approx(0.42, abs=1e-5)
        assert dist["max"] == pytest.approx(0.42, abs=1e-5)
        assert dist["median"] == pytest.approx(0.42, abs=1e-5)


# ---------------------------------------------------------------------------
# orphans.find_orphans
# ---------------------------------------------------------------------------


class TestFindOrphans:
    """find_orphans depends on similarity_matrix from vector_store.

    We pass raw numpy arrays — no embedding model needed — so these tests are
    fully offline and deterministic.
    """

    def _unit_vecs(self, n: int, dim: int = 4) -> np.ndarray:
        """n orthogonal-ish unit vectors of dimension dim."""
        rng = np.random.default_rng(42)
        raw = rng.standard_normal((n, dim)).astype(np.float32)
        norms = np.linalg.norm(raw, axis=1, keepdims=True)
        return raw / norms

    def test_all_above_threshold_returns_empty(self):
        # node vector == req vector -> cosine = 1.0 -> never orphaned
        vec = self._unit_vecs(1)
        nodes = [_make_node("doSomething")]
        result = find_orphans(nodes, vec, vec, threshold=0.5)
        assert result == []

    def test_orphan_detected_when_below_threshold(self):
        # Use two vectors that are nearly orthogonal (low cosine)
        node_vecs = np.array([[1.0, 0.0, 0.0, 0.0]], dtype=np.float32)
        req_vecs = np.array([[0.0, 1.0, 0.0, 0.0]], dtype=np.float32)
        # cosine ≈ 0 < 0.30 -> orphan
        nodes = [_make_node("orphanMethod")]
        result = find_orphans(nodes, node_vecs, req_vecs, threshold=0.30)
        assert len(result) == 1
        assert result[0][0].name == "orphanMethod"
        assert result[0][1] == pytest.approx(0.0, abs=1e-5)

    def test_non_method_nodes_excluded_by_default(self):
        # kind="class" should be ignored when kinds=("method",)
        node_vecs = np.array([[1.0, 0.0, 0.0, 0.0]], dtype=np.float32)
        req_vecs = np.array([[0.0, 1.0, 0.0, 0.0]], dtype=np.float32)
        nodes = [_make_node("MyClass", kind="class")]
        result = find_orphans(nodes, node_vecs, req_vecs, threshold=0.30)
        assert result == []

    def test_sorted_lowest_score_first(self):
        # Two nodes: one very orphaned (score~0), one borderline
        node_vecs = np.array(
            [[1.0, 0.0, 0.0, 0.0], [0.0, 0.5, 0.866, 0.0]],
            dtype=np.float32,
        )
        req_vecs = np.array([[0.0, 1.0, 0.0, 0.0]], dtype=np.float32)
        nodes = [_make_node("veryOrphaned"), _make_node("lessOrphaned", start_line=10)]
        result = find_orphans(nodes, node_vecs, req_vecs, threshold=1.0)
        # Both should be below threshold=1.0; lowest score first
        assert len(result) == 2
        assert result[0][1] <= result[1][1]

    def test_custom_kinds_filter(self):
        node_vecs = np.array([[1.0, 0.0, 0.0, 0.0]], dtype=np.float32)
        req_vecs = np.array([[0.0, 1.0, 0.0, 0.0]], dtype=np.float32)
        nodes = [_make_node("MyConstructor", kind="constructor")]
        # Exclude constructors
        result = find_orphans(
            nodes, node_vecs, req_vecs, threshold=1.0, kinds=("method",)
        )
        assert result == []
        # Include constructors
        result = find_orphans(
            nodes, node_vecs, req_vecs, threshold=1.0, kinds=("constructor",)
        )
        assert len(result) == 1

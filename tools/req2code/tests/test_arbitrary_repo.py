"""End-to-end tests on synthetic repositories built at test time.

Everything else in tests/ is a unit test over hand-made objects, and everything
in scripts/ runs against eTour. Neither exercises the case this file covers: a
repository the project has never seen, with **no answer key**, parsed from
source we generate here.

That case is the one that matters for the editor integration. eTour is a fixed
corpus with gold links; a user's project is arbitrary Java with none. The tests
below assert that retrieval, orphan detection, and incremental re-indexing all
work with `Corpus.gold` empty -- because nothing outside eval/ reads gold.

WHY A FAKE EMBEDDER. `load_corpus` needs vectors, and loading the real
sentence-transformer costs ~3.5s and a warm model directory. These tests
monkeypatch a deterministic bag-of-words embedder instead, which keeps the whole
suite instant, offline, and reproducible on a machine that has never downloaded
the model. It is not a stub that returns zeros: shared vocabulary produces real
cosine similarity, so ranking assertions here genuinely exercise the retrieval
path. What it deliberately does NOT test is embedding *quality* -- that is what
the ablation measures, and asserting on model behaviour in a unit test would
just pin today's numbers in place.

Real-model integration lives in the `slow` marker at the bottom, skipped by
default (`pytest -m slow` to run it).
"""

from __future__ import annotations

import hashlib
import os
import re

import numpy as np
import pytest

from src.eval.gold_loader import load_gold_links
from src.eval.metrics import evaluate
from src.index.node_doc import build_node_document, split_identifier
from src.parse.java_parser import enclosing_class_map, parse_repo

DIM = 384


# ---------------------------------------------------------------------------
# Fake embedder: deterministic, offline, and semantically meaningful
# ---------------------------------------------------------------------------


def _fake_embed(texts, model_name=None, cache_name=None, show_progress=False):
    """Hash each word into a fixed bucket and L2-normalise the result.

    Two documents sharing vocabulary get a high cosine; disjoint ones get ~0.
    That is enough to assert "the obviously-correct node ranks first" without
    involving a neural network.
    """
    out = np.zeros((len(texts), DIM), dtype=np.float32)
    for row, text in enumerate(texts):
        for word in re.findall(r"[a-z0-9]+", text.lower()):
            bucket = int(hashlib.md5(word.encode()).hexdigest(), 16) % DIM
            out[row, bucket] += 1.0
        norm = np.linalg.norm(out[row])
        if norm > 0:
            out[row] /= norm
    return out


@pytest.fixture
def fake_embedder(monkeypatch):
    """Patch the embedder module attribute.

    `load_corpus` and `reindex_file` both do `from src.index.embedder import
    embed_texts` *inside* the function body, so the lookup happens at call time
    and patching the module attribute is sufficient.
    """
    monkeypatch.setattr("src.index.embedder.embed_texts", _fake_embed)


# ---------------------------------------------------------------------------
# Synthetic repository construction
# ---------------------------------------------------------------------------

BANNER_JAVA = """
package com.example.ads;

/** Manages advertising banners for refreshment points. */
public class AdvertBoard {

    private int maxBanner;

    public AdvertBoard(int maxBanner) {
        this.maxBanner = maxBanner;
    }

    /**
     * Create and insert a new banner for a refreshment point, checking that
     * the maximum permitted number has not already been reached.
     *
     * @param pointId identifier of the refreshment point
     * @see com.example.ads.SomethingIrrelevant#noise
     */
    @Override
    public boolean insertBanner(int pointId, String imagePath) {
        if (countBanners(pointId) >= maxBanner) {
            return false;
        }
        return true;
    }

    /** Overloaded variant -- same name, different line. */
    public boolean insertBanner(int pointId) {
        return insertBanner(pointId, "default.png");
    }

    public int countBanners(int pointId) {
        return 0;
    }

    public String getFont() {
        return "Arial";
    }
}
"""

ALERT_JAVA = """
package com.example.notify;

public interface AlertDispatcher {

    /** Notify the traveller about attractions located nearby. */
    void sendAlertToNearbyUser(String travellerName, double radiusKm);

    void clearHTTPServerCache();
}
"""

LOGIN_JAVA = """
package com.example.auth;

/** Handles operator authentication against the agency database. */
public class LoginController {
    public boolean authenticateOperator(String username, String password) {
        return username != null && password != null;
    }
}
"""

REQ_BANNER = """Use case name: InsertBanner
Description: Inserting a new banner associated with a refreshment point.
Flow of events: The operator selects a refreshment point and inserts a banner,
and the system checks that the maximum number of banners is not exceeded.
"""

REQ_ALERT = """Use case name: NotifyNearby
Description: The system shall notify the traveller about attractions located
nearby, within a given radius.
"""


def make_repo(
    root, code: dict[str, str], reqs: dict[str, str], gold: str | None = None
):
    """Write a corpus to disk. Returns the root path."""
    (root / "code").mkdir(parents=True, exist_ok=True)
    (root / "req").mkdir(parents=True, exist_ok=True)
    for name, body in code.items():
        path = root / "code" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    for name, body in reqs.items():
        (root / "req" / name).write_text(body, encoding="utf-8")
    if gold is not None:
        (root / "etour_solution_links_english.txt").write_text(gold, encoding="utf-8")
    return root


@pytest.fixture
def repo(tmp_path):
    return make_repo(
        tmp_path / "proj",
        code={
            "AdvertBoard.java": BANNER_JAVA,
            "AlertDispatcher.java": ALERT_JAVA,
            "LoginController.java": LOGIN_JAVA,
        },
        reqs={"UC1.txt": REQ_BANNER, "UC2.txt": REQ_ALERT},
    )


# ---------------------------------------------------------------------------
# Parsing arbitrary Java
# ---------------------------------------------------------------------------


class TestParseArbitraryCode:
    def test_extracts_each_node_kind(self, repo):
        nodes, _ = parse_repo(repo / "code")
        kinds = {n.kind for n in nodes}
        assert {"class", "interface", "method", "constructor"} <= kinds

    def test_finds_expected_methods(self, repo):
        nodes, _ = parse_repo(repo / "code")
        names = {n.name for n in nodes if n.kind == "method"}
        assert {
            "insertBanner",
            "countBanners",
            "getFont",
            "authenticateOperator",
        } <= names

    def test_overloads_get_distinct_ids(self, repo):
        nodes, _ = parse_repo(repo / "code")
        overloads = [n for n in nodes if n.name == "insertBanner"]
        assert len(overloads) == 2
        assert len({n.node_id for n in overloads}) == 2, "line number must disambiguate"

    def test_javadoc_attached_past_annotations(self, repo):
        """The Javadoc on insertBanner sits before an @Override, so the parser
        has to walk back over modifiers/annotations to find it."""
        nodes, docs = parse_repo(repo / "code")
        target = next(
            n for n in nodes if n.name == "insertBanner" and "@Override" not in n.text
        )
        # the annotated overload is the one with the long doc
        annotated = next(
            n for n in nodes if n.name == "insertBanner" and n is not target
        )
        assert any(
            "maximum permitted number" in docs.get(n.node_id, "")
            for n in (target, annotated)
        )

    def test_constructor_is_not_a_method(self, repo):
        nodes, _ = parse_repo(repo / "code")
        ctor = next(
            n for n in nodes if n.name == "AdvertBoard" and n.kind == "constructor"
        )
        assert ctor.kind == "constructor"

    def test_enclosing_class_resolves(self, repo):
        nodes, _ = parse_repo(repo / "code")
        enclosing = enclosing_class_map(nodes)
        method = next(n for n in nodes if n.name == "authenticateOperator")
        assert enclosing[method.node_id] == "LoginController"

    def test_innermost_enclosing_wins_for_nested_class(self, tmp_path):
        src = """
        public class Outer {
            class Inner {
                public void innerMethod() { }
            }
            public void outerMethod() { }
        }
        """
        root = make_repo(tmp_path / "n", {"Outer.java": src}, {})
        nodes, _ = parse_repo(root / "code")
        enclosing = enclosing_class_map(nodes)
        inner = next(n for n in nodes if n.name == "innerMethod")
        outer = next(n for n in nodes if n.name == "outerMethod")
        assert enclosing[inner.node_id] == "Inner"
        assert enclosing[outer.node_id] == "Outer"

    def test_generics_and_annotations_parse(self, tmp_path):
        src = """
        import java.util.*;
        public class Repo<T extends Comparable<T>> {
            @Deprecated
            @SuppressWarnings("unchecked")
            public List<Map<String, T>> findAllMatching(Set<? super T> filter) {
                return new ArrayList<>();
            }
        }
        """
        root = make_repo(tmp_path / "g", {"Repo.java": src}, {})
        nodes, _ = parse_repo(root / "code")
        method = next(n for n in nodes if n.name == "findAllMatching")
        assert "findAllMatching" in method.signature
        assert "filter" in method.signature, "parameter names carry domain vocabulary"

    def test_malformed_source_does_not_raise(self, tmp_path):
        """tree-sitter emits ERROR nodes rather than throwing, so a broken file
        degrades to fewer nodes instead of taking the whole run down."""
        src = "public class Broken { public void oops( { { { "
        root = make_repo(tmp_path / "b", {"Broken.java": src}, {})
        nodes, _ = parse_repo(root / "code")
        assert isinstance(nodes, list)  # no exception is the assertion

    def test_empty_file_yields_no_nodes(self, tmp_path):
        root = make_repo(tmp_path / "e", {"Empty.java": ""}, {})
        nodes, _ = parse_repo(root / "code")
        assert nodes == []

    def test_non_java_files_ignored(self, tmp_path):
        root = make_repo(tmp_path / "x", {"Real.java": LOGIN_JAVA}, {})
        (root / "code" / "README.md").write_text("# not java", encoding="utf-8")
        (root / "code" / "notes.txt").write_text(
            "public class Fake {}", encoding="utf-8"
        )
        nodes, _ = parse_repo(root / "code")
        assert {n.file_path for n in nodes} == {"Real.java"}

    def test_test_directories_skipped(self, tmp_path):
        root = make_repo(tmp_path / "t", {"Main.java": LOGIN_JAVA}, {})
        (root / "code" / "test").mkdir()
        (root / "code" / "test" / "MainTest.java").write_text(
            BANNER_JAVA, encoding="utf-8"
        )
        nodes, _ = parse_repo(root / "code")
        assert all("test" not in n.file_path for n in nodes)

    def test_line_numbers_are_1_based_and_bracketing(self, repo):
        nodes, _ = parse_repo(repo / "code")
        for node in nodes:
            assert node.start_line >= 1
            assert node.end_line >= node.start_line


# ---------------------------------------------------------------------------
# Node documents over arbitrary identifiers
# ---------------------------------------------------------------------------


class TestNodeDocumentsOnArbitraryCode:
    def test_acronym_split_on_real_node(self, repo):
        nodes, docs = parse_repo(repo / "code")
        enclosing = enclosing_class_map(nodes)
        node = next(n for n in nodes if n.name == "clearHTTPServerCache")
        doc = build_node_document(
            node, enclosing.get(node.node_id, ""), docs.get(node.node_id, "")
        )
        assert "http" in doc.split()
        assert "server" in doc.split()
        assert "h t t p" not in doc

    def test_javadoc_prose_included_tags_dropped(self, repo):
        nodes, docs = parse_repo(repo / "code")
        enclosing = enclosing_class_map(nodes)
        node = next(n for n in nodes if n.name == "insertBanner" and n.node_id in docs)
        doc = build_node_document(
            node, enclosing.get(node.node_id, ""), docs[node.node_id]
        )
        assert "maximum" in doc
        # @see line is structural noise -- its package path must not leak in
        assert "somethingirrelevant" not in doc.lower()

    def test_enclosing_class_words_present(self, repo):
        nodes, docs = parse_repo(repo / "code")
        enclosing = enclosing_class_map(nodes)
        node = next(n for n in nodes if n.name == "getFont")
        doc = build_node_document(
            node, enclosing[node.node_id], docs.get(node.node_id, "")
        )
        assert "advert" in doc and "board" in doc

    def test_split_identifier_on_generated_names(self):
        assert split_identifier("sendAlertToNearbyUser") == [
            "send",
            "alert",
            "to",
            "nearby",
            "user",
        ]
        assert split_identifier("clearHTTPServerCache") == [
            "clear",
            "http",
            "server",
            "cache",
        ]


# ---------------------------------------------------------------------------
# Gold links are optional
# ---------------------------------------------------------------------------


class TestGoldIsOptional:
    def test_missing_file_non_strict_returns_empty(self, tmp_path):
        assert load_gold_links(tmp_path / "nope.txt", strict=False) == {}

    def test_missing_file_strict_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_gold_links(tmp_path / "nope.txt", strict=True)

    def test_arbitrary_gold_parses_non_strict(self, tmp_path):
        path = tmp_path / "links.txt"
        path.write_text(
            "UC1.txt: AdvertBoard.java\n"
            "UC1.txt: AlertDispatcher.java\n"
            "UC2.txt: AlertDispatcher.java\n",
            encoding="utf-8",
        )
        gold = load_gold_links(path, strict=False)
        assert gold == {
            "UC1.txt": {"AdvertBoard.java", "AlertDispatcher.java"},
            "UC2.txt": {"AlertDispatcher.java"},
        }

    def test_wrong_counts_rejected_under_strict(self, tmp_path):
        path = tmp_path / "links.txt"
        path.write_text("UC1.txt: A.java\n", encoding="utf-8")
        with pytest.raises(ValueError, match="expected 308"):
            load_gold_links(path, strict=True)

    def test_blank_and_malformed_lines_skipped(self, tmp_path):
        path = tmp_path / "links.txt"
        path.write_text(
            "\nUC1.txt: A.java\ngarbage-no-colon\nUC1.txt:   \n  \n", encoding="utf-8"
        )
        assert load_gold_links(path, strict=False) == {"UC1.txt": {"A.java"}}

    def test_evaluate_with_no_gold_returns_zeros_not_crash(self):
        from src.contracts import ResultRow

        rows = [ResultRow("E1", "UC1.txt", "A.java", 0.9, 1)]
        metrics = evaluate(rows, {})
        assert metrics["MAP"] == 0.0
        assert metrics["P@5"] == 0.0 and metrics["R@10"] == 0.0


# ---------------------------------------------------------------------------
# Whole-corpus behaviour on an unlabelled repository
# ---------------------------------------------------------------------------


def _load(root, **kw):
    from src.pipeline import load_corpus

    return load_corpus(root, **kw)


def _assert_aligned(corpus):
    assert (
        len(corpus.nodes) == len(corpus.node_documents) == corpus.node_vectors.shape[0]
    )


class TestUnlabelledCorpus:
    def test_loads_without_gold_file(self, repo, fake_embedder):
        corpus = _load(repo)
        assert corpus.gold == {}
        assert len(corpus.requirements) == 2
        assert len(corpus.nodes) > 0
        _assert_aligned(corpus)

    def test_strict_gold_defaults_off_for_foreign_corpus(self, repo, fake_embedder):
        # No explicit strict_gold, no gold file, and it must not raise.
        assert _load(repo).gold == {}

    def test_explicit_strict_gold_still_raises(self, repo, fake_embedder):
        with pytest.raises(FileNotFoundError):
            _load(repo, strict_gold=True)

    def test_gold_used_when_present(self, tmp_path, fake_embedder):
        root = make_repo(
            tmp_path / "withgold",
            code={"AdvertBoard.java": BANNER_JAVA},
            reqs={"UC1.txt": REQ_BANNER},
            gold="UC1.txt: AdvertBoard.java\n",
        )
        corpus = _load(root)
        assert corpus.gold == {"UC1.txt": {"AdvertBoard.java"}}

    def test_retrieval_ranks_the_obvious_node_first(self, repo, fake_embedder):
        from src.retrieve.req_to_code import trace_requirements_to_code

        corpus = _load(repo)
        rows = trace_requirements_to_code(
            corpus.requirements,
            corpus.nodes,
            corpus.req_vectors,
            corpus.node_vectors,
            run_id="T",
            top_k=5,
        )
        top = {r.req_id: r.artifact_id for r in rows if r.rank == 1}
        assert "AdvertBoard.java" in top["UC1.txt"], (
            "banner requirement -> banner class"
        )
        assert "AlertDispatcher.java" in top["UC2.txt"], (
            "notify requirement -> alert iface"
        )

    def test_reverse_direction_runs_unlabelled(self, repo, fake_embedder):
        from src.retrieve.code_to_req import trace_code_to_requirements

        corpus = _load(repo)
        rows = trace_code_to_requirements(
            corpus.nodes,
            corpus.requirements,
            corpus.node_vectors,
            corpus.req_vectors,
            run_id="R",
            top_k=2,
        )
        assert rows and all(r.artifact_id.endswith(".txt") for r in rows)

    def test_orphans_found_without_gold(self, repo, fake_embedder):
        from src.retrieve.orphans import find_orphans

        corpus = _load(repo)
        orphans = find_orphans(
            corpus.nodes, corpus.node_vectors, corpus.req_vectors, threshold=0.99
        )
        names = {n.name for n, _ in orphans}
        # getFont shares no vocabulary with either requirement
        assert "getFont" in names


# ---------------------------------------------------------------------------
# Incremental re-indexing on a live repository
# ---------------------------------------------------------------------------


class TestIncrementalReindex:
    def test_noop_reindex_preserves_everything(self, repo, fake_embedder):
        corpus = _load(repo)
        before_ids = {n.node_id for n in corpus.nodes}
        before_vecs = corpus.node_vectors.copy()

        corpus.reindex_file(repo / "code" / "AdvertBoard.java")

        assert {n.node_id for n in corpus.nodes} == before_ids
        _assert_aligned(corpus)
        # same set of vectors, possibly reordered -- compare as sorted rows
        assert np.allclose(
            np.sort(before_vecs, axis=0), np.sort(corpus.node_vectors, axis=0)
        )

    def test_edit_adds_new_method(self, repo, fake_embedder):
        corpus = _load(repo)
        assert not any(n.name == "deleteBanner" for n in corpus.nodes)

        path = repo / "code" / "AdvertBoard.java"
        path.write_text(
            BANNER_JAVA.replace(
                "public String getFont()",
                "public void deleteBanner(int id) { }\n\n    public String getFont()",
            ),
            encoding="utf-8",
        )
        corpus.reindex_file(path)

        assert any(n.name == "deleteBanner" for n in corpus.nodes)
        _assert_aligned(corpus)

    def test_edit_removes_deleted_method(self, repo, fake_embedder):
        corpus = _load(repo)
        path = repo / "code" / "AdvertBoard.java"
        path.write_text("public class AdvertBoard { }", encoding="utf-8")
        corpus.reindex_file(path)

        assert not any(n.name == "insertBanner" for n in corpus.nodes)
        assert any(n.name == "AdvertBoard" for n in corpus.nodes)
        _assert_aligned(corpus)

    def test_deleting_file_drops_its_nodes(self, repo, fake_embedder):
        corpus = _load(repo)
        path = repo / "code" / "LoginController.java"
        path.unlink()
        corpus.reindex_file(path)

        assert not any(n.file_path == "LoginController.java" for n in corpus.nodes)
        assert any(n.file_path == "AdvertBoard.java" for n in corpus.nodes)
        _assert_aligned(corpus)

    def test_other_files_untouched_by_reindex(self, repo, fake_embedder):
        corpus = _load(repo)
        other = {n.node_id for n in corpus.nodes if n.file_path != "AdvertBoard.java"}
        corpus.reindex_file(repo / "code" / "AdvertBoard.java")
        assert {
            n.node_id for n in corpus.nodes if n.file_path != "AdvertBoard.java"
        } == other

    def test_stale_docs_and_enclosing_are_evicted(self, repo, fake_embedder):
        corpus = _load(repo)
        path = repo / "code" / "AdvertBoard.java"
        old_ids = [n.node_id for n in corpus.nodes if n.file_path == "AdvertBoard.java"]

        path.write_text("public class AdvertBoard { }", encoding="utf-8")
        corpus.reindex_file(path)

        live = {n.node_id for n in corpus.nodes}
        for node_id in old_ids:
            if node_id not in live:
                assert node_id not in corpus.docs
                assert node_id not in corpus.enclosing


class TestRefresh:
    def test_no_changes_reports_nothing(self, repo, fake_embedder):
        corpus = _load(repo)
        assert corpus.refresh() == []

    def test_detects_edited_file(self, repo, fake_embedder):
        corpus = _load(repo)
        path = repo / "code" / "LoginController.java"
        path.write_text(
            LOGIN_JAVA.replace(
                "public boolean authenticateOperator",
                "public void logoutOperator() { }\n"
                "    public boolean authenticateOperator",
            ),
            encoding="utf-8",
        )
        # mtime resolution can be coarse; force a distinct value
        import os
        import time

        os.utime(path, (time.time() + 2, time.time() + 2))

        assert corpus.refresh() == ["LoginController.java"]
        assert any(n.name == "logoutOperator" for n in corpus.nodes)
        _assert_aligned(corpus)

    def test_detects_new_file(self, repo, fake_embedder):
        corpus = _load(repo)
        (repo / "code" / "Extra.java").write_text(
            "public class Extra { public void brandNewMethod() { } }", encoding="utf-8"
        )
        assert corpus.refresh() == ["Extra.java"]
        assert any(n.name == "brandNewMethod" for n in corpus.nodes)
        _assert_aligned(corpus)

    def test_detects_deleted_file(self, repo, fake_embedder):
        corpus = _load(repo)
        (repo / "code" / "LoginController.java").unlink()
        assert corpus.refresh() == ["LoginController.java"]
        assert not any(n.file_path == "LoginController.java" for n in corpus.nodes)
        _assert_aligned(corpus)

    def test_refresh_is_idempotent(self, repo, fake_embedder):
        corpus = _load(repo)
        (repo / "code" / "Extra.java").write_text(
            "public class Extra { public void m() { } }", encoding="utf-8"
        )
        assert corpus.refresh() == ["Extra.java"]
        assert corpus.refresh() == [], "second refresh must be a no-op"

    def test_reindex_without_code_root_is_rejected(self, repo, fake_embedder):
        corpus = _load(repo)
        corpus.code_root = None
        assert corpus.refresh() == []
        with pytest.raises(RuntimeError, match="code root"):
            corpus.reindex_file(repo / "code" / "AdvertBoard.java")


# ---------------------------------------------------------------------------
# The offline guarantee
# ---------------------------------------------------------------------------


class TestOfflinePin:
    """`pin_offline_if_cached` makes "no network calls" unconditional.

    The guard matters in both directions: it has to pin offline once the model
    is on disk, and it must NOT pin before the documented first-run download,
    which would leave a fresh clone unable to fetch the model at all.
    """

    def _clean_env(self, monkeypatch):
        monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)

    def test_pins_when_model_present(self, tmp_path, monkeypatch):
        from src.index.embedder import pin_offline_if_cached

        self._clean_env(monkeypatch)
        (tmp_path / "models--x--y" / "snapshots" / "abc").mkdir(parents=True)
        assert pin_offline_if_cached(tmp_path) is True
        assert os.environ["HF_HUB_OFFLINE"] == "1"

    def test_does_not_pin_before_first_download(self, tmp_path, monkeypatch):
        from src.index.embedder import pin_offline_if_cached

        self._clean_env(monkeypatch)
        assert pin_offline_if_cached(tmp_path) is False
        assert "HF_HUB_OFFLINE" not in os.environ

    def test_explicit_env_always_wins(self, tmp_path, monkeypatch):
        from src.index.embedder import pin_offline_if_cached

        (tmp_path / "models--x--y" / "snapshots" / "abc").mkdir(parents=True)
        monkeypatch.setenv("HF_HUB_OFFLINE", "0")
        assert pin_offline_if_cached(tmp_path) is False
        assert os.environ["HF_HUB_OFFLINE"] == "0", "caller override must survive"

    def test_model_is_cached_detects_layout(self, tmp_path):
        from src.index.embedder import model_is_cached

        assert model_is_cached(tmp_path) is False
        (
            tmp_path
            / "models--sentence-transformers--all-MiniLM-L6-v2"
            / "snapshots"
            / "s1"
        ).mkdir(parents=True)
        assert model_is_cached(tmp_path) is True

    def test_entry_points_pin_at_import(self, monkeypatch):
        """The scripts must pin at import time, before torch is loaded."""
        import importlib

        monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
        importlib.reload(importlib.import_module("scripts.run_demo"))
        assert os.environ.get("HF_HUB_OFFLINE") == "1"


# ---------------------------------------------------------------------------
# Real-model integration -- opt in with `pytest -m slow`
# ---------------------------------------------------------------------------


@pytest.mark.slow
class TestRealModelIntegration:
    def test_unlabelled_repo_end_to_end(self, repo):
        """The same flow as above, but with the actual embedder.

        Guards the one thing the fake cannot: that the real `embed_texts`
        signature and the per-node cache still satisfy the pipeline's calls.
        """
        from src.retrieve.req_to_code import trace_requirements_to_code

        corpus = _load(repo)
        assert corpus.gold == {}
        _assert_aligned(corpus)

        rows = trace_requirements_to_code(
            corpus.requirements,
            corpus.nodes,
            corpus.req_vectors,
            corpus.node_vectors,
            run_id="REAL",
            top_k=3,
        )
        top = {r.req_id: r.artifact_id for r in rows if r.rank == 1}
        assert "AdvertBoard.java" in top["UC1.txt"]

    def test_incremental_reindex_with_real_embedder(self, repo):
        corpus = _load(repo)
        path = repo / "code" / "AdvertBoard.java"
        path.write_text(
            "public class AdvertBoard { public void solo() { } }", encoding="utf-8"
        )
        corpus.reindex_file(path)
        _assert_aligned(corpus)
        assert any(n.name == "solo" for n in corpus.nodes)

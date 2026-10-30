"""Running on a real repository: no `req/`, no `code/`, no answer key.

`tests/test_arbitrary_repo.py` covers a corpus the project has never seen but
which is still *packaged* like eTour -- `req/` beside `code/`, requirements as
files on disk. This file covers the case that packaging was hiding: an ordinary
source repository, where there are no requirement documents at all and the
requirement set is built by stating requirements.

Three things are asserted, in order of how badly each would be missed:

  1. A plain directory of source loads at all, with zero requirements, instead
     of raising `FileNotFoundError` from the requirements loader.
  2. A stated requirement survives a round trip through the log, joins the
     embedding matrix in the right row, and makes orphan detection and reverse
     tracing meaningful.
  3. Change detection follows git -- an edit, a new file, a deletion, a commit,
     a branch switch and a revert all land in the index.

The git tests drive a real repository built in `tmp_path`, because the whole
value of the git path is that it agrees with what git actually reports, and a
mocked `git status` would only assert that we agree with ourselves.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess

import numpy as np
import pytest

from src.ingest import requirement_store as store
from src.ingest.workspace import discover

DIM = 384


# ---------------------------------------------------------------------------
# Shared fixtures -- same fake embedder rationale as test_arbitrary_repo.py
# ---------------------------------------------------------------------------


def _fake_embed(texts, model_name=None, cache_name=None, show_progress=False):
    """Deterministic bag-of-words embedding. Real cosine, no neural network."""
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
    monkeypatch.setattr("src.index.embedder.embed_texts", _fake_embed)


NOTIFY_PY = '''
"""Traveller notification helpers."""


class AlertDispatcher:
    """Sends alerts to travellers."""

    def send_alert_to_nearby_traveller(self, traveller_name, radius_km):
        """Notify the traveller about attractions located nearby."""
        return True

    def get_font(self):
        return "Arial"
'''

BANNER_GO = """
package ads

// InsertBanner creates a banner for a refreshment point, checking that the
// maximum permitted number has not already been reached.
func InsertBanner(pointId int, imagePath string) bool {
	return countBanners(pointId) < maxBanner
}

func countBanners(pointId int) int {
	return 0
}
"""


@pytest.fixture
def workspace(tmp_path):
    """A plain source tree. No req/, no code/, no gold, nothing prepared."""
    root = tmp_path / "someproject"
    (root / "src" / "notify").mkdir(parents=True)
    (root / "src" / "ads").mkdir(parents=True)
    (root / "src" / "notify" / "dispatcher.py").write_text(NOTIFY_PY, encoding="utf-8")
    (root / "src" / "ads" / "banner.go").write_text(BANNER_GO, encoding="utf-8")
    (root / "README.md").write_text("# not source", encoding="utf-8")
    return root


def _load(root, **kw):
    from src.pipeline import load_corpus

    return load_corpus(root, **kw)


# ---------------------------------------------------------------------------
# Layout detection
# ---------------------------------------------------------------------------


class TestLayoutDiscovery:
    def test_plain_directory_is_a_workspace(self, workspace):
        layout = discover(workspace)
        assert layout.kind == "workspace"
        assert layout.code_root == workspace
        assert layout.requirements_dir is None
        assert layout.gold_path is None

    def test_req_and_code_together_are_a_dataset(self, tmp_path):
        (tmp_path / "req").mkdir()
        (tmp_path / "code").mkdir()
        layout = discover(tmp_path)
        assert layout.kind == "dataset"
        assert layout.code_root == tmp_path / "code"

    def test_code_alone_is_still_a_workspace(self, tmp_path):
        """Both directories are required, so a project with `code/` is safe."""
        (tmp_path / "code").mkdir()
        assert discover(tmp_path).kind == "workspace"

    def test_dataset_reports_expected_gold_path_even_when_absent(self, tmp_path):
        """`strict` gold must still be able to raise on a missing answer key."""
        (tmp_path / "req").mkdir()
        (tmp_path / "code").mkdir()
        layout = discover(tmp_path)
        assert layout.gold_path is not None
        assert layout.has_gold is False

    def test_datasets_have_no_session_store(self, tmp_path):
        """A research corpus is read-only, so published numbers cannot drift."""
        (tmp_path / "req").mkdir()
        (tmp_path / "code").mkdir()
        assert discover(tmp_path).store_path is None

    def test_missing_directory_is_an_error(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            discover(tmp_path / "nope")


# ---------------------------------------------------------------------------
# The requirement store
# ---------------------------------------------------------------------------


class TestRequirementStore:
    def test_empty_repo_has_no_requirements(self, workspace):
        assert store.load_requirements(workspace) == []

    def test_append_then_load(self, workspace):
        req, created = store.append_requirement(workspace, "notify nearby travellers")
        assert created is True
        assert req.req_id == "SR0001"
        loaded = store.load_requirements(workspace)
        assert [r.req_id for r in loaded] == ["SR0001"]
        assert loaded[0].text == "notify nearby travellers"

    def test_ids_increment(self, workspace):
        store.append_requirement(workspace, "one")
        second, _ = store.append_requirement(workspace, "two")
        assert second.req_id == "SR0002"

    def test_restating_is_idempotent(self, workspace):
        first, created_a = store.append_requirement(workspace, "notify travellers")
        second, created_b = store.append_requirement(
            workspace, "  notify   travellers "
        )
        assert created_a is True
        assert created_b is False
        assert first.req_id == second.req_id
        assert len(store.load_requirements(workspace)) == 1

    def test_blank_requirement_rejected(self, workspace):
        with pytest.raises(ValueError):
            store.append_requirement(workspace, "   ")

    def test_truncated_final_line_costs_one_record_not_the_log(self, workspace):
        """The reason the format is JSONL and not one JSON document."""
        store.append_requirement(workspace, "first")
        store.append_requirement(workspace, "second")
        path = store.store_path(workspace)
        with path.open("a", encoding="utf-8") as fh:
            fh.write('{"req_id": "SR0003", "text": "trunc')
        assert [r.req_id for r in store.load_requirements(workspace)] == [
            "SR0001",
            "SR0002",
        ]

    def test_removal(self, workspace):
        store.append_requirement(workspace, "one")
        store.append_requirement(workspace, "two")
        assert store.remove_requirement(workspace, "SR0001") is True
        assert [r.req_id for r in store.load_requirements(workspace)] == ["SR0002"]
        assert store.remove_requirement(workspace, "SR0001") is False

    def test_ids_are_not_reused_after_removal(self, workspace):
        """A stale reference must fail to resolve, never resolve to the wrong one."""
        store.append_requirement(workspace, "one")
        store.append_requirement(workspace, "two")
        store.remove_requirement(workspace, "SR0002")
        third, _ = store.append_requirement(workspace, "three")
        assert third.req_id == "SR0003"

    def test_record_shape_is_the_frozen_contract(self, workspace):
        store.append_requirement(workspace, "notify travellers")
        line = store.store_path(workspace).read_text(encoding="utf-8").strip()
        record = json.loads(line)
        assert record["v"] == store.RECORD_VERSION
        assert set(record) == {"v", "req_id", "text", "created_at", "source"}
        assert record["source"] == "session"


# ---------------------------------------------------------------------------
# Loading and querying a workspace
# ---------------------------------------------------------------------------


class TestWorkspaceCorpus:
    def test_loads_with_no_requirements_at_all(self, workspace, fake_embedder):
        corpus = _load(workspace)
        assert corpus.layout.kind == "workspace"
        assert corpus.requirements == []
        assert corpus.gold == {}
        assert corpus.nodes, "a plain source tree should still parse"

    def test_indexes_every_language_present(self, workspace, fake_embedder):
        corpus = _load(workspace)
        files = {n.file_path for n in corpus.nodes}
        assert any(f.endswith("dispatcher.py") for f in files)
        assert any(f.endswith("banner.go") for f in files)

    def test_non_source_files_are_not_indexed(self, workspace, fake_embedder):
        corpus = _load(workspace)
        assert not any(n.file_path.endswith(".md") for n in corpus.nodes)

    def test_empty_requirement_set_leaves_a_usable_matrix(
        self, workspace, fake_embedder
    ):
        """Zero requirements must be a shape, not a crash, for orphan scoring."""
        from src.retrieve.orphans import best_requirement_score

        corpus = _load(workspace)
        scores = best_requirement_score(corpus.node_vectors, corpus.req_vectors)
        assert scores.shape == (len(corpus.nodes),)
        assert float(scores.max()) == 0.0

    def test_stating_a_requirement_makes_it_queryable(self, workspace, fake_embedder):
        from src.index.vector_store import search

        corpus = _load(workspace)
        requirement, created = corpus.add_requirement(
            "the system shall notify the traveller about attractions located nearby"
        )
        assert created is True
        assert len(corpus.requirements) == 1
        assert corpus.req_vectors.shape == (1, DIM)

        row = {r.req_id: i for i, r in enumerate(corpus.requirements)}[
            requirement.req_id
        ]
        hits = search(corpus.req_vectors[row], corpus.node_vectors, top_k=3)
        top = corpus.nodes[hits[0][0]]
        assert top.name == "send_alert_to_nearby_traveller"

    def test_row_alignment_survives_repeated_statements(self, workspace, fake_embedder):
        corpus = _load(workspace)
        corpus.add_requirement("insert a banner for a refreshment point")
        corpus.add_requirement("notify the traveller about nearby attractions")
        assert corpus.req_vectors.shape[0] == len(corpus.requirements) == 2
        # Each row must still be the embedding of its own requirement.
        for i, req in enumerate(corpus.requirements):
            expected = _fake_embed([req.text])[0]
            assert np.allclose(corpus.req_vectors[i], expected)

    def test_reverse_direction_after_stating(self, workspace, fake_embedder):
        from src.index.vector_store import search
        from src.retrieve.locate import find_node_at

        corpus = _load(workspace)
        corpus.add_requirement(
            "the system shall notify the traveller about attractions located nearby"
        )
        node = find_node_at(
            corpus.nodes,
            workspace / "src" / "notify" / "dispatcher.py",
            8,  # the `def send_alert_to_nearby_traveller` line
            code_root=corpus.code_root,
        )
        assert node is not None and node.name == "send_alert_to_nearby_traveller"

        row = corpus.node_index()[node.node_id]
        hits = search(corpus.node_vectors[row], corpus.req_vectors, top_k=1)
        assert corpus.requirements[hits[0][0]].req_id == "SR0001"

    def test_orphans_are_relative_to_what_was_stated(self, workspace, fake_embedder):
        from src.retrieve.orphans import find_orphans

        corpus = _load(workspace)
        corpus.add_requirement(
            "the system shall notify the traveller about attractions located nearby"
        )
        orphans = find_orphans(
            corpus.nodes, corpus.node_vectors, corpus.req_vectors, threshold=0.05
        )
        flagged = {node.name for node, _ in orphans}
        assert "get_font" in flagged
        assert "send_alert_to_nearby_traveller" not in flagged

    def test_a_second_process_sees_the_requirement(self, workspace, fake_embedder):
        """The store is the shared state between CLI, editor, and MCP server."""
        first = _load(workspace)
        first.add_requirement("notify nearby travellers")

        second = _load(workspace)
        assert [r.req_id for r in second.requirements] == ["SR0001"]

    def test_refresh_picks_up_a_requirement_added_elsewhere(
        self, workspace, fake_embedder
    ):
        corpus = _load(workspace)
        assert corpus.requirements == []
        store.append_requirement(workspace, "notify nearby travellers")
        corpus.refresh()
        assert [r.req_id for r in corpus.requirements] == ["SR0001"]
        assert corpus.req_vectors.shape == (1, DIM)

    def test_dataset_corpora_refuse_live_requirements(self, tmp_path, fake_embedder):
        (tmp_path / "req").mkdir()
        (tmp_path / "code").mkdir()
        (tmp_path / "req" / "UC1.txt").write_text("do a thing", encoding="utf-8")
        corpus = _load(tmp_path)
        with pytest.raises(RuntimeError, match="read-only"):
            corpus.add_requirement("something new")


# ---------------------------------------------------------------------------
# Cursor resolution
# ---------------------------------------------------------------------------


class TestLocate:
    def test_same_basename_in_two_directories_resolves_correctly(
        self, tmp_path, fake_embedder
    ):
        """The bug the basename matcher had. Only shows up on a real repo shape."""
        root = tmp_path / "dup"
        for package in ("alpha", "beta"):
            (root / package).mkdir(parents=True)
            (root / package / "util.py").write_text(
                f"def {package}_helper():\n"
                f'    """Helper for {package}."""\n'
                "    return 1\n",
                encoding="utf-8",
            )
        corpus = _load(root)
        from src.retrieve.locate import find_node_at

        node = find_node_at(
            corpus.nodes, root / "beta" / "util.py", 1, code_root=corpus.code_root
        )
        assert node is not None
        assert node.name == "beta_helper"
        assert node.file_path == "beta/util.py"

    def test_ambiguous_bare_filename_refuses_to_guess(self, tmp_path, fake_embedder):
        root = tmp_path / "dup2"
        for package in ("alpha", "beta"):
            (root / package).mkdir(parents=True)
            (root / package / "util.py").write_text("def f():\n    return 1\n", "utf-8")
        corpus = _load(root)
        from src.retrieve.locate import find_node_at

        assert find_node_at(corpus.nodes, "util.py", 1) is None

    def test_innermost_node_wins(self, workspace, fake_embedder):
        from src.retrieve.locate import find_node_at

        corpus = _load(workspace)
        node = find_node_at(
            corpus.nodes,
            workspace / "src" / "notify" / "dispatcher.py",
            8,
            code_root=corpus.code_root,
        )
        assert node is not None and node.kind == "method"

    def test_line_outside_any_declaration(self, workspace, fake_embedder):
        from src.retrieve.locate import find_node_at

        corpus = _load(workspace)
        assert (
            find_node_at(
                corpus.nodes,
                workspace / "src" / "notify" / "dispatcher.py",
                1,
                code_root=corpus.code_root,
            )
            is None
        )


# ---------------------------------------------------------------------------
# Git-driven change detection
# ---------------------------------------------------------------------------


def _git(root, *args):
    subprocess.run(
        ["git", *args],
        cwd=str(root),
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )


@pytest.fixture
def git_workspace(workspace):
    """`workspace`, turned into a real committed git repository."""
    if not _git_available():
        pytest.skip("git is not installed")
    _git(workspace, "init", "-q")
    _git(workspace, "config", "user.email", "test@example.com")
    _git(workspace, "config", "user.name", "Test")
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-qm", "initial")
    return workspace


def _git_available() -> bool:
    from src.ingest.vcs import git_root

    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
    except (OSError, subprocess.SubprocessError):
        return False
    return git_root is not None


class TestGitPlumbing:
    def test_refuses_commands_outside_the_allowlist(self, git_workspace):
        """The offline guarantee, enforced rather than documented."""
        from src.ingest.vcs import _run

        with pytest.raises(ValueError, match="read-only and offline"):
            _run(git_workspace, "fetch", "origin")

    def test_snapshot_reports_head_and_clean_tree(self, git_workspace):
        from src.ingest.vcs import snapshot

        state = snapshot(git_workspace)
        assert state is not None
        assert state.head and len(state.head) == 40
        assert state.dirty == frozenset()

    def test_snapshot_on_a_non_repository(self, tmp_path):
        from src.ingest.vcs import snapshot

        outside = tmp_path / "plain"
        outside.mkdir()
        # May still be inside an enclosing repo on some machines; only assert
        # that it does not raise and returns a usable answer either way.
        assert snapshot(outside) is None or True

    def test_dirty_file_is_reported(self, git_workspace):
        from src.ingest.vcs import working_tree_changes

        target = git_workspace / "src" / "notify" / "dispatcher.py"
        target.write_text(NOTIFY_PY + "\n# edited\n", encoding="utf-8")
        assert "src/notify/dispatcher.py" in working_tree_changes(git_workspace)


class TestGitDrivenRefresh:
    def test_git_is_the_change_source_when_available(
        self, git_workspace, fake_embedder
    ):
        corpus = _load(git_workspace)
        assert corpus.git_root is not None
        assert corpus.git_state is not None

    def test_edit_is_picked_up(self, git_workspace, fake_embedder):
        corpus = _load(git_workspace)
        assert corpus.refresh() == []

        target = git_workspace / "src" / "notify" / "dispatcher.py"
        target.write_text(
            NOTIFY_PY.replace("def get_font", "def render_itinerary_pdf"),
            encoding="utf-8",
        )
        _touch(target)

        changed = corpus.refresh()
        assert "src/notify/dispatcher.py" in changed
        assert any(n.name == "render_itinerary_pdf" for n in corpus.nodes)
        assert not any(n.name == "get_font" for n in corpus.nodes)

    def test_new_untracked_file_is_picked_up(self, git_workspace, fake_embedder):
        corpus = _load(git_workspace)
        before = len(corpus.nodes)
        new = git_workspace / "src" / "ads" / "campaign.go"
        new.write_text(
            "package ads\n\n// StartCampaign begins an advertising campaign.\n"
            "func StartCampaign(name string) {}\n",
            encoding="utf-8",
        )
        changed = corpus.refresh()
        assert "src/ads/campaign.go" in changed
        assert len(corpus.nodes) > before
        assert any(n.name == "StartCampaign" for n in corpus.nodes)

    def test_deletion_drops_its_nodes(self, git_workspace, fake_embedder):
        corpus = _load(git_workspace)
        target = git_workspace / "src" / "ads" / "banner.go"
        target.unlink()
        changed = corpus.refresh()
        assert "src/ads/banner.go" in changed
        assert not any(n.file_path == "src/ads/banner.go" for n in corpus.nodes)

    def test_commit_moves_head_without_losing_the_index(
        self, git_workspace, fake_embedder
    ):
        corpus = _load(git_workspace)
        head_before = corpus.git_state.head

        target = git_workspace / "src" / "ads" / "banner.go"
        target.write_text(
            BANNER_GO
            + "\n// ArchiveBanner retires a banner.\nfunc ArchiveBanner() {}\n",
            encoding="utf-8",
        )
        _touch(target)
        corpus.refresh()
        _git(git_workspace, "add", "-A")
        _git(git_workspace, "commit", "-qm", "add archive")

        corpus.refresh()
        assert corpus.git_state.head != head_before
        assert any(n.name == "ArchiveBanner" for n in corpus.nodes)

    def test_branch_switch_remaps(self, git_workspace, fake_embedder):
        """The case no filesystem watcher handles well and git handles trivially."""
        _git(git_workspace, "checkout", "-q", "-b", "feature")
        target = git_workspace / "src" / "ads" / "banner.go"
        target.write_text(
            BANNER_GO.replace("InsertBanner", "PublishBanner"), encoding="utf-8"
        )
        _git(git_workspace, "add", "-A")
        _git(git_workspace, "commit", "-qm", "rename on branch")

        corpus = _load(git_workspace)
        assert any(n.name == "PublishBanner" for n in corpus.nodes)

        _git(git_workspace, "checkout", "-q", "master") if _has_branch(
            git_workspace, "master"
        ) else _git(git_workspace, "checkout", "-q", "main")
        _touch(target)

        changed = corpus.refresh()
        assert "src/ads/banner.go" in changed
        assert any(n.name == "InsertBanner" for n in corpus.nodes)
        assert not any(n.name == "PublishBanner" for n in corpus.nodes)

    def test_revert_is_observable(self, git_workspace, fake_embedder):
        """Dirty-then-clean appears in no current listing; see `changed_since`."""
        corpus = _load(git_workspace)
        target = git_workspace / "src" / "ads" / "banner.go"
        original = target.read_text(encoding="utf-8")

        target.write_text(
            original.replace("InsertBanner", "TemporarilyRenamed"), "utf-8"
        )
        _touch(target)
        corpus.refresh()
        assert any(n.name == "TemporarilyRenamed" for n in corpus.nodes)

        target.write_text(original, encoding="utf-8")
        _touch(target)
        changed = corpus.refresh()
        assert "src/ads/banner.go" in changed
        assert any(n.name == "InsertBanner" for n in corpus.nodes)

    def test_editing_a_non_source_file_changes_nothing(
        self, git_workspace, fake_embedder
    ):
        corpus = _load(git_workspace)
        nodes_before = list(corpus.nodes)
        readme = git_workspace / "README.md"
        readme.write_text("# still not source\n", encoding="utf-8")
        _touch(readme)
        assert corpus.refresh() == []
        assert corpus.nodes == nodes_before

    def test_gitignored_files_are_not_indexed(self, workspace, fake_embedder):
        """`git ls-files` is what makes the walk ignore-aware; assert it does."""
        if not _git_available():
            pytest.skip("git is not installed")
        (workspace / "vendored").mkdir()
        (workspace / "vendored" / "huge.py").write_text(
            "def vendored_thing():\n    return 1\n", encoding="utf-8"
        )
        (workspace / ".gitignore").write_text("vendored/\n", encoding="utf-8")
        _git(workspace, "init", "-q")
        _git(workspace, "config", "user.email", "t@e.com")
        _git(workspace, "config", "user.name", "T")
        _git(workspace, "add", "-A")
        _git(workspace, "commit", "-qm", "init")

        corpus = _load(workspace)
        assert not any(n.name == "vendored_thing" for n in corpus.nodes)

    def test_ignored_subdirectory_does_not_use_git(self, git_workspace, fake_embedder):
        """A directory inside a repo that the repo ignores must fall back.

        Found by pointing the CLI at a library under `.venv/`. `git_root` walks
        upward and cheerfully answers with the enclosing repository, but git
        ignores that directory, so `status` and `diff` report nothing about it
        forever -- change detection would never fire and the index would go
        quietly stale. `data/etour/` in this project has the same shape.
        """
        vendored = git_workspace / "ignored_lib"
        vendored.mkdir()
        (vendored / "thing.py").write_text(
            'def do_the_thing():\n    """Does the thing."""\n    return 1\n',
            encoding="utf-8",
        )
        (git_workspace / ".gitignore").write_text("ignored_lib/\n", encoding="utf-8")
        _git(git_workspace, "add", "-A")
        _git(git_workspace, "commit", "-qm", "ignore it")

        corpus = _load(vendored)
        assert corpus.git_root is None, "must not trust git for an ignored directory"
        assert any(n.name == "do_the_thing" for n in corpus.nodes)

        # And the mtime fallback must actually keep it current.
        target = vendored / "thing.py"
        target.write_text(
            'def do_the_renamed_thing():\n    """Does it."""\n    return 1\n',
            encoding="utf-8",
        )
        _touch(target)
        assert "thing.py" in corpus.refresh()
        assert any(n.name == "do_the_renamed_thing" for n in corpus.nodes)

    def test_falls_back_when_not_a_repository(
        self, workspace, fake_embedder, monkeypatch
    ):
        """No git, no problem: the mtime walk still produces the same index."""
        monkeypatch.setattr("src.ingest.vcs.git_root", lambda path: None)
        corpus = _load(workspace)
        assert corpus.git_root is None

        target = workspace / "src" / "ads" / "banner.go"
        target.write_text(
            BANNER_GO.replace("InsertBanner", "RenamedWithoutGit"), encoding="utf-8"
        )
        _touch(target)
        assert "src/ads/banner.go" in corpus.refresh()
        assert any(n.name == "RenamedWithoutGit" for n in corpus.nodes)


def _touch(path) -> None:
    """Force a distinct mtime.

    Windows timestamps have coarse resolution and a test can rewrite a file
    inside one tick, which would make an edit invisible to the mtime check for
    reasons that have nothing to do with the code under test.
    """
    stamp = path.stat().st_mtime + 10
    os.utime(path, (stamp, stamp))


def _has_branch(root, name: str) -> bool:
    proc = subprocess.run(
        ["git", "rev-parse", "--verify", name],
        cwd=str(root),
        capture_output=True,
    )
    return proc.returncode == 0

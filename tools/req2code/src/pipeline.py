"""Corpus loading, wired once and reused by every run.

Both entry points (demo and ablation) need the same expensive setup: parse the
repo, build node documents, embed everything. Doing that once and reusing it is
what keeps a six-configuration ablation to seconds rather than minutes -- the
corpus does not change between configurations, only the scoring does.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from src.contracts import CodeNode, Requirement
from src.ingest.vcs import GitState
from src.ingest.workspace import GOLD_FILENAME, Layout, discover

DEFAULT_DATA_DIR = Path("data/etour")

__all__ = ["DEFAULT_DATA_DIR", "GOLD_FILENAME", "Corpus", "load_corpus"]


@dataclass
class Corpus:
    """Everything the retrieval and evaluation layers need, loaded once."""

    requirements: list[Requirement]
    nodes: list[CodeNode]
    docs: dict[str, str]  # node_id -> raw javadoc
    enclosing: dict[str, str]  # node_id -> enclosing class name
    node_documents: list[str]  # row-aligned with nodes
    gold: dict[str, set[str]]
    req_vectors: np.ndarray = field(repr=False)
    node_vectors: np.ndarray = field(repr=False)
    timings: dict[str, float] = field(default_factory=dict, repr=False)
    code_root: Path | None = field(default=None, repr=False)
    mtimes: dict[str, float] = field(default_factory=dict, repr=False)

    #: How this corpus was discovered. None for a `Corpus` built by hand in a
    #: test; present for anything `load_corpus` produced.
    layout: Layout | None = field(default=None, repr=False)

    #: Git working tree containing `code_root`, and the state it was in when we
    #: last looked. Both None when the target is not a git repository, which is
    #: a supported configuration, not a degraded one -- `refresh` falls back to
    #: the mtime walk and behaves exactly as it did before git existed here.
    git_root: Path | None = field(default=None, repr=False)
    git_state: GitState | None = field(default=None, repr=False)

    @property
    def node_to_file(self) -> dict[str, str]:
        """node_id -> file_path, for max-aggregation up to gold granularity."""
        return {n.node_id: n.file_path for n in self.nodes}

    def node_index(self) -> dict[str, int]:
        """node_id -> row index in `node_vectors`."""
        return {n.node_id: i for i, n in enumerate(self.nodes)}

    # -- incremental update ------------------------------------------------
    #
    # A corpus that can only be rebuilt from scratch cannot live in an editor:
    # a full rebuild is ~2.7s of embedding, which is 30x over the ~100ms budget
    # where a user stops perceiving an action as instant. Re-embedding only the
    # edited file costs ~45ms, because the per-text cache in index/embedder.py
    # means unchanged nodes are dict lookups rather than model calls.
    #
    # Row alignment between `nodes`, `node_documents` and `node_vectors` is the
    # invariant everything downstream depends on, so all three are rebuilt
    # together, never separately.

    def reindex_file(self, path: str | Path) -> int:
        """Re-parse and re-embed a single source file in place.

        Args:
            path: Path to the ``.java`` file. May be absolute or relative to
                the repo root. A file that no longer exists has its nodes
                dropped, which is what makes deletion work.

        Returns:
            Number of nodes the file now contributes (0 if deleted).
        """
        from src.index.embedder import embed_texts
        from src.index.node_doc import build_all_documents
        from src.parse.parser import enclosing_class_map, parse_file

        if self.code_root is None:
            raise RuntimeError("Corpus was not built from a code root; cannot reindex.")

        path = Path(path)
        rel = str(path.resolve().relative_to(self.code_root.resolve())).replace(
            "\\", "/"
        )

        # Drop everything the old version of this file contributed.
        keep = [i for i, n in enumerate(self.nodes) if n.file_path != rel]
        for node in self.nodes:
            if node.file_path == rel:
                self.docs.pop(node.node_id, None)
                self.enclosing.pop(node.node_id, None)

        if path.exists():
            new_nodes, new_docs = parse_file(path, self.code_root)
            new_enclosing = enclosing_class_map(new_nodes)
            new_documents = build_all_documents(new_nodes, new_enclosing, new_docs)
            new_vectors = embed_texts(new_documents, cache_name="nodes")
            self.docs.update(new_docs)
            self.enclosing.update(new_enclosing)
            self.mtimes[rel] = path.stat().st_mtime
        else:
            new_nodes, new_documents = [], []
            new_vectors = np.zeros((0, self.node_vectors.shape[1]), dtype=np.float32)
            self.mtimes.pop(rel, None)

        self.nodes = [self.nodes[i] for i in keep] + new_nodes
        self.node_documents = [self.node_documents[i] for i in keep] + new_documents
        self.node_vectors = np.vstack([self.node_vectors[keep], new_vectors])
        return len(new_nodes)

    def refresh(self) -> list[str]:
        """Bring the index back in line with the working tree. Returns the files
        that were reindexed.

        Also syncs the session requirement log, so a requirement stated by
        another process -- the CLI, a second editor window -- becomes visible to
        orphan scoring and reverse tracing without a restart. Requirement ids
        are not in the return value, which stays "source files touched"; use
        `len(corpus.requirements)` to observe that side.

        TWO STRATEGIES, ONE INVARIANT. Where git is available, it supplies the
        *candidate* set (see `ingest/vcs.py`); otherwise every source file is
        walked, as before. Either way the decision to reindex is made by mtime,
        so the two paths cannot disagree about the resulting index -- only about
        how much work it took to get there.

        That matters at scale. The walk is O(files in the repository) and runs
        on every query; the git path is three subprocesses whose cost does not
        grow with the tree. On eTour's 116 files the walk is ~9ms and either is
        fine. On a 20k-file checkout the walk is not fine, and it is the reason
        this method needed a second strategy at all.

        Polling on the read path -- rather than a filesystem watcher -- is
        unchanged and deliberate: it removes a dependency, a background thread,
        and the whole class of bugs where a watcher misses an event and the
        index goes quietly stale.
        """
        if self.code_root is None:
            return []

        self._refresh_requirements()

        candidates = self._git_candidates()
        if candidates is None:
            return self._refresh_by_walk()
        return self._reindex_if_stale(candidates)

    # -- refresh internals -------------------------------------------------

    def _git_candidates(self) -> list[Path] | None:
        """Paths git says may have moved, or None to fall back to the walk.

        None covers every way git can be unhelpful: not a repository, not
        installed, or an answer we cannot trust because a recorded commit has
        since been rewritten away. Falling back is always safe -- the walk is a
        superset of anything git could have reported.
        """
        if self.git_root is None or self.git_state is None:
            return None
        from src.ingest.vcs import changed_since

        result = changed_since(self.git_root, self.git_state)
        if result is None:
            # Trust lost (rebase, gc, git vanished). Take the full walk once and
            # re-establish a baseline, rather than serving a stale index.
            from src.ingest.vcs import snapshot

            self.git_state = snapshot(self.git_root)
            return None

        changed, state = result
        self.git_state = state

        # Git speaks in paths relative to the working tree root, which is not
        # necessarily `code_root` -- a dataset checked out inside a repository
        # has one nested in the other. Anything outside is not ours to index.
        root = self.code_root.resolve()
        out: list[Path] = []
        for rel in changed:
            path = (self.git_root / rel).resolve()
            try:
                path.relative_to(root)
            except ValueError:
                continue
            out.append(path)
        return out

    def _reindex_if_stale(self, paths: Iterable[Path]) -> list[str]:
        """Reindex each path whose mtime moved. Returns repo-relative names.

        The mtime check is what makes a git candidate list cheap to over-report.
        A file that git calls dirty on every refresh, because it has been dirty
        for a week, costs one `stat` here rather than a parse and an embed.
        """
        root = self.code_root.resolve()
        changed: list[str] = []
        for path in paths:
            try:
                rel = str(path.resolve().relative_to(root)).replace("\\", "/")
            except (OSError, ValueError):
                continue
            try:
                current = path.stat().st_mtime
            except OSError:
                # Deleted, or never readable. Only interesting if we had it.
                if rel in self.mtimes:
                    self.reindex_file(path)
                    changed.append(rel)
                continue
            if not self._indexable(path):
                continue
            if self.mtimes.get(rel) != current:
                self.reindex_file(path)
                changed.append(rel)
        return changed

    def _indexable(self, path: Path) -> bool:
        """Whether a path is one we would have indexed in the first place.

        Git reports every file it knows about -- READMEs, lockfiles, images. The
        walker filters those by suffix; on the git path nothing has filtered
        them yet, and parsing a 40MB lockfile because someone edited it would be
        a self-inflicted latency spike.
        """
        from src.ingest.repo_walker import MAX_FILE_BYTES, SKIP_DIRS
        from src.parse.languages import spec_for_path

        if spec_for_path(path) is None:
            return False
        try:
            rel_parts = path.resolve().relative_to(self.code_root.resolve()).parts
        except (OSError, ValueError):
            return False
        if any(part.lower() in SKIP_DIRS for part in rel_parts):
            return False
        try:
            return path.stat().st_size <= MAX_FILE_BYTES
        except OSError:
            return False

    def _refresh_by_walk(self) -> list[str]:
        """The original strategy: stat every source file under `code_root`.

        `use_git` is switched off when we already established at load time that
        this is not a working tree, so the walk does not spend a subprocess
        re-asking a question we know the answer to. That matters on eTour, whose
        refresh is ~9ms: a pointless `git rev-parse` would more than double it.
        When git *is* present and we are here only because a commit went missing,
        the probe is left on so the fallback still respects `.gitignore` and
        cannot produce a different index from the incremental path.
        """
        from src.ingest.repo_walker import walk_source_files

        # Resolve the root once, not once per file: `Path.resolve()` touches the
        # filesystem, and doing it 116 times is most of this function's cost.
        root = self.code_root.resolve()
        changed: list[str] = []
        seen: set[str] = set()
        use_git = self.git_root is not None
        for path in walk_source_files(self.code_root, use_git=use_git):
            rel = str(path.relative_to(root)).replace("\\", "/")
            seen.add(rel)
            if self.mtimes.get(rel) != path.stat().st_mtime:
                self.reindex_file(path)
                changed.append(rel)

        for rel in [r for r in self.mtimes if r not in seen]:
            self.reindex_file(self.code_root / rel)
            changed.append(rel)
        return changed

    # -- live requirements -------------------------------------------------

    def _refresh_requirements(self) -> list[Requirement]:
        """Pull in requirements added to the session log since we last looked.

        Appends rather than rebuilds: existing rows of `req_vectors` keep their
        indices, so anything holding a row number stays correct. That row
        alignment between `requirements` and `req_vectors` is the same invariant
        `reindex_file` maintains for nodes, and it is equally load-bearing.
        """
        if self.layout is None or self.layout.store_path is None:
            return []
        from src.index.embedder import embed_texts
        from src.ingest.requirement_store import load_requirements

        known = {r.req_id for r in self.requirements}
        fresh = [
            r for r in load_requirements(self.layout.root) if r.req_id not in known
        ]
        if not fresh:
            return []

        vectors = embed_texts([r.text for r in fresh], cache_name="reqs")
        self.requirements = self.requirements + fresh
        # An empty corpus starts with a (0, dim) matrix, which vstacks cleanly.
        # A hand-built one may not have a second axis at all, and stacking that
        # would raise -- so the first requirement on an empty set replaces
        # rather than appends.
        empty = self.req_vectors.ndim != 2 or self.req_vectors.shape[0] == 0
        self.req_vectors = vectors if empty else np.vstack([self.req_vectors, vectors])
        return fresh

    def add_requirement(self, text: str) -> tuple[Requirement, bool]:
        """State a requirement: append it to the log and embed it immediately.

        This is the primary product path. On a real repository nobody has a
        folder of use-case documents, so the requirement set is built by the act
        of asking -- see `ingest/requirement_store.py` for why that is a better
        definition of "orphan" as well as a more practical one.

        Returns `(requirement, created)`; `created` is False when an identical
        requirement was already logged, because re-stating a requirement to
        refine a search must not double its weight in orphan scoring.

        Raises:
            RuntimeError: on a dataset corpus. eTour and anything shaped like it
                is read-only, so that published numbers can never depend on
                local state that is not in the repository.
        """
        if self.layout is None or self.layout.store_path is None:
            raise RuntimeError(
                "This corpus has requirement documents on disk and is read-only. "
                "Live requirements are for workspace mode -- point req2code at a "
                "source repository instead."
            )
        from src.ingest.requirement_store import append_requirement

        requirement, created = append_requirement(self.layout.root, text)
        self._refresh_requirements()
        return requirement, created

    def remove_requirement(self, req_id: str) -> bool:
        """Forget a requirement, in the log and in memory. Returns whether it existed.

        `_refresh_requirements` only ever appends -- it exists to notice new
        rows, and asking it to also detect removals would mean diffing the whole
        set on every query for an event that happens once in a session. So a
        removal is applied here, to both places, at the moment it happens.

        The row is dropped from `req_vectors` rather than zeroed. A zero row
        still participates in `argmax` over the requirement axis and would show
        up as a 0.0-scoring match for every node, quietly re-floating deleted
        requirements into orphan scoring.
        """
        if self.layout is None or self.layout.store_path is None:
            raise RuntimeError("This corpus is read-only; nothing to forget.")
        from src.ingest.requirement_store import remove_requirement as _remove

        if not _remove(self.layout.root, req_id):
            return False

        keep = [i for i, r in enumerate(self.requirements) if r.req_id != req_id]
        self.requirements = [self.requirements[i] for i in keep]
        self.req_vectors = self.req_vectors[keep]
        return True


def load_corpus(
    data_dir: str | Path = DEFAULT_DATA_DIR,
    strict_gold: bool | None = None,
    verbose: bool = False,
) -> Corpus:
    """Parse, document-build, and embed whatever is at ``data_dir``.

    Two layouts are accepted, told apart by looking rather than by a flag (see
    `ingest/workspace.py`):

    **dataset** -- ``<data_dir>/req/*.txt`` beside ``<data_dir>/code/``. A
    research corpus. An answer key at `GOLD_FILENAME` is optional: present it
    enables scoring, absent `Corpus.gold` is empty and everything else is
    unchanged. Nothing outside eval/ reads gold.

    **workspace** -- anything else. ``data_dir`` *is* the source tree. There are
    no requirement documents and no answer key; requirements come from the
    session log and accumulate as the user states them, so this returns a
    working corpus with zero requirements rather than refusing to load. That is
    the normal state of a repository someone just pointed the tool at, and it is
    why retrieval had to stop depending on the corpus layout.

    Args:
        data_dir: Corpus root or repository root.
        strict_gold: Enforce the documented eTour answer-key shape. Defaults to
            ``True`` for the bundled eTour path and ``False`` for anything else,
            so the published numbers stay guarded by the count assertions while
            an arbitrary repository just works. Pass explicitly to override.
        verbose: Print per-step timings.

    Embeddings are content-hash cached per node to ``models/.embcache``, so the
    first run pays the model cost and later runs -- and single-file edits -- are
    near-instant. See `Corpus.reindex_file`.
    """
    from src.eval.gold_loader import load_gold_links
    from src.index.embedder import embed_texts
    from src.index.node_doc import build_all_documents
    from src.ingest.requirement_store import load_requirements as load_session
    from src.ingest.requirements_loader import load_requirements
    from src.ingest.vcs import git_root as find_git_root
    from src.ingest.vcs import snapshot, tracks_path
    from src.parse.parser import enclosing_class_map, parse_repo

    data_dir = Path(data_dir)
    layout = discover(data_dir)
    if strict_gold is None:
        strict_gold = data_dir.resolve() == Path(DEFAULT_DATA_DIR).resolve()
    timings: dict[str, float] = {}

    def step(label: str):
        start = time.perf_counter()

        def done():
            timings[label] = time.perf_counter() - start
            if verbose:
                print(f"  {label:22} {timings[label]:6.2f}s")

        return done

    finish = step("load requirements")
    if layout.requirements_dir is not None:
        requirements = load_requirements(layout.requirements_dir)
    else:
        requirements = load_session(layout.root)
    finish()

    finish = step("parse source")
    code_root = layout.code_root
    nodes, docs = parse_repo(code_root)
    enclosing = enclosing_class_map(nodes)
    finish()

    finish = step("build node docs")
    node_documents = build_all_documents(nodes, enclosing, docs)
    finish()

    finish = step("load gold links")
    gold = (
        load_gold_links(layout.gold_path, strict=strict_gold)
        if layout.gold_path is not None
        else {}
    )
    finish()

    finish = step("embed requirements")
    req_vectors = embed_texts([r.text for r in requirements], cache_name="reqs")
    finish()

    finish = step("embed nodes")
    node_vectors = embed_texts(node_documents, cache_name="nodes")
    finish()

    from src.ingest.repo_walker import walk_source_files

    mtimes = {
        str(p.resolve().relative_to(code_root.resolve())).replace(
            "\\", "/"
        ): p.stat().st_mtime
        for p in walk_source_files(code_root)
    }

    # Establish the git baseline at load time, so the first `refresh()` has
    # something to diff against. A target that is not a git working tree -- or is
    # inside one but ignored by it -- leaves both fields None and `refresh` uses
    # the filesystem walk, exactly as it did before this existed.
    #
    # The `tracks_path` check is not redundant with finding a root. Indexing a
    # library under `.venv/`, or `data/etour/` in this very repository, finds an
    # enclosing repository that deliberately ignores the directory: git would
    # then report no changes there, forever, and the index would go quietly
    # stale. See `vcs.tracks_path`.
    finish = step("git baseline")
    git_top = find_git_root(code_root)
    if git_top is not None and not tracks_path(git_top, code_root):
        git_top = None
    git_state = snapshot(git_top) if git_top is not None else None
    if git_state is None:
        git_top = None
    finish()

    return Corpus(
        requirements=requirements,
        nodes=nodes,
        docs=docs,
        enclosing=enclosing,
        node_documents=node_documents,
        gold=gold,
        req_vectors=req_vectors,
        node_vectors=node_vectors,
        timings=timings,
        code_root=code_root,
        mtimes=mtimes,
        layout=layout,
        git_root=git_top,
        git_state=git_state,
    )

"""Walk the target source repository and yield files to parse.

Deliberately dumb: it finds files, it does not read or parse them. Parsing is
parse/'s job. Keeping the two separate is what made adding six languages a
matter of adding specs rather than rewriting the walk.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from pathlib import Path

from src.parse.languages import SUPPORTED_SUFFIXES

#: Directory names whose contents are skipped.
#:
#: Two groups, for two different reasons:
#:
#: - Test directories: test code inflates the orphan count with nodes no
#:   requirement could ever claim, which makes orphan detection look noisier
#:   than it is.
#: - Build output, dependencies, and VCS metadata: these are not the project's
#:   own code. Indexing `node_modules` would bury a repository's own methods
#:   under a hundred thousand vendored ones, and is the single fastest way to
#:   turn a 0.25ms query into an unusable one.
SKIP_DIRS = frozenset(
    {
        # tests
        "test",
        "tests",
        "__tests__",
        "spec",
        "testdata",
        # build output
        "build",
        "dist",
        "out",
        "target",
        "bin",
        "obj",
        "cmake-build-debug",
        # dependencies / vendored code
        "node_modules",
        "vendor",
        "third_party",
        "site-packages",
        ".venv",
        "venv",
        "env",
        # tooling and VCS
        ".git",
        ".hg",
        ".svn",
        ".idea",
        ".vscode",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
    }
)

#: Files larger than this are skipped.
#:
#: A source file this size is machine-generated -- a parser table, a bundled
#: minified script, an embedded blob. Parsing one costs seconds and contributes
#: a single enormous node whose vector is meaningless. The cap is on bytes
#: rather than lines because it must be decidable from `stat()`, without
#: reading the file.
MAX_FILE_BYTES = 1_000_000


def _git_listing(repo_root: Path) -> tuple[Path, list[Path]] | None:
    """Enumerate via git when `repo_root` is itself a working tree root.

    Returns None to mean "walk the filesystem instead" -- git absent, or the
    directory is merely *inside* a repository rather than being its root.

    THE ROOT EQUALITY CHECK IS LOAD-BEARING. `git_root` walks upward, so asking
    it about `data/etour/code` inside this project answers with *this* project's
    root. Enumerating from there would be doubly wrong: it would list req2code's
    own source instead of eTour's, and `data/` is gitignored so `ls-files` would
    correctly report none of the corpus at all. A corpus checked out inside
    another repository must keep being read as a directory of files.

    When it does apply, the payoff is large and free: `git ls-files` is
    `.gitignore`-aware by construction, so `node_modules`, `target/`, build
    output and vendored trees are excluded according to the repository's own
    rules rather than the guesses in `SKIP_DIRS`.

    Returns `(base, paths)` so the caller can strip the prefix without asking
    the filesystem: the base is git's idea of the root, which may be spelled
    differently from the argument even when it names the same directory.
    """
    from src.ingest.vcs import git_root, list_files

    top = git_root(repo_root)
    if top is None:
        return None
    try:
        if top.resolve() != repo_root.resolve():
            return None
    except OSError:
        return None

    listed = list_files(top)
    if listed is None:
        return None
    return top, [top / rel for rel in listed]


def walk_source_files(
    repo_root: str | Path,
    suffix: str | None = None,
    suffixes: Iterable[str] | None = None,
    max_bytes: int = MAX_FILE_BYTES,
    use_git: bool = True,
) -> Iterator[Path]:
    """Yield every parseable source file under `repo_root`.

    Args:
        repo_root: Root of the code corpus -- `data/etour/code/` for the bundled
            dataset, or an ordinary repository root in workspace mode.
        suffix: A single extension, for callers that want exactly one language.
            Kept for backwards compatibility with the Java-only signature.
        suffixes: Explicit set of extensions. Defaults to every suffix any
            installed `LanguageSpec` claims, so adding a language automatically
            widens the walk with no change here.
        max_bytes: Skip files larger than this. See `MAX_FILE_BYTES`.
        use_git: Allow `git ls-files` to supply the file list when `repo_root`
            is a git working tree root. Set False to force the filesystem walk;
            the two paths must agree on everything except ignored files, and a
            test asserts as much.

    Yields:
        Absolute paths, sorted, so node ids and results are stable across runs
        and across machines.

    The same filters apply to both enumeration strategies. That matters: git
    decides which files *exist* for our purposes, but the size cap, the suffix
    filter and `SKIP_DIRS` still decide which are worth parsing, so a committed
    `vendor/` directory is skipped whether or not git ignores it.
    """
    repo_root = Path(repo_root)
    if not repo_root.is_dir():
        raise FileNotFoundError(
            f"Source directory not found: {repo_root}\n"
            "Fetch the dataset first -- see the README quickstart."
        )

    if suffix is not None:
        suffixes = (suffix,)
    chosen = suffixes if suffixes is not None else SUPPORTED_SUFFIXES
    wanted = {s.lower() for s in chosen}

    listing = _git_listing(repo_root) if use_git else None
    if listing is not None:
        base, candidates = listing[0], sorted(listing[1])
    else:
        base, candidates = repo_root, sorted(repo_root.rglob("*"))

    # `SKIP_DIRS` must be tested against the path *below* the root, never the
    # whole path. In workspace mode the root is absolute and arbitrary, so a
    # project that happens to live in `~/bin/` or `D:\build\` would match on an
    # ancestor directory the user never asked us to judge, and the index would
    # come back mysteriously empty.
    #
    # Sliced lexically rather than via `relative_to`, because both enumeration
    # strategies build their paths from a known prefix and `Path.resolve()`
    # touches the filesystem -- 20k of those is a visible cost for a check that
    # is otherwise pure string work.
    prefix_len = len(base.parts)

    for path in candidates:
        if path.suffix.lower() not in wanted:
            continue
        if any(part.lower() in SKIP_DIRS for part in path.parts[prefix_len:]):
            continue
        if not path.is_file():
            continue
        try:
            if path.stat().st_size > max_bytes:
                continue
        except OSError:
            # A file that vanished between listing and stat-ing, or that we
            # cannot read. Skipping is correct: an index is allowed to be
            # incomplete, but a crawler that dies on one unreadable file in a
            # real repository is not usable.
            continue
        yield path.resolve()

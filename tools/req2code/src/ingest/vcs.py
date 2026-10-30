"""Git as a change-detection oracle. Read-only, offline, no network, ever.

WHY GIT AT ALL
--------------
`Corpus.refresh()` originally answered "what changed?" by stat-ing every source
file. On eTour that is 116 files and ~9ms, comfortably inside the interaction
budget. On a real repository it is not: the walk is O(files in tree) and runs on
every single query, so a 20k-file checkout spends most of its latency budget
asking the filesystem questions whose answer git already knows.

Git knows because it already maintains the index. Three cheap, constant-cost
commands replace the linear walk:

    rev-parse HEAD      what commit are we on
    diff --name-only    what moved between two commits (a pull, a checkout, a
                        rebase -- all of which change thousands of files with no
                        editor event to observe)
    status --porcelain  what is dirty in the working tree right now

That last one is the live-editing path, and the first two are what make
`git checkout other-branch` re-map the whole repository correctly instead of
silently serving a stale index.

GIT IS A CANDIDATE FILTER, NOT THE TRUTH
----------------------------------------
Everything here narrows *which files are worth looking at*. Whether a file
actually needs re-indexing is still decided by mtime in `pipeline.py`, exactly
as before. That layering is deliberate: if git is absent, lies, or reports a
file that has not really changed, the worst outcome is a redundant stat. There
is no path where a git answer alone causes the index to change.

OFFLINE GUARANTEE
-----------------
This project promises no network calls at serve time, and shelling out to git is
the one place that promise could quietly break -- `git fetch` and friends dial
out, and a repo with an unreachable remote can hang for a TCP timeout with an
editor waiting on it.

Three enforcement layers, because a comment is not an enforcement:

  1. `_ALLOWED_SUBCOMMANDS` -- an allowlist checked at call time. A command
     outside it raises rather than runs, so no future edit can add a fetch here
     by accident.
  2. `GIT_TERMINAL_PROMPT=0` and a no-op `GIT_ASKPASS` -- git can never block
     waiting for credentials, which is how "offline" usually fails in practice:
     not with an error but with a hang.
  3. A timeout on every call. A wedged git is a slow query, never a hung editor.

Every function returns `None` rather than raising when git is unavailable or
unhappy. A repository that is not under version control is not an error -- it is
the common case for a folder someone drags in to try the tool -- and the caller
falls back to the filesystem walk.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

#: Seconds before a git call is abandoned. Generous enough for a cold index on a
#: large repository, short enough that a wedged git cannot hold an editor.
GIT_TIMEOUT_SECONDS = 15

#: The only git subcommands this module may run. All four are read-only and
#: none touches a remote. Enforced in `_run`, not merely documented -- see the
#: offline note in the module docstring.
_ALLOWED_SUBCOMMANDS = frozenset({"rev-parse", "ls-files", "status", "diff"})


def _env() -> dict[str, str]:
    """Environment that cannot prompt, cannot lock, and speaks stable English.

    `GIT_OPTIONAL_LOCKS=0` matters more than it looks: without it `git status`
    takes a lock to refresh the index, which fights with the user's own git
    commands and with their editor's git integration. We are a reader; readers
    should not take write locks on someone else's repository.

    `LC_ALL=C` pins the output language. Porcelain formats are stable by
    contract, but error strings and rev-parse messages are not, and this code
    runs on whatever locale the user happens to have.
    """
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_ASKPASS"] = "echo"
    env["LC_ALL"] = "C"
    return env


def _run(cwd: Path, *args: str) -> bytes | None:
    """Run one read-only git command in `cwd`. Returns stdout, or None on failure.

    Bytes rather than text: paths in a real repository are not guaranteed to be
    valid UTF-8, and `-z` output is NUL-separated, so decoding is the caller's
    business once the record boundaries are known.
    """
    if not args or args[0] not in _ALLOWED_SUBCOMMANDS:
        raise ValueError(
            f"Refusing to run git {args[0] if args else '<nothing>'!r}: this "
            "module is read-only and offline. Allowed: "
            f"{sorted(_ALLOWED_SUBCOMMANDS)}."
        )
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            env=_env(),
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        # git not installed, not on PATH, or timed out. All the same to us.
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _split_nul(raw: bytes) -> list[str]:
    """Split NUL-separated git output into decoded, forward-slashed paths."""
    return [
        part.decode("utf-8", errors="replace").replace("\\", "/")
        for part in raw.split(b"\0")
        if part
    ]


def git_root(path: str | Path) -> Path | None:
    """The top level of the working tree containing `path`, or None.

    Note this walks *upward*: a subdirectory of a repository returns the
    repository root, not itself. Callers that care about the difference must
    compare -- see `repo_walker.walk_source_files`, where indexing
    `data/etour/code` inside this very repository must NOT be treated as a git
    enumeration, because that path is gitignored and `ls-files` would correctly
    report nothing.
    """
    path = Path(path)
    if not path.is_dir():
        path = path.parent
    if not path.is_dir():
        return None
    out = _run(path, "rev-parse", "--show-toplevel")
    if out is None:
        return None
    text = out.decode("utf-8", errors="replace").strip()
    return Path(text) if text else None


def head_commit(root: Path) -> str | None:
    """Current HEAD sha, or None in a repository with no commits yet.

    A fresh `git init` has a HEAD that points nowhere. That is a normal state
    for a project someone just started, and it must degrade to "no commit
    baseline", not to an error.
    """
    out = _run(root, "rev-parse", "HEAD")
    if out is None:
        return None
    text = out.decode("ascii", errors="replace").strip()
    return text or None


def list_files(root: Path) -> list[str] | None:
    """Every file git would show you: tracked, plus untracked and not ignored.

    This is the enumeration that makes the walker `.gitignore`-aware for free.
    Writing our own ignore-file parser would mean reimplementing precedence,
    negation, nested `.gitignore` files, `.git/info/exclude` and the global
    excludes file -- and getting any of it subtly wrong means indexing
    `node_modules`, which is the fastest way to make the tool unusable.

    `--others --exclude-standard` is the load-bearing pair: a file the user
    created five seconds ago and has not staged is exactly the file they are
    most likely to ask about.
    """
    out = _run(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    if out is None:
        return None
    return _split_nul(out)


def tracks_path(root: Path, path: Path) -> bool:
    """Whether the repository at `root` actually knows about anything in `path`.

    NOT the same question as "is `path` inside a working tree", and conflating
    the two is a silent-staleness bug rather than a loud one.

    `git_root` walks upward, so a directory that git deliberately ignores still
    answers with the enclosing repository. Two real cases hit this immediately:
    indexing a library under `.venv/` (ignored), and indexing `data/etour/`
    (ignored). Both are *inside* a repository and neither is *tracked by* it, so
    `git status` and `git diff` will forever report nothing about them -- and
    change detection built on those would never fire, with no error to notice.

    One `ls-files` with a pathspec answers it exactly, and only at load time.
    """
    out = _run(
        root,
        "ls-files",
        "-z",
        "--cached",
        "--others",
        "--exclude-standard",
        "--",
        str(path),
    )
    return bool(out and out.strip(b"\0"))


def working_tree_changes(root: Path) -> set[str] | None:
    r"""Paths that differ from HEAD right now -- modified, staged, added, deleted.

    Parsing note: ``--porcelain -z`` emits ``XY <path>\0`` per entry, except that
    a rename or copy emits the *original* path as a second NUL-separated field.
    Consuming that extra field is not optional -- treat it as an entry and every
    path after a single rename is misread as a status code.
    """
    out = _run(root, "status", "--porcelain", "-z", "--untracked-files=all")
    if out is None:
        return None

    fields = out.split(b"\0")
    changed: set[str] = set()
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        changed.add(path.decode("utf-8", errors="replace").replace("\\", "/"))
        if b"R" in status or b"C" in status:
            if i < len(fields) and fields[i]:
                changed.add(
                    fields[i].decode("utf-8", errors="replace").replace("\\", "/")
                )
            i += 1
    return changed


def diff_names(root: Path, old: str, new: str) -> set[str] | None:
    """Files that differ between two commits, or None if either is unreachable.

    Unreachable is a real case, not a defensive one: a rebase, an amend, or a
    `gc` can leave the sha we recorded at load time dangling. None is the signal
    to stop trusting the incremental path and fall back to a full scan.
    """
    out = _run(root, "diff", "--name-only", "-z", old, new)
    if out is None:
        return None
    return set(_split_nul(out))


@dataclass(frozen=True)
class GitState:
    """What the working tree looked like the last time we asked.

    Frozen and value-typed so it can be compared and stored without anyone
    mutating it in place -- the same reasoning as `contracts.py`.
    """

    head: str | None
    dirty: frozenset[str]


def snapshot(root: Path) -> GitState | None:
    """Record HEAD and the dirty set. None if `root` is not a git working tree."""
    dirty = working_tree_changes(root)
    if dirty is None:
        return None
    return GitState(head=head_commit(root), dirty=frozenset(dirty))


def changed_since(root: Path, previous: GitState) -> tuple[set[str], GitState] | None:
    """Candidate changed paths since `previous`, plus the new state.

    Returns None when the incremental answer cannot be trusted (git gone, or a
    commit that no longer exists), which tells the caller to do a full scan.

    THE UNION WITH THE OLD DIRTY SET IS THE SUBTLE PART. A file that was dirty
    last time and is clean now appears in *no* current listing -- not in
    `status`, because it matches HEAD again, and not in `diff`, because HEAD did
    not move. Someone hitting undo, or running `git checkout -- file`, would
    leave the index permanently serving the edited version. Carrying the previous
    dirty set forward is what makes a revert observable.

    Paths are relative to the git root and may point at files we do not index or
    that no longer exist. Filtering is the caller's job; this function's contract
    is "everything that might have moved", not "everything that did".
    """
    current = snapshot(root)
    if current is None:
        return None

    changed: set[str] = set(previous.dirty) | set(current.dirty)

    if previous.head != current.head and previous.head and current.head:
        moved = diff_names(root, previous.head, current.head)
        if moved is None:
            return None
        changed |= moved

    return changed, current

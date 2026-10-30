"""Resolving a cursor position to the node under it.

"What is this method for?" is asked by putting a caret somewhere and pressing a
key. The editor hands over a file path and a line number; this turns that into a
`CodeNode`. Small, but it is the entry point for the whole reverse direction, so
it is worth getting exactly right rather than approximately right.

TWO RULES, BOTH LEARNED THE HARD WAY
------------------------------------
**Innermost wins.** A line inside a method is inside its class too, and both are
indexed. Returning the class would be technically correct and useless -- the
user's caret is on a method and they are asking about the method. Smallest
containing span wins.

**Match on the repo-relative path, not the basename.** The first version of this
compared filenames only. On eTour that is harmless: 116 files, all distinct
names, all in one directory. On a real repository it is a bug that produces a
confidently wrong answer -- three `utils.py`, four `main.go`, a dozen
`__init__.py`, and the caret in one of them resolves to a method in another.

Absolute paths from an editor, relative paths from a CLI, and forward or
backslashes all have to land on the same node, so the comparison is done on a
normalised suffix of the path rather than on equality. Basename-only remains
available as an explicit last resort, because a user typing a bare filename at a
prompt does mean "find the file with this name" -- but it is opt-in, and it
refuses to guess when the name is ambiguous.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from src.contracts import CodeNode


def _normalise(path: str | Path) -> str:
    return str(path).replace("\\", "/").strip("/")


def candidate_nodes(
    nodes: Sequence[CodeNode], file: str | Path, code_root: str | Path | None = None
) -> list[CodeNode]:
    """Every node belonging to `file`, matched as robustly as is safe.

    Tried in order, stopping at the first that yields anything:

      1. Exact match on the repo-relative path, after normalising separators.
         This is the editor's case, once the root has been stripped, and it is
         the only branch that can never be ambiguous.
      2. Suffix match in either direction -- the caller knowing more of the path
         than we stored (`src/app/main.py` against a node at `app/main.py`, when
         `code_root` is a subdirectory), or less (a bare `main.py` typed at a
         prompt). This branch **refuses when it is ambiguous**: if the match
         spans more than one file, nothing is returned. A confident wrong answer
         about which `utils.py` the user meant is worse than no answer, because
         nothing downstream can tell that it was a guess.
    """
    wanted = _normalise(file)
    if code_root is not None:
        try:
            wanted = _normalise(
                Path(file).resolve().relative_to(Path(code_root).resolve())
            )
        except (OSError, ValueError):
            pass

    exact = [n for n in nodes if _normalise(n.file_path) == wanted]
    if exact:
        return exact

    suffix = [
        n
        for n in nodes
        if wanted.endswith("/" + _normalise(n.file_path))
        or _normalise(n.file_path).endswith("/" + wanted)
    ]
    if len({n.file_path for n in suffix}) == 1:
        return suffix
    return []


def find_node_at(
    nodes: Sequence[CodeNode],
    file: str | Path,
    line: int,
    code_root: str | Path | None = None,
    kinds: tuple[str, ...] | None = None,
) -> CodeNode | None:
    """The innermost node whose span contains `line` in `file`, or None.

    Args:
        nodes: The corpus.
        file: Path as the caller has it -- absolute, relative, either separator.
        line: 1-based, as every editor counts.
        code_root: Root the corpus paths are relative to, when known. Supplying
            it turns an editor's absolute path into an exact match instead of a
            suffix guess.
        kinds: Restrict to these node kinds. Default is any kind, so a caret on
            a class declaration line resolves to the class rather than to
            nothing.

    Returns None for a line in an unparsed file, in an import block, or between
    declarations. That is a normal answer, not an error -- most lines of most
    files belong to no node.
    """
    candidates = [
        n
        for n in candidate_nodes(nodes, file, code_root)
        if n.start_line <= line <= n.end_line and (kinds is None or n.kind in kinds)
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda n: (n.end_line - n.start_line, n.start_line))

"""Deciding what kind of thing the user pointed us at.

`load_corpus` used to assume one shape: `<dir>/req/*.txt` alongside
`<dir>/code/**`. That is the eTour packaging, and it is the reason the tool
could not be run on anything else -- point it at a real project and
`load_requirements` raises `FileNotFoundError` before a single file is parsed.

There are two shapes to support, and they are told apart by looking, not by a
flag:

**dataset** -- `req/` and `code/` both present. A research corpus, possibly with
an answer key. Requirements are files on disk; the gold links, if any, enable
evaluation. This is eTour and anything packaged like it.

**workspace** -- anything else. The directory *is* the code. There are no
requirement documents and no answer key; requirements come from the session log
(`ingest/requirement_store.py`) and accumulate as the user states them.

WHY SNIFFING RATHER THAN A --mode FLAG
--------------------------------------
The tool is going to be launched by an editor, with the workspace root as its
only argument. There is nobody there to pass a flag. Detection has to work from
the directory alone or the editor integration needs configuration before it can
do anything, which is exactly the friction that stops people trying a tool.

The sniff is also conservative in the direction that matters: it takes *both*
`req/` and `code/` to be read as a dataset. A project that happens to have a
`code/` directory is still a workspace, so the failure mode of a false positive
-- silently indexing a subdirectory and reporting an empty requirement set --
cannot happen by accident.

WHAT THIS DELIBERATELY DOES NOT DO
----------------------------------
The session store is enabled for workspaces only, never for datasets. Letting
someone append live requirements to eTour would mean the published ablation
numbers depended on local state that is not in the repository, and the whole
point of `docs/results/` snapshots is that a number can be traced to a committed
input. A research corpus is read-only by construction.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from src.ingest.requirement_store import store_path

#: The eTour answer key. Named here rather than in `pipeline.py` because layout
#: detection is what decides whether an answer key is even applicable.
GOLD_FILENAME = "etour_solution_links_english.txt"

LayoutKind = Literal["dataset", "workspace"]


@dataclass(frozen=True)
class Layout:
    """Where everything lives for one target directory.

    Frozen for the same reason as the contracts: it is a fact derived from the
    filesystem at load time, not a scratch variable. Anything that wants a
    different layout re-runs `discover`.

    Attributes:
        root: What the user pointed at.
        code_root: Directory to walk for source. Equal to `root` for a
            workspace; `root/code` for a dataset.
        requirements_dir: Directory of requirement documents, or None when
            requirements come from the session store instead.
        gold_path: Where an answer key *would* be, for a dataset; None for a
            workspace, where the concept does not apply. Note this is the
            expected location whether or not the file exists -- `load_gold_links`
            is what decides, because its `strict` mode must be able to raise on
            an answer key that should be there and is not. Ask `has_gold`
            whether one is actually present. Evaluation-only either way; nothing
            in retrieval reads it.
        kind: See `LayoutKind`.
    """

    root: Path
    code_root: Path
    requirements_dir: Path | None
    gold_path: Path | None
    kind: LayoutKind

    @property
    def store_path(self) -> Path | None:
        """The session requirement log, or None for a dataset.

        None is the enforcement of the read-only-corpus rule in the module
        docstring: `Corpus.add_requirement` refuses when this is None, so the
        rule lives in the type rather than in a convention someone can forget.
        """
        return store_path(self.root) if self.kind == "workspace" else None

    @property
    def has_gold(self) -> bool:
        """Whether an answer key is actually on disk, as opposed to expected."""
        return self.gold_path is not None and self.gold_path.is_file()


def discover(root: str | Path, gold_filename: str = GOLD_FILENAME) -> Layout:
    """Work out how to read `root`.

    Args:
        root: A research corpus directory or an ordinary source repository.
        gold_filename: Answer-key filename to look for. Parameterised rather
            than hard-coded so a second labelled corpus can be added without
            touching this module.

    Returns:
        A `Layout`. Never raises for a merely unusual directory -- an empty
        folder is a valid workspace with nothing in it, and reporting "0 nodes"
        is more useful than an exception.

    Raises:
        FileNotFoundError: if `root` does not exist or is not a directory. That
            one is a genuine user error (a typo in a path) and guessing past it
            would produce a confusing empty index instead of a clear message.
    """
    root = Path(root)
    if not root.is_dir():
        raise FileNotFoundError(
            f"Not a directory: {root}\n"
            "Point req2code at a source repository, or at a corpus directory "
            "containing req/ and code/."
        )

    req_dir = root / "req"
    code_dir = root / "code"

    if req_dir.is_dir() and code_dir.is_dir():
        return Layout(
            root=root,
            code_root=code_dir,
            requirements_dir=req_dir,
            gold_path=root / gold_filename,
            kind="dataset",
        )

    return Layout(
        root=root,
        code_root=root,
        requirements_dir=None,
        gold_path=None,
        kind="workspace",
    )

"""The session requirement log -- where requirements come from on a real repo.

THE REFRAME THIS IMPLEMENTS
---------------------------
Until now a requirement was a file in `data/etour/req/`, read off disk at load
time. That is an artefact of evaluating on a research corpus, and it does not
survive contact with a real project: nobody has a folder of numbered use-case
documents sitting next to their source.

On a real repository, requirements are *stated live*:

    user types "the system shall notify a traveller about nearby attractions"
        -> ranked methods that implement it

Every requirement stated that way is appended here. The tool therefore accretes
its own requirement corpus as it is used, starting from nothing.

WHY THAT MATTERS BEYOND CONVENIENCE
-----------------------------------
Two of the three headline capabilities need a *set* of requirements, not one
query. Code-to-requirement ranks a method against all known requirements, and
orphan detection flags methods that no requirement claims. On a fresh repository
both are undefined, because the set is empty.

The store is what makes them defined -- and it sharpens the definition. "Orphan"
stops meaning "unclaimed by the corpus we shipped" and starts meaning *code that
nothing you have ever asked for touches*, which is the more honest claim anyway:
it is scoped to what this user has actually specified, and it gets sharper the
more they use the tool rather than being fixed at packaging time.

FORMAT: JSONL, APPEND-ONLY
--------------------------
One JSON object per line, at `<repo>/.req2code/requirements.jsonl`.

Append-only and line-delimited is chosen over a single JSON document for one
reason: a partial write cannot corrupt earlier records. An editor process, a CLI
invocation and an MCP server may all have this file open, and a crash mid-append
costs at worst the last line -- which `read_records` skips -- rather than the
whole log. A rewritten JSON array would risk all of it.

The shape is frozen by the same rule as `src/contracts.py`: both authors must
agree before it changes, because one half of the project writes it and the other
half evaluates against it.

    {"v": 1, "req_id": "SR0001", "text": "...", "created_at": "...",
     "source": "session"}

`v` is present from the first record so a future format change can be detected
rather than guessed at. Unknown fields are preserved on rewrite and ignored on
read, so adding one later is not a breaking change.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

from src.contracts import Requirement

#: Directory holding all per-repository state this tool creates. One directory,
#: at the repo root, so a user can delete the tool's entire footprint with one
#: `rm -rf` and be sure nothing is left behind.
STORE_DIRNAME = ".req2code"

STORE_FILENAME = "requirements.jsonl"

#: Current record schema version. See the module docstring.
RECORD_VERSION = 1

#: Prefix for generated ids. Deliberately not "UC": a session requirement is not
#: a use case from a specification document, and an id that could be confused
#: with a corpus id would make gold-linked and user-stated requirements
#: indistinguishable in output.
REQ_ID_PREFIX = "SR"

_ID_RE = re.compile(rf"^{REQ_ID_PREFIX}(\d+)$")


def store_dir(root: str | Path) -> Path:
    return Path(root) / STORE_DIRNAME


def store_path(root: str | Path) -> Path:
    return store_dir(root) / STORE_FILENAME


def _normalise(text: str) -> str:
    """Collapse whitespace for duplicate detection.

    Two statements of the same requirement that differ only in line wrapping are
    the same requirement. Without this, re-stating one after an editor reflow
    silently doubles its weight in orphan scoring, because the requirement set
    would contain it twice.
    """
    return " ".join(text.split()).casefold()


def read_raw(root: str | Path) -> list[dict]:
    """Every well-formed line in the log, in file order, tombstones included.

    A line that does not parse is skipped rather than fatal. That is the whole
    reason for choosing JSONL: a truncated final line from an interrupted write
    costs one requirement, not the log.

    Callers almost always want `read_records` instead. This exists because id
    allocation has to see deletions -- see `next_req_id`.
    """
    path = store_path(root)
    if not path.is_file():
        return []

    records: list[dict] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and record.get("req_id"):
                records.append(record)
    return records


def read_records(root: str | Path) -> list[dict]:
    """The live requirements: everything stated and not since deleted.

    A deletion is a tombstone line rather than a rewrite of the file, so the log
    stays strictly append-only -- see `remove_requirement`. Tombstones are
    resolved here, which is why every reader except `next_req_id` can ignore
    that they exist.
    """
    live: dict[str, dict] = {}
    for record in read_raw(root):
        req_id = str(record["req_id"])
        if record.get("deleted"):
            live.pop(req_id, None)
        elif "text" in record:
            live[req_id] = record
    return list(live.values())


def load_requirements(root: str | Path) -> list[Requirement]:
    """The log as `Requirement` objects, ready for the same pipeline as a corpus.

    Deliberately returns the frozen contract type rather than a store-specific
    one. Everything downstream -- embedding, retrieval, orphans, justification --
    must not be able to tell whether a requirement came from a research corpus
    or from something the user typed a minute ago. That indistinguishability is
    what lets the evaluation harness make claims about the live product.

    Order is insertion order, not sorted, because ids are already monotonic and
    a stated requirement's position in the log is meaningful history.
    """
    path = str(store_path(root))
    return [
        Requirement(
            req_id=str(record["req_id"]),
            text=str(record["text"]).strip(),
            source_path=path,
        )
        for record in read_records(root)
    ]


def next_req_id(records: list[dict]) -> str:
    """Next free id, one past the highest number the log has *ever* used.

    Args:
        records: Raw log lines from `read_raw`, not the live set. Passing the
            live set would reuse the id of a deleted requirement, and a stale
            reference that quietly resolves to a different requirement is a
            worse failure than one that fails to resolve at all.

    Derived from the file rather than from a counter so that two processes
    appending independently produce a detectable collision (two records with one
    id, resolved by `read_records` keeping the last) instead of silently
    interleaving. At the rate a human states requirements, that is the right
    trade against the complexity of a lock.
    """
    highest = 0
    for record in records:
        match = _ID_RE.match(str(record.get("req_id", "")))
        if match:
            highest = max(highest, int(match.group(1)))
    return f"{REQ_ID_PREFIX}{highest + 1:04d}"


def append_requirement(
    root: str | Path, text: str, source: str = "session"
) -> tuple[Requirement, bool]:
    """Add a requirement to the log, or return the existing identical one.

    Args:
        root: Repository root. The store lives at `<root>/.req2code/`.
        text: The requirement as the user stated it.
        source: Provenance tag. "session" for something typed into the editor;
            callers importing from elsewhere should say so, because it is the
            field that lets the evaluation distinguish stated requirements from
            bulk-imported ones.

    Returns:
        `(requirement, created)`. `created` is False when an identical
        requirement was already present, in which case the existing record is
        returned unchanged -- re-stating a requirement is idempotent, which
        matters because the natural way to refine a query is to type it again.

    Raises:
        ValueError: if `text` is blank. A requirement with no content would
            embed to a meaningless vector and pollute every orphan score.
    """
    text = text.strip()
    if not text:
        raise ValueError("A requirement must have text.")

    records = read_records(root)
    target = _normalise(text)
    for record in records:
        if _normalise(str(record["text"])) == target:
            return (
                Requirement(
                    req_id=str(record["req_id"]),
                    text=str(record["text"]).strip(),
                    source_path=str(store_path(root)),
                ),
                False,
            )

    record = {
        "v": RECORD_VERSION,
        "req_id": next_req_id(read_raw(root)),
        "text": text,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "source": source,
    }
    _append(root, record)
    return (
        Requirement(
            req_id=record["req_id"], text=text, source_path=str(store_path(root))
        ),
        True,
    )


def _append(root: str | Path, record: dict) -> None:
    path = store_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def remove_requirement(root: str | Path, req_id: str) -> bool:
    """Drop one requirement. Returns whether it was there.

    Implemented as a tombstone line rather than a rewrite, which keeps the log
    strictly append-only. A rewrite would be the obvious implementation and it
    is the wrong one here: another process may hold the file open, and an
    interrupted rewrite is the one failure that could lose requirements the user
    typed. Appending cannot destroy an earlier line by construction, so there is
    no window to lose them in.

    It also keeps ids monotonic. `next_req_id` reads the raw log, tombstones
    included, so a removed id is never handed out again -- a stale reference to
    a deleted requirement fails to resolve rather than quietly resolving to a
    different one.

    The cost is a log that only grows. At one line per stated requirement that
    is not a real cost; if it ever becomes one, compaction is a separate
    operation that can take a lock, rather than something every delete pays for.
    """
    if not any(str(r.get("req_id")) == req_id for r in read_records(root)):
        return False
    _append(
        root,
        {
            "v": RECORD_VERSION,
            "req_id": req_id,
            "deleted": True,
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        },
    )
    return True

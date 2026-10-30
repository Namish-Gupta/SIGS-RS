"""The JSON shapes every front end speaks. One definition, three consumers.

There are now three ways to reach the engine -- the CLI (`--json`), the MCP
server (`structuredContent`), and the VS Code extension (which talks to the MCP
server) -- and they must agree about what a "hit" is. If each built its own dict
they would drift, and the drift would show up as a field the extension reads and
the CLI stopped emitting, at the worst possible moment.

So the payloads live here and nowhere else. `src/cli.py` and
`scripts/mcp_server.py` are both thin renderers over these functions.

This is the same reasoning as `src/contracts.py`, one layer out: contracts.py
freezes what a `CodeNode` *is*, this freezes what a `CodeNode` *looks like on the
wire*. It is not frozen by the two-signature rule -- it is an internal interface
and it will change as the extension grows -- but it is the single place to
change it.

CONVENTIONS
-----------
- Every score is a float rounded to 4 decimal places. Raw numpy floats are not
  JSON-serialisable and full precision is noise no reader can use.
- Line numbers are 1-based and inclusive, as every editor counts. The extension
  converts to 0-based once, at the boundary.
- File paths are repo-relative with forward slashes, always. The extension joins
  them against the workspace root; nothing downstream should ever see a
  backslash or an absolute path.
"""

from __future__ import annotations

from typing import Any

from src.contracts import CodeNode


def hit(corpus, node: CodeNode, score: float, rank: int) -> dict[str, Any]:
    """One retrieved code node.

    `enclosing` is separate from `name` rather than pre-joined because the UI
    wants to style them differently -- the class dimmed, the method not -- and a
    pre-joined string cannot be taken apart reliably once an anonymous class or
    a free function is in play.
    """
    return {
        "rank": rank,
        "score": round(float(score), 4),
        "node_id": node.node_id,
        "name": node.name,
        "enclosing": corpus.enclosing.get(node.node_id, ""),
        "kind": node.kind,
        "file": node.file_path,
        "start_line": node.start_line,
        "end_line": node.end_line,
    }


#: Longest requirement title a list row will show. Past this the row wraps to a
#: third line and the list stops being scannable.
TITLE_CHARS = 120


def _title(text: str) -> str:
    """First non-blank line, trimmed to `TITLE_CHARS` on a word boundary.

    Cutting mid-word produces "using version control where availabl", which
    reads as corruption rather than as elision -- so back up to the last space
    and mark it with an ellipsis. The full text always travels alongside in
    `text`, so nothing is lost, only shortened.
    """
    first = next((ln for ln in text.splitlines() if ln.strip()), "").strip()
    if len(first) <= TITLE_CHARS:
        return first
    clipped = first[:TITLE_CHARS]
    space = clipped.rfind(" ")
    if space > TITLE_CHARS // 2:
        clipped = clipped[:space]
    return clipped.rstrip(" ,;:.") + "…"


def requirement(req, score: float | None = None, rank: int | None = None) -> dict:
    """One requirement, with the first non-blank line pulled out as a title.

    The title exists because requirement text is a paragraph and a list row is
    one line. Doing the truncation here rather than in each front end means the
    CLI and the extension elide at the same place.
    """
    out: dict[str, Any] = {
        "req_id": req.req_id,
        "title": _title(req.text),
        "text": req.text,
    }
    if score is not None:
        out["score"] = round(float(score), 4)
    if rank is not None:
        out["rank"] = rank
    return out


def status(corpus) -> dict[str, Any]:
    """Everything the status panel shows, in one call.

    Includes the requirement list: it is small, the panel always wants it, and
    one round trip beats two. If a project ever accumulates enough requirements
    for that to hurt, paginating is a change here and nowhere else.
    """
    from src.parse.languages import spec_for_path

    languages: dict[str, int] = {}
    for node in corpus.nodes:
        spec = spec_for_path(node.file_path)
        key = spec.name if spec is not None else "unknown"
        languages[key] = languages.get(key, 0) + 1

    layout = corpus.layout
    return {
        "root": str(layout.root if layout else corpus.code_root),
        "mode": layout.kind if layout else "unknown",
        "read_only": bool(layout and layout.store_path is None),
        "files": len(corpus.mtimes),
        "nodes": len(corpus.nodes),
        "methods": sum(1 for n in corpus.nodes if n.kind == "method"),
        "languages": dict(sorted(languages.items(), key=lambda kv: -kv[1])),
        "gold_links": len(corpus.gold),
        "git": {
            "tracked": corpus.git_root is not None,
            "head": corpus.git_state.head if corpus.git_state else None,
            "dirty": len(corpus.git_state.dirty) if corpus.git_state else 0,
        },
        "requirements": [requirement(r) for r in corpus.requirements],
        "timings": {k: round(v, 3) for k, v in corpus.timings.items()},
    }


def trace(corpus, hits, *, query: str = "", req=None, created: bool | None = None):
    """A ranked requirement-to-code result.

    Covers all three ways of asking -- a throwaway search, a newly stated
    requirement, and a re-trace of a stored one -- because the *answer* is the
    same shape in every case and only the provenance differs. The extension
    renders one list regardless; `req_id` being null is how it knows the query
    was not logged.
    """
    gold = corpus.gold.get(req.req_id, set()) if req is not None else set()
    rows = []
    for rank, (i, score) in enumerate(hits, start=1):
        node = corpus.nodes[i]
        row = hit(corpus, node, score, rank)
        if gold:
            row["gold"] = node.file_path in gold
        rows.append(row)
    return {
        "query": req.text if req is not None else query,
        "req_id": req.req_id if req is not None else None,
        "created": created,
        "hits": rows,
    }


def orphans(corpus, threshold: float, limit: int) -> dict[str, Any]:
    """Unclaimed code, plus the distribution the threshold should be read against.

    The distribution ships with the list on purpose. A bare list invites reading
    the cutoff as a result; the percentiles are what make "0.30 is untuned"
    visible in the UI rather than buried in a docstring.
    """
    from src.retrieve.orphans import best_requirement_score, score_distribution
    from src.retrieve.orphans import find_orphans as _find

    methods = sum(1 for n in corpus.nodes if n.kind == "method")
    if not methods or not corpus.requirements:
        return {
            "threshold": threshold,
            "methods": methods,
            "flagged": 0,
            "distribution": {},
            "orphans": [],
            "reason": (
                "no methods indexed"
                if not methods
                else "no requirements stated yet -- every method would be flagged"
            ),
        }

    dist = score_distribution(
        best_requirement_score(corpus.node_vectors, corpus.req_vectors)
    )
    found = _find(
        corpus.nodes, corpus.node_vectors, corpus.req_vectors, threshold=threshold
    )
    return {
        "threshold": threshold,
        "methods": methods,
        "flagged": len(found),
        "distribution": {k: round(v, 4) for k, v in dist.items()},
        "orphans": [
            hit(corpus, node, score, rank)
            for rank, (node, score) in enumerate(found[:limit], start=1)
        ],
        "reason": None,
    }


def file_map(corpus, file: str, top_k: int = 3) -> dict[str, Any]:
    """Every declaration in one file, each with the requirements that claim it.

    ONE CALL PER FILE, NOT ONE PER METHOD. This exists because of CodeLens: a
    600-line file has thirty methods and each needs a label, so the per-cursor
    `whose_requirement` shape would mean thirty round trips every time the user
    scrolls. Here it is one matrix multiply against the whole file's rows.

    Returns nodes in source order, which is the order the editor draws them.
    """
    from src.index.vector_store import similarity_matrix
    from src.retrieve.locate import candidate_nodes

    nodes = candidate_nodes(corpus.nodes, file, corpus.code_root)
    if not nodes or not corpus.requirements:
        return {"file": file, "nodes": [], "requirements": len(corpus.requirements)}

    index = corpus.node_index()
    rows = [index[n.node_id] for n in nodes]
    scores = similarity_matrix(corpus.node_vectors[rows], corpus.req_vectors)

    out = []
    for offset, node in enumerate(nodes):
        row = scores[offset]
        order = row.argsort()[::-1][:top_k]
        out.append(
            {
                "node_id": node.node_id,
                "name": node.name,
                "enclosing": corpus.enclosing.get(node.node_id, ""),
                "kind": node.kind,
                "start_line": node.start_line,
                "end_line": node.end_line,
                "best_score": round(float(row.max()), 4) if row.size else 0.0,
                "requirements": [
                    requirement(
                        corpus.requirements[j], score=row[j], rank=rank
                    )
                    for rank, j in enumerate(order, start=1)
                ],
            }
        )
    out.sort(key=lambda n: n["start_line"])
    return {
        "file": nodes[0].file_path,
        "nodes": out,
        "requirements": len(corpus.requirements),
    }

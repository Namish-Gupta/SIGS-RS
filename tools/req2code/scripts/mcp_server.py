"""MCP server -- exposes req2code's retrieval to any MCP-speaking editor.

    python -m scripts.mcp_server

One server reaches every AI editor that speaks the Model Context Protocol:
Claude Code, Cursor, Zed, Windsurf, Claude Desktop. That is why this exists
rather than a VSCode extension -- an extension would cover the VSCode forks and
leave Zed out, and would need a second language and a publishing pipeline to do
it. This is ~150 lines of the Python that is already here.

WHY A LONG-LIVED PROCESS. Startup is ~3.5s, ~92% of it loading the
sentence-transformer. Shelling out per query would pay that every time and the
tool would be unusable. Paying it once at boot -- behind the editor's own
startup -- puts every subsequent query at ~0.25ms. The transport choice is what
makes the latency budget work; see scripts/bench_latency.py for the numbers.

STALENESS. Every tool calls `corpus.refresh()` first, which stats the source
tree (~9ms) and re-embeds only the files whose mtime moved (~45ms per genuinely
edited file; a touched-but-unchanged file costs nothing extra, because the
per-node embedding cache is keyed on content). There is no filesystem watcher
and no background thread: polling on the read path cannot miss an event, and at
this corpus size it costs less than the query it precedes.

Register it with your editor by pointing at this module. The config key differs
per editor (`.mcp.json` for Claude Code, `.cursor/mcp.json` for Cursor,
`context_servers` in Zed's settings) but the shape is the same:

    {"command": ".venv/Scripts/python", "args": ["-m", "scripts.mcp_server"]}
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Pin HuggingFace offline before sentence-transformers is imported: a flaky
# network or an HF outage must not be able to stall an editor's startup. Guarded
# on the model already being downloaded, so a first run still works.
from src.index.embedder import pin_offline_if_cached  # noqa: E402
from src.pipeline import DEFAULT_DATA_DIR, load_corpus  # noqa: E402

pin_offline_if_cached()

try:
    from mcp.server import MCPServer  # noqa: E402
except ModuleNotFoundError:  # pragma: no cover - depends on optional install
    sys.exit(
        "The MCP SDK is not installed. It is optional -- the demo and ablation\n"
        "do not need it -- so install it only to run this server:\n\n"
        "    python -m pip install mcp\n"
    )

server = MCPServer(
    name="req2code",
    version="0.2.0",
    instructions=(
        "Requirement-to-code traceability over this workspace, in seven "
        "languages. Use these tools to answer which code implements which "
        "requirement, what a given method exists for, and which code no "
        "requirement claims. Always prefer them over guessing from the source "
        "alone: they rank by embedding similarity over the whole repository, "
        "which you cannot do by reading files. Requirements are stated by the "
        "user rather than read from a folder -- when the user describes "
        "something the system should do, record it with state_requirement."
    ),
)

#: Directory to serve. Two shapes are accepted and told apart by looking (see
#: `ingest/workspace.py`): a research corpus with `req/` and `code/`, or -- the
#: normal case -- an ordinary repository root, where requirements are stated
#: live and accumulate in `.req2code/requirements.jsonl` instead of being read
#: from a folder.
#:
#: `REQ2CODE_ROOT` is the name to use; `REQ2CODE_DATA_DIR` is kept working
#: because it is what the README and the existing editor configs say. Set via
#: the `env` block every editor's MCP config already supports, so pointing the
#: server at a project needs no code change and no CLI parsing.
DATA_DIR = Path(
    os.environ.get(
        "REQ2CODE_ROOT", os.environ.get("REQ2CODE_DATA_DIR", DEFAULT_DATA_DIR)
    )
)

_corpus = None


def corpus():
    """The loaded corpus, refreshed against on-disk changes."""
    global _corpus
    if _corpus is None:
        _corpus = load_corpus(DATA_DIR)
    _corpus.refresh()
    return _corpus


def _label(c, node) -> str:
    enclosing = c.enclosing.get(node.node_id, "")
    name = f"{enclosing}.{node.name}()" if enclosing else f"{node.name}()"
    return f"{name}  {node.file_path}:{node.start_line}-{node.end_line}"


def _find_node(c, file: str, line: int):
    """Innermost node containing `line` in `file`, or None.

    Delegates to `retrieve/locate.py`. This used to match on the basename, which
    is safe on eTour's 116 uniquely-named files and wrong on any real repository
    -- see that module for why.
    """
    from src.retrieve.locate import find_node_at

    return find_node_at(c.nodes, file, line, code_root=c.code_root)


@server.tool(
    description=(
        "Given a requirement id (e.g. 'UC20.txt'), return the code methods most "
        "likely to implement it, ranked, with exact file and line range. Call "
        "this when asked where a requirement is implemented or what code a "
        "requirement change would affect."
    )
)
def trace_requirement(req_id: str, top_k: int = 10) -> str:
    from src.index.vector_store import search

    c = corpus()
    index = {r.req_id: i for i, r in enumerate(c.requirements)}
    if req_id not in index:
        return f"Unknown requirement {req_id!r}. Known: {', '.join(sorted(index))}"

    hits = search(c.req_vectors[index[req_id]], c.node_vectors, top_k=top_k)
    gold = c.gold.get(req_id, set())
    lines = [f"{req_id} -> top {len(hits)} methods:"]
    for rank, (i, score) in enumerate(hits, start=1):
        node = c.nodes[i]
        mark = "  [gold]" if node.file_path in gold else ""
        lines.append(f"{rank:>3}. {score:.3f}  {_label(c, node)}{mark}")
    return "\n".join(lines)


@server.tool(
    description=(
        "Search the codebase by meaning using plain English, not keywords. "
        "Returns ranked methods with file and line range. Use this when the user "
        "describes desired behaviour rather than naming a requirement id -- it "
        "matches 'notify the user' to sendAlert() even with no shared words."
    )
)
def search_code(query: str, top_k: int = 10) -> str:
    from src.index.embedder import embed_texts
    from src.index.vector_store import search

    c = corpus()
    vec = embed_texts([query])[0]
    hits = search(vec, c.node_vectors, top_k=top_k)
    lines = [f"{query!r} -> top {len(hits)} methods:"]
    for rank, (i, score) in enumerate(hits, start=1):
        lines.append(f"{rank:>3}. {score:.3f}  {_label(c, c.nodes[i])}")
    return "\n".join(lines)


@server.tool(
    description=(
        "Record a requirement the user has just stated in plain English, and "
        "return the code that appears to implement it. Use this -- rather than "
        "search_code -- whenever the user is describing something the system is "
        "supposed to do, because it also adds the requirement to this project's "
        "set, which is what makes whose_requirement and find_orphans meaningful. "
        "Idempotent: re-stating the same requirement returns the existing one."
    )
)
def state_requirement(text: str, top_k: int = 10) -> str:
    from src.index.vector_store import search

    c = corpus()
    try:
        requirement, created = c.add_requirement(text)
    except (RuntimeError, ValueError) as exc:
        return str(exc)

    row = {r.req_id: i for i, r in enumerate(c.requirements)}[requirement.req_id]
    hits = search(c.req_vectors[row], c.node_vectors, top_k=top_k)

    note = "recorded" if created else "already recorded"
    lines = [f"{requirement.req_id} ({note}) -> top {len(hits)} methods:"]
    for rank, (i, score) in enumerate(hits, start=1):
        lines.append(f"{rank:>3}. {score:.3f}  {_label(c, c.nodes[i])}")
    if not hits:
        lines.append("  (nothing indexed -- no parseable source found)")
    return "\n".join(lines)


@server.tool(
    description=(
        "List the requirements stated so far for this project, with their ids. "
        "Use before trace_requirement, which needs an id."
    )
)
def list_requirements() -> str:
    c = corpus()
    if not c.requirements:
        return (
            "No requirements yet. State one with state_requirement -- on a normal "
            "repository the requirement set is built by asking, not read from a "
            "folder."
        )
    lines = [f"{len(c.requirements)} requirement(s):"]
    for req in c.requirements:
        first = next((ln for ln in req.text.splitlines() if ln.strip()), "")
        lines.append(f"  {req.req_id}  {first.strip()[:90]}")
    return "\n".join(lines)


@server.tool(
    description=(
        "Given a file and a line number, identify the method at that location and "
        "return the requirements that most plausibly justify its existence. Call "
        "this to answer 'what is this method for?' or 'did anyone ask for this?'"
    )
)
def whose_requirement(file: str, line: int, top_k: int = 5) -> str:
    from src.index.vector_store import search

    c = corpus()
    node = _find_node(c, file, line)
    if node is None:
        return f"No parsed method covers {file}:{line}."

    row = c.node_index()[node.node_id]
    hits = search(c.node_vectors[row], c.req_vectors, top_k=top_k)
    best = hits[0][1] if hits else 0.0
    lines = [f"{_label(c, node)}", ""]
    for rank, (i, score) in enumerate(hits, start=1):
        req = c.requirements[i]
        first = next((ln for ln in req.text.splitlines() if ln.strip()), "")
        lines.append(f"{rank:>3}. {score:.3f}  {req.req_id}  {first.strip()[:70]}")
    if best < 0.30:
        lines.append(
            f"\nBest match is only {best:.3f} -- this method may be an orphan "
            "(infrastructure or dead code that no requirement asks for)."
        )
    return "\n".join(lines)


@server.tool(
    description=(
        "List methods that no requirement appears to claim -- candidate dead code, "
        "undocumented features, or plumbing. Returns the most-orphaned first. "
        "The threshold is uncalibrated; the ranked order is the reliable signal."
    )
)
def find_orphans(threshold: float = 0.30, limit: int = 20) -> str:
    from src.retrieve.orphans import (
        best_requirement_score,
        score_distribution,
    )
    from src.retrieve.orphans import (
        find_orphans as _find,
    )

    c = corpus()
    n_methods = sum(1 for n in c.nodes if n.kind == "method")
    if not n_methods:
        return "No methods indexed -- nothing here parses as source we support."
    if not c.requirements:
        return (
            "No requirements have been stated yet, so every method would be "
            "flagged -- true, and useless. State a requirement first with "
            "state_requirement, then ask again."
        )

    dist = score_distribution(best_requirement_score(c.node_vectors, c.req_vectors))
    orphans = _find(c.nodes, c.node_vectors, c.req_vectors, threshold=threshold)

    lines = [
        f"{len(orphans)} of {n_methods} methods ({len(orphans) / n_methods:.0%}) "
        f"score below {threshold:.2f}.",
        f"Distribution: p5={dist['p5']:.3f} median={dist['median']:.3f} "
        f"max={dist['max']:.3f}. The 5th percentile ({dist['p5']:.2f}) flags a "
        "reviewable set; 0.30 is an untuned default.",
        "",
    ]
    for node, score in orphans[:limit]:
        lines.append(f"  {score:.3f}  {_label(c, node)}")
    return "\n".join(lines)


@server.tool(
    description=(
        "Return the cached natural-language argument for why a specific method "
        "satisfies a specific requirement, grounded in identifiers in the code. "
        "Use after trace_requirement when the user asks why a link holds."
    )
)
def justify_link(req_id: str, node_id: str) -> str:
    from src.justify.justifier import get_justification

    c = corpus()
    req = next((r for r in c.requirements if r.req_id == req_id), None)
    node = next((n for n in c.nodes if n.node_id == node_id), None)
    if req is None:
        return f"Unknown requirement {req_id!r}."
    if node is None:
        return f"Unknown node {node_id!r}."

    text = get_justification(req, node)
    if not text:
        return (
            f"No cached justification for {req_id} -> {node_id}. Justifications "
            "are pre-generated offline; regenerate with "
            "`python -m scripts.generate_justifications`."
        )
    return text


# ---------------------------------------------------------------------------
# UI tools
#
# The tools above return prose, because their reader is a language model. These
# return data, because their reader is the VS Code extension, which draws a list
# and needs to know where the numbers end and the identifiers begin.
#
# The MCP SDK puts a dict return into `structuredContent` *and* renders it as
# text, so one tool serves both without a second server or a second protocol.
# Every shape here comes from `src/api.py`, which is also what the CLI's --json
# emits -- that is what stops the two from drifting.
#
# They are marked "UI tool" in their descriptions so a model reading the tool
# list routes to the prose versions instead. There is no way to hide a tool from
# one client and not another, so the steering has to live in the description.
# ---------------------------------------------------------------------------


@server.tool(
    description=(
        "UI tool for the req2code VS Code extension -- prefer list_requirements. "
        "Returns index size, language breakdown, git state, and every stated "
        "requirement, as structured data."
    )
)
def ui_status() -> dict:
    from src import api

    return api.status(corpus())


@server.tool(
    description=(
        "UI tool for the req2code VS Code extension -- prefer state_requirement "
        "or search_code. Ranks code against a plain-English requirement and "
        "returns structured hits. Set log=true to record the requirement in the "
        "project's set (making it count for orphans and reverse tracing); "
        "log=false is a throwaway query."
    )
)
def ui_trace(text: str, top_k: int = 20, log: bool = True) -> dict:
    from src import api
    from src.index.embedder import embed_texts
    from src.index.vector_store import search

    c = corpus()
    if not c.nodes:
        return {"query": text, "req_id": None, "created": None, "hits": []}

    if log:
        try:
            requirement, created = c.add_requirement(text)
        except (RuntimeError, ValueError) as exc:
            return {"query": text, "req_id": None, "created": None, "hits": [],
                    "error": str(exc)}
        row = {r.req_id: i for i, r in enumerate(c.requirements)}[requirement.req_id]
        hits = search(c.req_vectors[row], c.node_vectors, top_k=top_k)
        return api.trace(c, hits, req=requirement, created=created)

    vector = embed_texts([text])[0]
    return api.trace(c, search(vector, c.node_vectors, top_k=top_k), query=text)


@server.tool(
    description=(
        "UI tool for the req2code VS Code extension -- prefer trace_requirement. "
        "Re-ranks code against a requirement already in the set, by id."
    )
)
def ui_retrace(req_id: str, top_k: int = 20) -> dict:
    from src import api
    from src.index.vector_store import search

    c = corpus()
    index = {r.req_id: i for i, r in enumerate(c.requirements)}
    if req_id not in index:
        return {"query": "", "req_id": req_id, "created": None, "hits": [],
                "error": f"Unknown requirement {req_id!r}."}
    requirement = c.requirements[index[req_id]]
    hits = search(c.req_vectors[index[req_id]], c.node_vectors, top_k=top_k)
    return api.trace(c, hits, req=requirement)


@server.tool(
    description=(
        "UI tool for the req2code VS Code extension -- prefer find_orphans. "
        "Returns unclaimed methods plus the score distribution the threshold "
        "should be read against, as structured data."
    )
)
def ui_orphans(threshold: float = 0.30, limit: int = 100) -> dict:
    from src import api

    return api.orphans(corpus(), threshold, limit)


@server.tool(
    description=(
        "UI tool for the req2code VS Code extension. Given one file, returns "
        "every declaration in it with the requirements that best claim it. One "
        "call covers a whole file, which is what makes per-method annotations "
        "affordable while scrolling; do not call whose_requirement in a loop."
    )
)
def ui_file_map(file: str, top_k: int = 3) -> dict:
    from src import api

    return api.file_map(corpus(), file, top_k=top_k)


@server.tool(
    description=(
        "UI tool for the req2code VS Code extension. Removes a stated "
        "requirement from the project's set by id. Returns whether it existed."
    )
)
def ui_forget(req_id: str) -> dict:
    try:
        return {"removed": corpus().remove_requirement(req_id)}
    except RuntimeError as exc:
        return {"removed": False, "error": str(exc)}


def main() -> int:
    if not DATA_DIR.is_dir():
        sys.exit(
            f"Not a directory: {DATA_DIR}. Set REQ2CODE_ROOT to the repository "
            "you want traced, or see the README quickstart to fetch eTour."
        )

    # Load eagerly, before serving. The alternative -- lazy on first call -- just
    # moves the 15s from startup into the user's first query, which is the one
    # place the latency actually shows.
    print("req2code: indexing...", file=sys.stderr, flush=True)
    c = corpus()
    mode = c.layout.kind if c.layout else "unknown"
    vcs = "git" if c.git_root else "mtime polling"
    print(
        f"req2code: ready -- {mode} mode, {len(c.nodes)} nodes in "
        f"{len(c.mtimes)} files, {len(c.requirements)} requirements, "
        f"tracking changes via {vcs}.",
        file=sys.stderr,
        flush=True,
    )
    server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Command line for running req2code against a real repository.

    python -m scripts.req2code --path /some/project index
    python -m scripts.req2code --path /some/project state "the system shall ..."
    python -m scripts.req2code --path /some/project watch

WHY A CLI WHEN THERE IS ALREADY AN MCP SERVER
---------------------------------------------
Different jobs. The MCP server is how an editor talks to this; the CLI is how a
*person* does, and in particular how we test the claim that this works on code
it has never seen. Pointing it at a checkout of somebody else's project and
reading the output is the experiment, and it needs no editor, no extension, and
no protocol client to run.

It is also the honest way to demonstrate the tool. A demo script that reads a
bundled corpus and prints a table proves the pipeline runs. Typing a requirement
about a repository nobody prepared, and getting the right method back, proves
something else entirely.

THE COST MODEL, STATED PLAINLY
------------------------------
Every invocation loads the model (~3.5s) before it can answer. That is fine for
one question and wrong for twenty, which is exactly why the editor integration
is a long-lived process rather than a shell-out -- see `scripts/mcp_server.py`.
`watch` exists for the same reason: it pays the startup once and then answers
from a warm index while you edit.

OFFLINE
-------
No network, no API keys, no telemetry. The model is loaded from `models/` and
HuggingFace is pinned offline before it is imported; git is invoked only through
the read-only, remote-free allowlist in `ingest/vcs.py`. Justifications come
from the committed cache. There is nothing here that dials out, which is the
property that lets this run on a codebase that is not allowed to leave a
machine.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from src.index.embedder import pin_offline_if_cached

# Before sentence-transformers is imported anywhere, as every entry point must.
pin_offline_if_cached()

#: Poll interval for `watch`. Chosen to be well below human patience and well
#: above the cost of a refresh -- a git-backed refresh is three subprocesses, so
#: a second is roughly a 3% duty cycle rather than a busy loop.
WATCH_INTERVAL_SECONDS = 1.0


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def _label(corpus, node) -> str:
    enclosing = corpus.enclosing.get(node.node_id, "")
    name = f"{enclosing}.{node.name}()" if enclosing else f"{node.name}()"
    return f"{name}  {node.file_path}:{node.start_line}-{node.end_line}"


#: Payload builders live in `src/api.py`, shared with the MCP server so the
#: CLI's --json and the extension's structuredContent cannot drift apart.


def _emit(payload: dict, as_json: bool, render) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        render(payload)


def _print_hits(corpus, hits, header: str) -> None:
    print(header)
    if not hits:
        print("  (nothing indexed yet)")
        return
    for rank, (i, score) in enumerate(hits, start=1):
        print(f"  {rank:>3}. {score:.3f}  {_label(corpus, corpus.nodes[i])}")


# ---------------------------------------------------------------------------
# Corpus loading
# ---------------------------------------------------------------------------


def _load(path: Path, verbose: bool = False):
    from src.pipeline import load_corpus

    return load_corpus(path, verbose=verbose)


def _summary(corpus) -> dict:
    from src import api

    return api.status(corpus)


def _render_summary(payload: dict) -> None:
    print(f"root          {payload['root']}")
    print(f"mode          {payload['mode']}")
    langs = ", ".join(f"{k} {v}" for k, v in payload["languages"].items()) or "none"
    print(f"indexed       {payload['nodes']} nodes in {payload['files']} files")
    print(f"              {payload['methods']} methods; {langs}")
    print(f"requirements  {len(payload['requirements'])}")
    git = payload["git"]
    if git["tracked"]:
        head = (git["head"] or "no commits")[:12]
        print(f"git           {head}, {git['dirty']} path(s) dirty")
    else:
        print("git           not a repository -- falling back to mtime scanning")
    total = sum(payload["timings"].values())
    print(f"loaded in     {total:.2f}s")

    if not payload["requirements"]:
        print(
            "\nNo requirements yet. State one to begin building the map:\n"
            '  python -m scripts.req2code state "the system shall ..."'
        )


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_index(args) -> int:
    corpus = _load(args.path, verbose=args.verbose and not args.json)
    _emit(_summary(corpus), args.json, _render_summary)
    return 0


def cmd_search(args) -> int:
    """Free-text query without logging it. The read-only half of `state`."""
    from src import api
    from src.index.embedder import embed_texts
    from src.index.vector_store import search

    corpus = _load(args.path)
    if not corpus.nodes:
        print("Nothing indexed -- no parseable source found.", file=sys.stderr)
        return 1

    vector = embed_texts([args.query])[0]
    hits = search(vector, corpus.node_vectors, top_k=args.top_k)
    payload = api.trace(corpus, hits, query=args.query)
    _emit(
        payload,
        args.json,
        lambda p: _print_hits(corpus, hits, f"{args.query!r} -> top {len(hits)}:"),
    )
    return 0


def cmd_state(args) -> int:
    """State a requirement: log it, embed it, and show what implements it.

    The primary product path. Unlike `search` this is durable -- the requirement
    joins the set that orphan detection and reverse tracing score against, so
    stating requirements is how the map gets built.
    """
    from src import api
    from src.index.vector_store import search

    corpus = _load(args.path)
    try:
        requirement, created = corpus.add_requirement(args.text)
    except (RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    row = {r.req_id: i for i, r in enumerate(corpus.requirements)}[requirement.req_id]
    hits = search(corpus.req_vectors[row], corpus.node_vectors, top_k=args.top_k)
    payload = api.trace(corpus, hits, req=requirement, created=created)

    def render(p):
        note = "" if p["created"] else "  (already logged)"
        print(f"{p['req_id']}{note}  {p['query']}")
        _print_hits(corpus, hits, "")

    _emit(payload, args.json, render)
    return 0


def cmd_trace(args) -> int:
    """Rank code against a requirement already in the set."""
    from src import api
    from src.index.vector_store import search

    corpus = _load(args.path)
    index = {r.req_id: i for i, r in enumerate(corpus.requirements)}
    if args.req_id not in index:
        known = ", ".join(sorted(index)) or "none yet"
        print(f"Unknown requirement {args.req_id!r}. Known: {known}", file=sys.stderr)
        return 1

    row = index[args.req_id]
    hits = search(corpus.req_vectors[row], corpus.node_vectors, args.top_k)
    payload = api.trace(corpus, hits, req=corpus.requirements[row])

    def render(p):
        print(f"{args.req_id} -> top {len(p['hits'])}:")
        for item in p["hits"]:
            mark = "  [gold]" if item.get("gold") else ""
            enclosing = item["enclosing"]
            name = f"{enclosing}.{item['name']}()" if enclosing else item["name"]
            print(
                f"  {item['rank']:>3}. {item['score']:.3f}  {name}  "
                f"{item['file']}:{item['start_line']}-{item['end_line']}{mark}"
            )

    _emit(payload, args.json, render)
    return 0


def cmd_whose(args) -> int:
    """The reverse direction: what is the method under this cursor for?"""
    from src import api
    from src.index.vector_store import search
    from src.retrieve.locate import find_node_at

    corpus = _load(args.path)
    node = find_node_at(corpus.nodes, args.file, args.line, code_root=corpus.code_root)
    if node is None:
        print(f"No parsed declaration covers {args.file}:{args.line}.", file=sys.stderr)
        return 1
    if not corpus.requirements:
        print(
            f"{_label(corpus, node)}\n\n"
            "No requirements have been stated, so there is nothing to rank this "
            'against. Try: python -m scripts.req2code state "..."',
            file=sys.stderr,
        )
        return 1

    row = corpus.node_index()[node.node_id]
    hits = search(corpus.node_vectors[row], corpus.req_vectors, top_k=args.top_k)
    payload = {
        "node_id": node.node_id,
        "name": node.name,
        "enclosing": corpus.enclosing.get(node.node_id, ""),
        "file": node.file_path,
        "start_line": node.start_line,
        "end_line": node.end_line,
        "requirements": [
            api.requirement(corpus.requirements[i], score=s, rank=r)
            for r, (i, s) in enumerate(hits, start=1)
        ],
    }

    def render(p):
        print(_label(corpus, node))
        print()
        for item in p["requirements"]:
            print(
                f"  {item['rank']:>3}. {item['score']:.3f}  {item['req_id']}  "
                f"{item['title']}"
            )
        best = p["requirements"][0]["score"] if p["requirements"] else 0.0
        if best < args.threshold:
            print(
                f"\n  Best match is only {best:.3f} -- this may be an orphan "
                "(plumbing or dead code that no stated requirement asks for)."
            )

    _emit(payload, args.json, render)
    return 0


def cmd_orphans(args) -> int:
    from src import api

    corpus = _load(args.path)
    payload = api.orphans(corpus, args.threshold, args.limit)
    if payload["reason"]:
        print(
            f"Orphan detection is not meaningful here: {payload['reason']}.",
            file=sys.stderr,
        )
        return 1

    def render(p):
        share = p["flagged"] / p["methods"]
        print(
            f"{p['flagged']} of {p['methods']} methods ({share:.0%}) score below "
            f"{p['threshold']:.2f}."
        )
        d = p["distribution"]
        if d:
            print(
                f"Distribution: p5={d['p5']:.3f} median={d['median']:.3f} "
                f"max={d['max']:.3f}. The ranked order is the reliable signal; "
                "the threshold is uncalibrated."
            )
        print()
        for item in p["orphans"]:
            print(
                f"  {item['score']:.3f}  {item['name']}()  "
                f"{item['file']}:{item['start_line']}"
            )

    _emit(payload, args.json, render)
    return 0


def cmd_reqs(args) -> int:
    """List the stated requirements, newest last."""
    from src import api

    corpus = _load(args.path)
    requirements = [api.requirement(r) for r in corpus.requirements]
    payload = {"count": len(requirements), "requirements": requirements}

    def render(p):
        if not p["count"]:
            print("No requirements stated yet.")
            return
        for item in p["requirements"]:
            print(f"  {item['req_id']}  {item['title']}")

    _emit(payload, args.json, render)
    return 0


def cmd_forget(args) -> int:
    from src.ingest.requirement_store import remove_requirement
    from src.ingest.workspace import discover

    layout = discover(args.path)
    if layout.store_path is None:
        print("This corpus has requirement documents on disk; nothing to forget.")
        return 1
    if remove_requirement(layout.root, args.req_id):
        print(f"Removed {args.req_id}.")
        return 0
    print(f"No such requirement: {args.req_id}", file=sys.stderr)
    return 1


def cmd_watch(args) -> int:
    """Keep the index live while you work.

    This is the whole live-mapping claim, reduced to something you can watch
    happen. Edit a file, switch a branch, pull -- the mapping follows, and every
    update costs one re-parse and one embed of the file that actually changed
    rather than a rebuild.

    Deliberately a foreground loop rather than a daemon: the point is to *see*
    it react. A background service that silently keeps an index warm is the
    right shape for the editor integration and the wrong shape for convincing
    anyone it works.
    """
    corpus = _load(args.path, verbose=not args.json)
    print(f"Watching {corpus.code_root} -- Ctrl-C to stop.")
    _render_summary(_summary(corpus))
    print()

    try:
        while True:
            start = time.perf_counter()
            before = len(corpus.requirements)
            changed = corpus.refresh()
            elapsed = (time.perf_counter() - start) * 1000
            if changed:
                stamp = time.strftime("%H:%M:%S")
                print(
                    f"[{stamp}] reindexed {len(changed)} file(s) in {elapsed:.0f}ms: "
                    + ", ".join(sorted(changed)[:5])
                    + (" ..." if len(changed) > 5 else "")
                )
                print(f"          {len(corpus.nodes)} nodes indexed")
            if len(corpus.requirements) > before:
                added = len(corpus.requirements) - before
                print(f"[{time.strftime('%H:%M:%S')}] +{added} requirement(s)")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="req2code",
        description=(
            "Requirement-to-code traceability over any repository. Offline: no "
            "network, no API keys."
        ),
    )
    parser.add_argument(
        "--path",
        "-C",
        type=Path,
        default=Path("."),
        help="Repository or corpus root (default: current directory).",
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit machine-readable output."
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Show timings.")

    sub = parser.add_subparsers(dest="command", required=True)

    def with_top_k(p, default: int = 10):
        p.add_argument("--top-k", "-k", type=int, default=default)
        return p

    p = sub.add_parser("index", help="Build the index and report what was found.")
    p.set_defaults(func=cmd_index)

    p = with_top_k(sub.add_parser("search", help="Free-text search; does not log."))
    p.add_argument("query")
    p.set_defaults(func=cmd_search)

    p = with_top_k(
        sub.add_parser("state", help="State a requirement and trace it (logs it).")
    )
    p.add_argument("text")
    p.set_defaults(func=cmd_state)

    p = with_top_k(sub.add_parser("trace", help="Trace a requirement already stated."))
    p.add_argument("req_id")
    p.set_defaults(func=cmd_trace)

    p = with_top_k(
        sub.add_parser("whose", help="What requirement is this code for?"), default=5
    )
    p.add_argument("file")
    p.add_argument("line", type=int)
    p.add_argument("--threshold", type=float, default=0.30)
    p.set_defaults(func=cmd_whose)

    p = sub.add_parser("orphans", help="Code no stated requirement claims.")
    p.add_argument("--threshold", type=float, default=0.30)
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_orphans)

    p = sub.add_parser("reqs", help="List stated requirements.")
    p.set_defaults(func=cmd_reqs)

    p = sub.add_parser("forget", help="Remove a stated requirement.")
    p.add_argument("req_id")
    p.set_defaults(func=cmd_forget)

    p = sub.add_parser("watch", help="Keep the index live as the repository changes.")
    p.add_argument("--interval", type=float, default=WATCH_INTERVAL_SECONDS)
    p.set_defaults(func=cmd_watch)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

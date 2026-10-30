"""LLM-generated explanations of why a node satisfies a requirement.

Novelty claim #3. A ranked list tells an engineer *that* method X scored 0.71
against requirement R; it does not tell them whether to believe it. A short
natural-language rationale turns an opaque score into something reviewable.

Two hard rules:

1. **Everything is cached to disk as JSON.** The demo must run with the network
   off. Justifications are generated once, committed, and replayed. A live API
   call during a demo is a coin flip on your wifi, and the marginal value of
   freshness is zero -- the corpus does not change.

2. **Justifications are never fed back into scoring.** The LLM sees the trace
   after it is chosen and explains it. If its output influenced the ranking, we
   could no longer claim the retrieval results are reproducible from a fixed
   pipeline, and the eventual "does the rationale match human reasoning?"
   evaluation would be circular.

Scoring the justifications against human rationale is explicitly out of scope
for Review 1. Generate them, cache them, show one.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from src.contracts import CodeNode, Requirement

CACHE_DIR = Path(__file__).parent / "cache"
CACHE_FILE = CACHE_DIR / "justifications.json"

#: Bump when the prompt changes, so old cached text is not silently reused with a
#: new prompt. Logged alongside each entry.
PROMPT_VERSION = "v1"

PROMPT_TEMPLATE = """\
You are analysing traceability between a software requirement and a code method.

REQUIREMENT ({req_id}):
{req_text}

CODE ({node_id}, lines {start_line}-{end_line}):
{node_text}

In 2-3 sentences, explain whether and how this method contributes to satisfying
the requirement. Refer to specific identifiers in the code. If it does not
appear to satisfy the requirement, say so plainly -- a confident wrong answer is
worse than an honest negative.
"""


def cache_key(req: Requirement, node: CodeNode) -> str:
    """Stable, filename-safe key for one (requirement, node) pair.

    Hashed rather than concatenated because `node_id` contains path separators
    and ``#``, which are hostile to filenames and to JSON-pointer style lookups.
    The readable ids are stored inside the entry, so nothing is lost.
    """
    raw = f"{PROMPT_VERSION}|{req.req_id}|{node.node_id}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def build_prompt(req: Requirement, node: CodeNode, max_code_chars: int = 1800) -> str:
    """Render the prompt for one pair. Pure -- no network, safe to unit test."""
    return PROMPT_TEMPLATE.format(
        req_id=req.req_id,
        req_text=req.text.strip()[:1500],
        node_id=node.node_id,
        start_line=node.start_line,
        end_line=node.end_line,
        node_text=node.text[:max_code_chars],
    )


def load_cache(cache_file: Path = CACHE_FILE) -> dict[str, dict]:
    """Load the whole justification cache. Returns ``{}`` if absent or corrupt."""
    if not cache_file.exists():
        return {}
    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_cache(cache: dict[str, dict], cache_file: Path = CACHE_FILE) -> None:
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(cache, indent=2), encoding="utf-8")


def get_justification(
    req: Requirement, node: CodeNode, cache_file: Path = CACHE_FILE
) -> str | None:
    """Return the cached justification text for a pair, or None if absent.

    Read-only by design: it never calls out to a model. Generation is a separate,
    deliberate, online step, so it is impossible to accidentally trigger a
    network call from the demo path.
    """
    entry = load_cache(cache_file).get(cache_key(req, node))
    if entry is None:
        return None
    text = entry.get("justification")
    return text if isinstance(text, str) else None


def store_justification(
    req: Requirement,
    node: CodeNode,
    justification: str,
    model: str,
    cache_file: Path = CACHE_FILE,
) -> None:
    """Write one justification into the cache, with provenance.

    The model id and prompt version are stored per entry because "every number
    traceable to a logged config" applies to generated text too -- in six weeks
    nobody will remember which model wrote these.
    """
    cache = load_cache(cache_file)
    cache[cache_key(req, node)] = {
        "req_id": req.req_id,
        "node_id": node.node_id,
        "model": model,
        "prompt_version": PROMPT_VERSION,
        "justification": justification.strip(),
    }
    save_cache(cache, cache_file)


def cached_pairs(cache_file: Path = CACHE_FILE) -> list[tuple[str, str]]:
    """``(req_id, node_id)`` for everything in the cache -- useful for the demo
    to pick a pair it knows it can display."""
    return [
        (entry["req_id"], entry["node_id"])
        for entry in load_cache(cache_file).values()
        if "req_id" in entry and "node_id" in entry
    ]

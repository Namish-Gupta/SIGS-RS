"""Requirement rewriting, to close the vocabulary gap. Ablation run E2.

The problem: requirements are written in user/business language ("the tourist
shall be able to search for nearby cultural sites") while code is written in
implementation language (`findPOIByRadius`). The two describe the same behaviour
with almost disjoint vocabulary. This is the central difficulty of traceability
recovery and the reason lexical baselines plateau.

The idea: rewrite each requirement into something closer to how a developer
would name things, then embed *that*.

E2 exists to test whether it helps. It may not -- expansion can also add noise
and drag in unrelated methods. A negative result here is a perfectly good
ablation row and should be reported as one.

Offline constraint: expansions are generated once and committed to disk as JSON,
exactly like the justification cache. The demo never makes a network call.

The offline fallback below is deliberately mechanical rather than LLM-generated:
it strips use-case boilerplate ("Use case name:", "Entry conditions:", "Flow of
events") that appears in all 58 eTour requirements and therefore carries zero
discriminating signal while consuming MiniLM's 256-token budget. That is a real,
explainable transformation -- and if an LLM expansion cache is present it takes
precedence.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from src.contracts import Requirement

CACHE_PATH = Path("src/retrieve/cache/expansions.json")

#: Use-case template labels present in every eTour requirement. Uniform across
#: the corpus -> zero discriminating power, but they eat the token budget.
_BOILERPLATE = re.compile(
    r"^\s*(use case name|description|participating actors?|participating actor"
    r"|entry (operator )?conditions?|exit conditions?|flow of events"
    r"|quality requirements?|special requirements?|operator agency"
    r"|initialization|interruption of connection)\s*:?\s*",
    re.IGNORECASE | re.MULTILINE,
)

#: Leading step numbers ("1.", "2 ") in the event-flow lists.
_STEP_NUMBERS = re.compile(r"^\s*\d+[.)]?\s*", re.MULTILINE)


def mechanical_expansion(text: str) -> str:
    """Strip use-case boilerplate, keeping the behavioural prose.

    Not an "expansion" in the generative sense -- it is a reduction that raises
    the signal-to-noise ratio of what reaches the embedder. Named for its role in
    the ablation (E2 = "does requirement rewriting help?") rather than its
    mechanism.
    """
    cleaned = _BOILERPLATE.sub(" ", text)
    cleaned = _STEP_NUMBERS.sub(" ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def load_expansion_cache(cache_path: str | Path = CACHE_PATH) -> dict[str, str]:
    """Load pre-generated LLM expansions. Returns ``{}`` if the file is absent."""
    cache_path = Path(cache_path)
    if not cache_path.exists():
        return {}
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, str)}


def expand_requirement(req: Requirement, cache: dict[str, str] | None = None) -> str:
    """Return the expanded query text for a requirement.

    Precedence: a cached LLM expansion if one exists, else the mechanical
    transformation. Falling back rather than raising means a partially populated
    cache still produces a complete, interpretable run.
    """
    if cache:
        cached = cache.get(req.req_id)
        if cached:
            return cached
    return mechanical_expansion(req.text)


def expand_all(
    requirements: list[Requirement], cache_path: str | Path = CACHE_PATH
) -> list[str]:
    """Expanded text for every requirement, order-aligned with the input."""
    cache = load_expansion_cache(cache_path)
    return [expand_requirement(req, cache) for req in requirements]

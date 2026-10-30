"""Load the eTour gold answer set.

GATE -- VERIFIED. The raw file was opened and inspected before anything was built
on it. Format is one link per line:

    UC1.txt: CulturalHeritageAgencyManager.java

i.e. requirement -> code FILE, which is what the whole granularity strategy
assumes (see eval/metrics.py). Confirmed counts: 58 requirements, 116 code
artifacts, 308 links -- matching the dataset README exactly.

Note the requirement id includes the ``.txt`` extension and the artifact id
includes ``.java``. Both are kept verbatim; stripping either would make every
lookup miss and silently score zero.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from src.ingest.text_io import read_text

#: Expected link count, from the dataset README. Asserted on load because a
#: silent parse failure that yields 12 links will still "run" and will quietly
#: produce excellent-looking recall.
EXPECTED_LINKS = 308

#: Requirements present in req/ but absent from the gold file. VERIFIED against
#: the raw data: eTour ships 58 use cases but only 57 appear in the answer set --
#: UC37 ("Logout") was never linked to any class by the original annotators.
#:
#: This is a property of the dataset, not a parse bug, and it is handled rather
#: than hidden: metrics.py excludes requirements with no gold links from MAP,
#: because scoring them as 0.0 would drag every configuration down by the same
#: constant and make our numbers non-comparable with published eTour results.
UNLINKED_REQUIREMENTS = frozenset({"UC37.txt"})
EXPECTED_REQUIREMENTS = 57


def load_gold_links(gold_path: str | Path, strict: bool = True) -> dict[str, set[str]]:
    """Read the gold trace matrix. Returns ``{}`` when there is no answer key.

    Args:
        gold_path: Path to an answer-set file, e.g.
            ``data/etour/etour_solution_links_english.txt``.
        strict: Enforce the documented eTour shape -- the file must exist and
            must parse to exactly `EXPECTED_LINKS` links over
            `EXPECTED_REQUIREMENTS` requirements. Pass ``False`` for any other
            corpus, where those counts are meaningless.

    Returns:
        Mapping of ``req_id -> set of code file names`` that genuinely implement
        it. A set because membership testing is the only operation metrics.py
        needs, and duplicates in the source file should collapse.

    Gold links are an *evaluation* input, not a retrieval input: nothing in
    parse/, index/, retrieve/ or justify/ reads them. An unlabelled repository
    therefore traces, ranks, and reports orphans exactly as eTour does -- it
    simply cannot be scored, because there is nothing to score against. That is
    why a missing file is a valid state under ``strict=False`` rather than an
    error, and why the strict eTour contract is still enforced by default: a
    silent parse failure on the dataset we *do* publish numbers for would
    produce excellent-looking recall against a fraction of the answer key.
    """
    gold_path = Path(gold_path)
    if not gold_path.exists():
        if strict:
            raise FileNotFoundError(
                f"Gold link file not found: {gold_path}\n"
                "Fetch the dataset first -- see the README quickstart, or pass "
                "strict=False to run unscored on a corpus with no answer key."
            )
        return {}

    gold: dict[str, set[str]] = defaultdict(set)
    total = 0
    for line in read_text(gold_path).splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        req_id, _, artifact = line.partition(":")
        req_id, artifact = req_id.strip(), artifact.strip()
        if not req_id or not artifact:
            continue
        gold[req_id].add(artifact)
        total += 1

    if strict:
        if total != EXPECTED_LINKS:
            raise ValueError(
                f"Parsed {total} gold links, expected {EXPECTED_LINKS}. "
                "The gold file format may have changed -- inspect it before trusting "
                "any metric computed from it."
            )
        if len(gold) != EXPECTED_REQUIREMENTS:
            raise ValueError(
                f"Parsed {len(gold)} linked requirements, expected "
                f"{EXPECTED_REQUIREMENTS}. Note eTour ships 58 use cases but only "
                f"{EXPECTED_REQUIREMENTS} carry gold links "
                f"({', '.join(sorted(UNLINKED_REQUIREMENTS))} has none)."
            )

    return dict(gold)

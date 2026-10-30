"""Spike: watch a CodeNode turn into an embeddable document, one stage at a time.

    python spikes/spike_split.py
    python spikes/spike_split.py --name insertBanner

This is the learning sandbox for src/index/node_doc.py -- the highest-leverage
module in the project. Everything it prints is real output from the real corpus,
so you can change node_doc.py, re-run this, and see exactly what moved.

Throwaway. Not imported by src/, not tested, not required to be clean.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Running `python spikes/spike_split.py` puts spikes/ on sys.path, not the repo
# root, so `import src...` would fail. Adding the repo root here means the spike
# works however you launch it -- directly, via -m, or from an IDE play button.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.index.node_doc import (  # noqa: E402
    JAVA_STOPWORDS,
    MAX_BODY_WORDS,
    build_node_document,
    clean_doc_comment,
    split_identifier,
)
from src.parse.java_parser import enclosing_class_map, parse_repo  # noqa: E402

BAR = "=" * 78


def stage_1_splitter() -> None:
    """The 20 lines everything else rests on."""
    print(f"\n{BAR}\nSTAGE 1 -- split_identifier(): camelCase -> English words\n{BAR}")
    cases = [
        ("insertBanner", "the ordinary case"),
        ("send_alert", "snake_case: '_' is a separator"),
        ("HTTPServer", "ACRONYM -- the one that breaks naive splitters"),
        ("IDBRefreshmentPoint", "acronym + words, real eTour identifier"),
        ("getURLFor2ndUser", "digits stay glued to their suffix"),
        ("XMLHttpRequest2", "trailing digit stands alone"),
    ]
    for ident, why in cases:
        print(f"  {ident:22} -> {str(split_identifier(ident)):48} {why}")

    print("\n  Why the acronym case matters: a naive r'(?=[A-Z])' split would give")
    print("  ['h','t','t','p','server'] -- five useless tokens instead of two real")
    print("  words. The negative lookahead (?![a-z]) is the whole trick.")


def stage_2_javadoc(docs: dict[str, str], node_id: str) -> None:
    """Javadoc is the strongest signal in this corpus -- 933 of 1210 nodes have it."""
    print(f"\n{BAR}\nSTAGE 2 -- clean_doc_comment(): Javadoc -> prose\n{BAR}")
    raw = docs.get(node_id, "")
    if not raw:
        print("  (this node has no Javadoc)")
        return
    print("  RAW:")
    for line in raw.splitlines()[:8]:
        print(f"    {line}")
    print("\n  CLEANED:")
    print(f"    {clean_doc_comment(raw)[:300]}")
    print("\n  Note @param/@see lines are dropped: in eTour they are almost all")
    print("  fully-qualified package paths, which repeat across hundreds of nodes")
    print("  and carry no discriminating signal.")


def stage_3_document(node, enclosing: str, doc: str) -> None:
    """The five ingredients, assembled."""
    print(f"\n{BAR}\nSTAGE 3 -- build_node_document(): the five ingredients\n{BAR}")
    parts = [
        ("1. name (split)", " ".join(split_identifier(node.name))),
        (
            "2. enclosing class",
            " ".join(split_identifier(enclosing)) if enclosing else "(none)",
        ),
        ("3. javadoc prose", clean_doc_comment(doc)[:120] if doc else "(none)"),
        ("4. signature", node.signature[:120]),
        ("5. body identifiers", "(see full document below)"),
    ]
    for label, value in parts:
        print(f"  {label:22} {value}")

    document = build_node_document(node, enclosing_class=enclosing, doc_comment=doc)
    words = document.split()
    print(f"\n  FULL DOCUMENT ({len(words)} words, body capped at {MAX_BODY_WORDS}):")
    for i in range(0, min(len(words), 60), 12):
        print(f"    {' '.join(words[i : i + 12])}")
    if len(words) > 60:
        print(f"    ... +{len(words) - 60} more")

    print("\n  This -- not the raw Java -- is what gets embedded. Compare:")
    print(f"    raw source : {len(node.text)} chars of braces, modifiers, types")
    print(f"    document   : {len(document)} chars of pseudo-English")


def stage_4_why_it_works(node, enclosing: str, doc: str) -> None:
    """Show the actual bridge between requirement prose and code."""
    print(f"\n{BAR}\nSTAGE 4 -- why this bridges prose and code\n{BAR}")
    document = set(
        build_node_document(node, enclosing_class=enclosing, doc_comment=doc).split()
    )
    requirement = (
        "Inserting a new banner associated with a point of rest. Check that the "
        "number of banner did not exceed the maximum point of the restaurant."
    )
    req_words = {w.lower().strip(".,") for w in requirement.split()}
    shared = sorted(w for w in req_words & document if w not in JAVA_STOPWORDS)

    print(f"  requirement UC20: {requirement[:70]}...")
    print(f"\n  words shared with the node document: {shared}")
    print("\n  Those overlaps exist ONLY because identifiers were split. In the raw")
    print("  source the words 'banner', 'insert', and 'maximum' are welded inside")
    print("  insertBanner / getMaxBanner and invisible to any tokenizer.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", default="insertBanner", help="method name to inspect")
    parser.add_argument("--data-dir", default="data/etour/code")
    args = parser.parse_args()

    stage_1_splitter()

    nodes, docs = parse_repo(args.data_dir)
    enclosing = enclosing_class_map(nodes)

    match = next(
        (n for n in nodes if n.name == args.name and n.kind == "method" and n.text),
        None,
    )
    if match is None:
        print(f"\nNo method named {args.name!r} found. Try --name modifyNews")
        return 1

    print(f"\n\nInspecting: {match.node_id}")
    doc = docs.get(match.node_id, "")
    enc = enclosing.get(match.node_id, "")

    stage_2_javadoc(docs, match.node_id)
    stage_3_document(match, enc, doc)
    stage_4_why_it_works(match, enc, doc)

    print(f"\n{BAR}")
    print("TRY THIS: open src/index/node_doc.py, comment out the enclosing-class")
    print("block in build_node_document(), re-run this spike, then re-run")
    print("`python -m scripts.run_ablation` and see whether MAP moves.")
    print(BAR)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

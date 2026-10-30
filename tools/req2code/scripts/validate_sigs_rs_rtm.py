"""Score req2code against SIGS-RS's hand-written requirements traceability matrix.

Usage (run with req2code's venv, cwd = req2code root):
    python <this file> <sigs-rs checkout>
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(Path.cwd()))

from src.index.embedder import pin_offline_if_cached  # noqa: E402

pin_offline_if_cached()

from src.index.embedder import embed_texts  # noqa: E402
from src.index.vector_store import search  # noqa: E402
from src.pipeline import load_corpus  # noqa: E402

RTM = ROOT / "docs/phase14/requirements-traceability-matrix.md"
ROW = re.compile(r"^\|\s*\*\*(REQ-[A-Z]+-\d+):\*\*\s*(.*?)\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|\s*$")
TICK = re.compile(r"`([^`]+)`")


def parse_rtm():
    rows = []
    for line in RTM.read_text(encoding="utf-8").splitlines():
        m = ROW.match(line)
        if not m:
            continue
        req_id, text, impl, _test, status = m.groups()
        files, syms = set(), set()
        for tok in TICK.findall(impl):
            tok = tok.strip()
            if tok.endswith(".py") and "/" in tok:
                files.add(tok)
            elif re.fullmatch(r"[A-Za-z_][\w.]*(\(\))?", tok):
                syms.add(tok.rstrip("()").split(".")[-1])
        files = {f for f in files if (ROOT / f).is_file()}
        if files:
            rows.append((req_id, text, files, syms, status))
    return rows


def main() -> None:
    rows = parse_rtm()
    corpus = load_corpus(ROOT)
    print(f"requirements with a checkable answer: {len(rows)}; nodes: {len(corpus.nodes)}")
    vecs = embed_texts([r[1] for r in rows])

    K = 10
    stats = {"file@5": 0, "file@10": 0, "sym@5": 0, "sym@10": 0, "rr": 0.0}
    print(f"\n{'req':<10} {'1st file hit':>12} {'1st sym hit':>12}  top-3")
    for (req_id, _text, files, syms, status), vec in zip(rows, vecs):
        hits = search(vec, corpus.node_vectors, top_k=K)
        ranked = [corpus.nodes[i] for i, _ in hits]
        norm = lambda p: p.replace("\\", "/")  # noqa: E731
        f_rank = next((r for r, n in enumerate(ranked, 1) if norm(n.file_path) in files), None)
        s_rank = next(
            (r for r, n in enumerate(ranked, 1)
             if norm(n.file_path) in files and n.name in syms),
            None,
        )
        for key, rank in (("file", f_rank), ("sym", s_rank)):
            if rank and rank <= 5:
                stats[f"{key}@5"] += 1
            if rank and rank <= 10:
                stats[f"{key}@10"] += 1
        stats["rr"] += 1 / f_rank if f_rank else 0
        top3 = "; ".join(f"{n.name}" for n in ranked[:3])
        print(f"{req_id:<10} {str(f_rank):>12} {str(s_rank):>12}  {top3}")

    n = len(rows)
    print("\n--- summary ---")
    print(f"file-level hit@5   {stats['file@5']}/{n} = {stats['file@5']/n:.2f}")
    print(f"file-level hit@10  {stats['file@10']}/{n} = {stats['file@10']/n:.2f}")
    print(f"symbol-level hit@5   {stats['sym@5']}/{n} = {stats['sym@5']/n:.2f}")
    print(f"symbol-level hit@10  {stats['sym@10']}/{n} = {stats['sym@10']/n:.2f}")
    print(f"file-level MRR     {stats['rr']/n:.3f}")


main()

"""Load eTour requirement documents off disk into `Requirement` objects.

eTour ships one plain-text file per requirement (a use-case description). The
filename INCLUDING the .txt extension is the requirement id used in the gold-link
file -- gold lines look like ``UC1.txt: CulturalHeritage.java`` -- so the
extension is carried through verbatim. Stripping it would make every gold lookup
miss and silently score zero.
"""

from __future__ import annotations

from pathlib import Path

from src.contracts import Requirement
from src.ingest.text_io import read_text


def load_requirements(requirements_dir: str | Path) -> list[Requirement]:
    """Read every requirement document in `requirements_dir`.

    Args:
        requirements_dir: Directory of one-file-per-requirement text documents,
            e.g. ``data/etour/req/``.

    Returns:
        Requirements sorted by `req_id`, so runs are reproducible regardless of
        filesystem iteration order.
    """
    requirements_dir = Path(requirements_dir)
    if not requirements_dir.is_dir():
        raise FileNotFoundError(
            f"Requirements directory not found: {requirements_dir}\n"
            "Fetch the dataset first -- see the README quickstart."
        )

    reqs = [
        Requirement(
            req_id=path.name,  # keep the .txt -- gold links include it
            text=read_text(path).strip(),
            source_path=str(path),
        )
        for path in sorted(requirements_dir.glob("*.txt"))
    ]
    return sorted(reqs, key=lambda r: r.req_id)

"""Entry point for the command line. See `src/cli.py` for the commands.

    python -m scripts.req2code --path /some/project index

A two-line shim, for the same reason every other entry point in `scripts/` is
thin: the logic lives in `src/` where it can be imported and tested, and
`scripts/` holds only the things a person types. `src.cli` pins HuggingFace
offline at import, before anything can pull in torch.
"""

from __future__ import annotations

from src.cli import main

if __name__ == "__main__":
    raise SystemExit(main())

"""Java parsing -- now a thin alias over the generic, multi-language parser.

Everything that used to live here moved to `parse/parser.py` when the project
gained Python, C#, C, C++, Go and Rust support. The Java-specific knowledge that
remained -- which node types to lift, which siblings are doc comments, which
field holds the return type -- is now `JAVA` in `parse/languages.py`.

This module stays because it is the import path used by `pipeline.py`,
`bench_latency.py`, `spike_split.py`, and the test-suite, and because those call
sites are the regression gate for the refactor: if Java parsing changed, they
fail, and so does the ablation table. Keeping the alias means the gate stayed
usable across the change instead of being rewritten at the same time as the
thing it was meant to be guarding.

New code should import from `src.parse.parser` directly.
"""

from __future__ import annotations

from src.parse.languages import JAVA
from src.parse.parser import enclosing_class_map, parse_file, parse_repo

#: Preserved for callers that imported the Java node-type table directly.
NODE_TYPES = JAVA.node_types

__all__ = ["NODE_TYPES", "enclosing_class_map", "parse_file", "parse_repo"]

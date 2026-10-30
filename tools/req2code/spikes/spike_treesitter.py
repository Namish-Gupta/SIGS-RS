"""Spike: does tree-sitter parse Java on this machine?

Run first, before any pipeline code:

    python spikes/spike_treesitter.py

Answers two questions:
    1. Does the parser install and load on Windows?
    2. Do the core (0.26.0) and grammar (0.23.5) agree on ABI version? Grammar
       wheels encode an ABI number and a newer core can refuse an older grammar.

If step 2 fails, downgrade `tree-sitter` to 0.23.x in requirements.txt before
reaching for the javalang fallback -- a version mismatch is not a reason to
throw away the pluggable-language parser.

Throwaway. Not imported by src/, not tested, not required to be clean.
"""

from __future__ import annotations

import textwrap

SAMPLE = textwrap.dedent("""\
    package com.example.etour;

    /** Guides a tourist around nearby points of interest. */
    public class TourGuide {
        private final SiteRepository repo;

        public TourGuide(SiteRepository repo) {
            this.repo = repo;
        }

        /** Notifies the user about attractions close to them. */
        public List<Site> findNearbyAttractions(double lat, double lon, int radiusKm) {
            List<Site> sites = repo.withinRadius(lat, lon, radiusKm);
            sendAlert(sites);
            return sites;
        }

        private void sendAlert(List<Site> sites) {
            notifier.push("Found " + sites.size() + " attractions nearby");
        }
    }
    """)


def main() -> int:
    import tree_sitter
    import tree_sitter_java

    print(f"tree-sitter core   : {getattr(tree_sitter, '__version__', 'unknown')}")

    # The ABI handshake. If core and grammar disagree, this is where it blows up.
    try:
        language = tree_sitter.Language(tree_sitter_java.language())
    except Exception as exc:
        print(f"\nFAILED at Language(): {type(exc).__name__}: {exc}")
        print("-> Core/grammar ABI mismatch. Pin tree-sitter==0.23.2 and retry.")
        return 1

    print(f"grammar ABI version: {language.abi_version}")

    parser = tree_sitter.Parser(language)
    tree = parser.parse(SAMPLE.encode("utf-8"))

    if tree.root_node.has_error:
        print("\nWARNING: parse produced ERROR nodes on known-good Java.")

    # Walk for the declarations we care about. This is a throwaway version of
    # what src/parse/java_parser.py will do properly.
    wanted = {
        "method_declaration",
        "constructor_declaration",
        "class_declaration",
    }
    found: list[tuple[str, str, int, int]] = []

    def visit(node) -> None:
        if node.type in wanted:
            name_node = node.child_by_field_name("name")
            name = name_node.text.decode("utf-8") if name_node else "<anonymous>"
            # tree-sitter rows are 0-based; CodeNode is 1-based.
            found.append(
                (node.type, name, node.start_point[0] + 1, node.end_point[0] + 1)
            )
        for child in node.children:
            visit(child)

    visit(tree.root_node)

    print(f"\nfound {len(found)} declarations:")
    for kind, name, start, end in found:
        print(f"  {kind:24} {name:24} lines {start}-{end}")

    # Sanity check: the sample has 1 class, 1 constructor, 2 methods.
    ok = len(found) == 4 and any(n == "findNearbyAttractions" for _, n, _, _ in found)
    print(
        "\nRESULT:",
        "OK - tree-sitter works, proceed" if ok else "UNEXPECTED - inspect above",
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

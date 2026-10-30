"""Multi-language parsing tests.

These cover the `LanguageSpec` layer added in Phase 0 of docs/ROADMAP.md. They
are offline and need no embedding model: parsing is pure tree-sitter, so the
whole file runs in well under a second.

The tests are written per *behaviour*, not per language, so that adding an
eighth language means adding a fixture rather than a new test class. Where a
language is called out by name it is because that language is the one that
breaks a naive implementation -- Python's docstring placement, C's declarator
nesting, Rust's `impl` blocks, Go's receiver.
"""

from __future__ import annotations

import pytest

from src.index.node_doc import build_all_documents
from src.parse.languages import (
    SUFFIX_TO_SPEC,
    SUPPORTED_SUFFIXES,
    spec_for_language,
    spec_for_path,
    stopwords_for_path,
)
from src.parse.parser import enclosing_class_map, parse_repo

# ---------------------------------------------------------------------------
# Fixtures: one small, realistic file per language.
# ---------------------------------------------------------------------------

SOURCES: dict[str, str] = {
    "Guide.java": (
        "package tour;\n"
        "/** Guides tourists. */\n"
        "public class TourGuide {\n"
        "  /** Finds attractions near the visitor. */\n"
        "  public List<Site> findNearbyAttractions(int radiusMeters, String city) {\n"
        "    return query(radiusMeters);\n"
        "  }\n"
        "  public TourGuide(int maxResults) { }\n"
        "}\n"
    ),
    "Guide.cs": (
        "namespace Tour {\n"
        "  /// <summary>Guides tourists.</summary>\n"
        "  public class TourGuide {\n"
        "    /// Finds attractions near the visitor.\n"
        "    /// Ordered by distance.\n"
        "    public List<Site> FindNearbyAttractions(int radiusMeters, string city) {\n"
        "      return Query(radiusMeters);\n"
        "    }\n"
        "  }\n"
        "}\n"
    ),
    "guide.py": (
        '"""Module docstring that must not leak onto the class."""\n'
        "\n"
        "class TourGuide:\n"
        '    """Guides tourists around a city."""\n'
        "\n"
        "    def find_nearby_attractions(self, radius_meters, city):\n"
        '        """Find attractions near the visitor."""\n'
        "        return self._query(radius_meters)\n"
    ),
    "guide.cpp": (
        "namespace tour {\n"
        "/// Guides tourists.\n"
        "class TourGuide {\n"
        " public:\n"
        "  /// Finds attractions near the visitor.\n"
        "  std::vector<Site> *findNearbyAttractions(int radiusMeters) {\n"
        "    return query(radiusMeters);\n"
        "  }\n"
        "};\n"
        "}\n"
    ),
    "guide.c": (
        "/* Finds attractions near the visitor. */\n"
        "int find_nearby_attractions(int radius_meters, char *city) {\n"
        "  return query(radius_meters);\n"
        "}\n"
    ),
    "guide.go": (
        "package tour\n"
        "// TourGuide guides tourists.\n"
        "type TourGuide struct{ Max int }\n"
        "// FindNearbyAttractions finds attractions near the visitor.\n"
        "// Ordered by distance.\n"
        "func (g *TourGuide) FindNearbyAttractions(radiusMeters int) []Site {\n"
        "  return query(radiusMeters)\n"
        "}\n"
    ),
    "guide.rs": (
        "/// Guides tourists.\n"
        "pub struct TourGuide { max: i32 }\n"
        "impl TourGuide {\n"
        "    /// Finds attractions near the visitor.\n"
        "    /// Ordered by distance.\n"
        "    pub fn find_nearby_attractions(&self, radius_meters: i32) -> Vec<Site> {\n"
        "        self.query(radius_meters)\n"
        "    }\n"
        "}\n"
    ),
}

#: filename -> the method every language fixture defines, in that language's
#: naming convention.
EXPECTED_METHOD = {
    "Guide.java": "findNearbyAttractions",
    "Guide.cs": "FindNearbyAttractions",
    "guide.py": "find_nearby_attractions",
    "guide.cpp": "findNearbyAttractions",
    "guide.c": "find_nearby_attractions",
    "guide.go": "FindNearbyAttractions",
    "guide.rs": "find_nearby_attractions",
}


@pytest.fixture(scope="module")
def parsed(tmp_path_factory):
    """Parse every fixture once; return (nodes, docs, enclosing, documents)."""
    root = tmp_path_factory.mktemp("multi") / "code"
    root.mkdir(parents=True)
    for name, body in SOURCES.items():
        (root / name).write_text(body, encoding="utf-8")

    nodes, docs = parse_repo(root)
    enclosing = enclosing_class_map(nodes)
    documents = build_all_documents(nodes, enclosing, docs)
    return nodes, docs, enclosing, dict(zip(nodes, documents, strict=True))


def _nodes_in(nodes, filename):
    return [n for n in nodes if n.file_path == filename]


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_every_spec_suffix_maps_back_to_that_spec(self):
        for suffix, spec in SUFFIX_TO_SPEC.items():
            assert suffix in spec.suffixes

    def test_supported_suffixes_covers_every_fixture(self):
        for name in SOURCES:
            assert spec_for_path(name) is not None, f"{name} has no spec"
            assert "." + name.rsplit(".", 1)[1] in SUPPORTED_SUFFIXES

    def test_unknown_suffix_is_none_not_an_error(self):
        # A real repository is full of files we cannot parse. Walking one must
        # not be a minefield.
        assert spec_for_path("README.md") is None
        assert spec_for_path("Cargo.lock") is None

    def test_unknown_language_name_raises_with_a_useful_message(self):
        with pytest.raises(KeyError, match="Known:"):
            spec_for_language("cobol")

    def test_stopwords_are_language_specific(self):
        # `def` is noise in Python and a real word nowhere else; `synchronized`
        # is noise in Java and not a Python keyword at all.
        assert "def" in stopwords_for_path("x.py")
        assert "def" not in stopwords_for_path("X.java")
        assert "synchronized" in stopwords_for_path("X.java")

    def test_unknown_suffix_still_gets_common_stopwords(self):
        assert "return" in stopwords_for_path("x.unknown")


# ---------------------------------------------------------------------------
# Parsing, per behaviour
# ---------------------------------------------------------------------------


class TestParsesEveryLanguage:
    @pytest.mark.parametrize("filename", sorted(SOURCES))
    def test_finds_the_method(self, parsed, filename):
        nodes, _, _, _ = parsed
        names = {n.name for n in _nodes_in(nodes, filename) if n.kind == "method"}
        assert EXPECTED_METHOD[filename] in names

    @pytest.mark.parametrize("filename", sorted(SOURCES))
    def test_line_numbers_are_1_based_and_ordered(self, parsed, filename):
        nodes, _, _, _ = parsed
        for node in _nodes_in(nodes, filename):
            assert node.start_line >= 1
            assert node.end_line >= node.start_line

    @pytest.mark.parametrize("filename", sorted(SOURCES))
    def test_the_method_carries_its_doc_comment(self, parsed, filename):
        nodes, docs, _, _ = parsed
        method = next(
            n for n in _nodes_in(nodes, filename) if n.name == EXPECTED_METHOD[filename]
        )
        assert "attractions near the visitor" in docs.get(method.node_id, "").lower()

    @pytest.mark.parametrize("filename", sorted(SOURCES))
    def test_parameter_names_reach_the_node_document(self, parsed, filename):
        # Parameter names carry real domain vocabulary; losing them is the
        # quietest possible way for a new language spec to be wrong.
        nodes, _, _, documents = parsed
        method = next(
            n for n in _nodes_in(nodes, filename) if n.name == EXPECTED_METHOD[filename]
        )
        assert "radius" in documents[method]
        assert "meters" in documents[method]


class TestDocCommentRuns:
    """Languages that write docs as consecutive line comments must keep them all."""

    @pytest.mark.parametrize("filename", ["Guide.cs", "guide.go", "guide.rs"])
    def test_multi_line_doc_is_collected_whole(self, parsed, filename):
        nodes, docs, _, _ = parsed
        method = next(
            n for n in _nodes_in(nodes, filename) if n.name == EXPECTED_METHOD[filename]
        )
        doc = docs[method.node_id].lower()
        assert "attractions near the visitor" in doc
        assert "ordered by distance" in doc, "the second doc line was dropped"

    def test_java_keeps_the_nearest_comment_only(self, parsed):
        # Parity guard. Java must NOT collect runs -- doing so changes 27 of 933
        # eTour docs and moves every published number. See LanguageSpec
        # .doc_comment_run.
        from src.parse.languages import JAVA

        assert JAVA.doc_comment_run is False


class TestPythonDocstrings:
    """Python is why `LanguageSpec` exists rather than a suffix switch."""

    def test_class_gets_its_own_docstring(self, parsed):
        nodes, docs, _, _ = parsed
        klass = next(n for n in _nodes_in(nodes, "guide.py") if n.kind == "class")
        assert "guides tourists around a city" in docs[klass.node_id].lower()

    def test_module_docstring_does_not_leak_onto_the_class(self, parsed):
        # The module docstring is the *preceding sibling* of the class, so a
        # sibling-based strategy attaches the wrong doc rather than no doc.
        nodes, docs, _, _ = parsed
        klass = next(n for n in _nodes_in(nodes, "guide.py") if n.kind == "class")
        assert "must not leak" not in docs[klass.node_id].lower()

    def test_method_docstring_is_taken_from_inside_the_body(self, parsed):
        nodes, docs, _, _ = parsed
        method = next(
            n
            for n in _nodes_in(nodes, "guide.py")
            if n.name == "find_nearby_attractions"
        )
        assert "find attractions near the visitor" in docs[method.node_id].lower()


class TestEnclosingScope:
    @pytest.mark.parametrize(
        "filename", ["Guide.java", "Guide.cs", "guide.py", "guide.cpp", "guide.rs"]
    )
    def test_method_reports_its_enclosing_type(self, parsed, filename):
        nodes, _, enclosing, _ = parsed
        method = next(
            n for n in _nodes_in(nodes, filename) if n.name == EXPECTED_METHOD[filename]
        )
        assert enclosing[method.node_id] == "TourGuide"

    def test_rust_impl_block_supplies_the_enclosing_name(self, parsed):
        # `impl TourGuide { ... }` has no name field of its own -- the type it
        # implements is in `.type`, which is what `name_strategy="type_field"`
        # resolves.
        nodes, _, enclosing, _ = parsed
        method = next(
            n
            for n in _nodes_in(nodes, "guide.rs")
            if n.name == "find_nearby_attractions"
        )
        assert enclosing[method.node_id] == "TourGuide"

    @pytest.mark.xfail(
        reason="Go associates a method with its type via the receiver "
        "`(g *TourGuide)`, not by line containment. ROADMAP A5.",
        strict=True,
    )
    def test_go_method_reports_its_receiver_type(self, parsed):
        nodes, _, enclosing, _ = parsed
        method = next(
            n for n in _nodes_in(nodes, "guide.go") if n.name == "FindNearbyAttractions"
        )
        assert enclosing[method.node_id] == "TourGuide"


class TestCDeclarators:
    def test_pointer_return_does_not_swallow_the_name(self, parsed):
        # `std::vector<Site> *findNearbyAttractions(...)` nests the identifier
        # under a pointer_declarator inside a function_declarator.
        nodes, _, _, _ = parsed
        names = {n.name for n in _nodes_in(nodes, "guide.cpp")}
        assert "findNearbyAttractions" in names

    def test_c_function_signature_includes_parameters(self, parsed):
        nodes, _, _, _ = parsed
        method = next(
            n
            for n in _nodes_in(nodes, "guide.c")
            if n.name == "find_nearby_attractions"
        )
        assert "radius_meters" in method.signature


# ---------------------------------------------------------------------------
# The fragment bug -- regression guard
# ---------------------------------------------------------------------------


class TestNoWordFragments:
    """`node.text` never starts with `node.signature`, so it must never be sliced.

    The old implementation did `node.text[len(node.signature):]`, which
    mis-sliced 1210 of 1210 eTour nodes and injected 185 distinct word fragments
    ("ourist" from Tourist, 64 times). This guards the fix.
    """

    # The premise of the bug, stated precisely. `signature` is *reconstructed*
    # ("int getX()") while `text` is raw source ("public int getX()"), so the
    # two coincide only when a declaration carries no modifiers at all. Plain C
    # often does coincide; Java, C# and Rust essentially never do. That is why
    # the fix excludes signature words rather than slicing by length -- the
    # slice is not merely wrong sometimes, it is unpredictable.
    @pytest.mark.parametrize(
        "filename", ["Guide.java", "Guide.cs", "guide.py", "guide.rs"]
    )
    def test_signature_is_not_a_prefix_of_text_when_modifiers_exist(
        self, parsed, filename
    ):
        nodes, _, _, _ = parsed
        methods = [n for n in _nodes_in(nodes, filename) if n.kind == "method"]
        assert methods
        assert any(not n.text.startswith(n.signature) for n in methods), (
            "expected the reconstructed signature to diverge from raw source "
            "here; if it no longer does, the positional slice may have become "
            "valid for this language and the fix needs revisiting"
        )

    def test_no_fragment_of_a_domain_word_appears(self, parsed):
        nodes, _, _, documents = parsed
        for node, document in documents.items():
            words = set(document.split())
            # "ourist"/"ttractions" style fragments: a suffix of a real word
            # that is itself present. Cheap, and catches the exact failure.
            for whole in ("tourist", "attractions", "radius", "meters"):
                for cut in range(1, 4):
                    assert whole[cut:] not in words, (
                        f"fragment {whole[cut:]!r} of {whole!r} in {node.node_id}"
                    )

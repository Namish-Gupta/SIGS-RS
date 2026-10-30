"""Per-language parsing rules -- the contract that makes this multi-language.

This module is the second frozen interface in the project, alongside
`src/contracts.py`. Track A (reach) produces `LanguageSpec` values; the generic
walker in `parse/parser.py` and the document builder in `index/node_doc.py`
consume them. Like `contracts.py`, it should not change unilaterally.

WHY A SPEC OBJECT RATHER THAN A SUFFIX SWITCH
---------------------------------------------
The obvious implementation is `if suffix == ".py": ... elif suffix == ".cs": ...`
inside the parser. That collapses the moment you look at what the grammars
actually disagree about. Measured, not assumed -- every row below was read off a
real parse tree:

    concern         java      c#        python    c/c++     go        rust
    return type     .type     .returns  (none)    .type     .result   .return_type
    name            .name     .name     .name     declrtr   .name     .name
    container name  .name     .name     .name     .name     type_spec .type (impl)
    doc location    prev sib  prev sib  IN BODY   prev sib  prev sib  prev sib
    comment node    block_..  comment   (n/a)     comment   comment   line_comment

Python is the case that settles the argument. Its docstring is the first
*statement inside the function body*, not a preceding sibling -- and worse, a
module-level docstring IS the preceding sibling of the first class, so a
sibling-based strategy does not merely miss the doc, it attaches the wrong one.
No amount of suffix-switching makes that safe; it needs a different algorithm,
selected by data.

WHAT IS DELIBERATELY NOT HERE
-----------------------------
`CodeNode` gains no `language` field. Language is derivable from the file
suffix via `spec_for_path`, and `src/contracts.py` is frozen by agreement
between both authors -- so a detail only the parser and indexer need does not
get to widen the shared contract. Same reasoning as `docs` being returned
alongside `CodeNode` rather than stored on it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from src.contracts import NodeKind

# --------------------------------------------------------------------------
# Strategy enums
# --------------------------------------------------------------------------

#: How to find a declaration's name.
#:
#:   field        -- `node.child_by_field_name(name_field)`. The common case.
#:   declarator   -- C/C++. The name is buried inside a `function_declarator`,
#:                   which may itself be wrapped in pointer/reference
#:                   declarators, so we descend the `declarator` chain until we
#:                   run out. `int *get_thing(void)` puts `get_thing` two levels
#:                   down.
#:   child_field  -- Go `type_declaration`, which has no name of its own: the
#:                   name lives on a child `type_spec`.
#:   type_field   -- Rust `impl_item`. `impl Guide { ... }` names the type it is
#:                   implementing in `.type`, and that type name is exactly the
#:                   "enclosing class" a method inside it should report.
NameStrategy = Literal["field", "declarator", "child_field", "type_field"]

#: How to find the doc comment attached to a declaration.
#:
#:   preceding_sibling  -- Javadoc, XML doc, `///`, `/** */`. Walk backwards over
#:                         modifiers/annotations/attributes, then collect the
#:                         contiguous run of comment nodes.
#:   leading_body_string -- Python. The first statement inside the body, if it is
#:                         a bare string expression.
DocStrategy = Literal["preceding_sibling", "leading_body_string"]


# --------------------------------------------------------------------------
# Language-specific stopwords
# --------------------------------------------------------------------------
#
# Reserved words carry no domain meaning and, being uniformly distributed across
# every node in a corpus, dilute every vector by the same amount. They must be
# per-language: `def` is noise in Python and a real word nowhere else, while
# `int` is noise in Java and C but is not a Python keyword at all.
#
# These are unioned with the shared `COMMON_STOPWORDS` below, never used alone.

#: Words that are noise regardless of language -- generic type names and the
#: universal control-flow vocabulary that survives in every C-family grammar.
COMMON_STOPWORDS = frozenset(
    """
    if else for while do return break continue switch case default
    true false null none nil new delete this self super
    class struct enum interface trait impl namespace package import include
    public private protected internal static final const let var
    try catch finally throw throws raise except
    void int long short float double bool boolean char string str object
    type types value values result results item items obj tmp temp
    get set is has
    """.split()
)

JAVA_KEYWORDS = frozenset(
    """
    abstract assert byte extends goto implements instanceof native record
    strictfp synchronized transient volatile yield override deprecated
    exception
    """.split()
)

CSHARP_KEYWORDS = frozenset(
    """
    abstract as async await base checked decimal delegate event explicit extern
    fixed foreach implicit in lock operator out override params readonly ref
    sbyte sealed sizeof stackalloc typeof uint ulong unchecked unsafe ushort
    using virtual when where yield partial nameof
    """.split()
)

PYTHON_KEYWORDS = frozenset(
    """
    def lambda pass global nonlocal assert del elif yield with as from and or
    not in async await print len range dict list tuple set frozenset args kwargs
    """.split()
)

C_KEYWORDS = frozenset(
    """
    auto extern goto register signed sizeof typedef union unsigned inline
    restrict size_t uint8 uint16 uint32 uint64 int8 int16 int32 int64
    """.split()
)

CPP_KEYWORDS = C_KEYWORDS | frozenset(
    """
    constexpr consteval decltype explicit friend mutable operator template
    typename virtual noexcept nullptr std vector map unique ptr shared
    """.split()
)

GO_KEYWORDS = frozenset(
    """
    func go defer chan select range map make len cap append panic recover
    fallthrough err error ok string8 rune byte uint uintptr iota
    """.split()
)

RUST_KEYWORDS = frozenset(
    """
    fn mut pub use crate mod match move ref unsafe where dyn box option some
    ok err vec string str usize isize u8 u16 u32 u64 i8 i16 i32 i64 f32 f64
    self_ into from clone copy debug derive
    """.split()
)


# --------------------------------------------------------------------------
# The spec
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class LanguageSpec:
    """Everything the generic walker needs to know about one language.

    Frozen, like every other contract in this project: a spec is a description
    of a grammar, not a scratch variable, and freezing makes it hashable so it
    can be cached per suffix.

    Attributes:
        name: Short identifier ("java", "python"). Used in messages and as the
            registry key.
        suffixes: File extensions this spec claims. A suffix maps to exactly one
            spec -- `.h` is assigned to C++ rather than C, because C++ headers
            are far more common in the wild and the C grammar chokes on classes
            while the C++ grammar parses plain C fine.
        grammar_module: The `tree_sitter_*` module to import. Imported lazily so
            that a missing optional grammar degrades to "this language is
            unavailable" rather than breaking every import in the project.
        node_types: tree-sitter node type -> `NodeKind`. This is the whitelist:
            anything not named here is walked through but never emitted.
        containers: Node types that establish an enclosing scope name.
        comment_types: Node types that count as comments for doc extraction.
        doc_strategy: See `DocStrategy`.
        name_strategy: See `NameStrategy`.
        name_field: Field to read when `name_strategy == "field"`.
        name_child_type: Child node type to descend into for `"child_field"`.
        params_field: Field holding the parameter list.
        return_fields: Field names to try, in order, for the return type. Tried
            in order because some grammars use different fields for different
            declaration types.
        doc_skip_types: Node types to walk backwards over when looking for a
            preceding doc comment -- modifiers, annotations, attributes. These
            sit between the comment and the declaration keyword.
        doc_comment_run: Whether to collect a *contiguous run* of preceding
            comments rather than only the nearest one. See below -- this is a
            measured accuracy tradeoff, not a formatting preference.
        stopwords: Reserved words to drop from node documents.

    On `doc_comment_run`. Languages that write docs as consecutive line comments
    (`///` in C# and Rust, `//` in Go and C) put each line in its own node, so
    taking only the nearest one keeps a single line of a paragraph. Block-comment
    languages do not have that problem, and for them the run is actively harmful:
    measured on eTour, enabling it changes 27 of 933 Java docs, and the changes
    go both ways --

        Banner.java     better: recovers "This class intercepts mouse events and
                        then / Makes the frame below blocked", of which the
                        single-comment rule kept only the second line.
        Category.java   worse:  drags in "// contains the maximum distances",
                        a trailing comment belonging to the *previous field
                        declaration*, and attaches it to a constructor.

    Net effect on the ablation was E1 +0.001 MAP, E2 -0.008. Mixed and small --
    which is exactly why it is a flag defaulting to off rather than a change
    folded into a refactor. Whether block-comment languages should opt in is an
    ablation row (ROADMAP B8), to be decided by measurement.
    """

    name: str
    suffixes: tuple[str, ...]
    grammar_module: str
    node_types: Mapping[str, NodeKind]
    containers: frozenset[str]
    comment_types: frozenset[str]
    doc_strategy: DocStrategy = "preceding_sibling"
    name_strategy: NameStrategy = "field"
    name_field: str = "name"
    name_child_type: str = ""
    params_field: str = "parameters"
    return_fields: tuple[str, ...] = ("type",)
    doc_skip_types: frozenset[str] = field(default_factory=frozenset)
    doc_comment_run: bool = False
    stopwords: frozenset[str] = field(default_factory=frozenset)

    def __hash__(self) -> int:  # `node_types` is a dict, so hash on the key
        return hash(self.name)


# --------------------------------------------------------------------------
# The specs themselves
# --------------------------------------------------------------------------

JAVA = LanguageSpec(
    name="java",
    suffixes=(".java",),
    grammar_module="tree_sitter_java",
    # Fields and enums are deliberately excluded: a bare field declaration
    # carries almost no behavioural signal and would flood the index with
    # near-empty documents, hurting both precision and the orphan count.
    node_types={
        "method_declaration": "method",
        "constructor_declaration": "constructor",
        "class_declaration": "class",
        "interface_declaration": "interface",
    },
    containers=frozenset({"class_declaration", "interface_declaration"}),
    # Older grammars emit a single "comment" type; newer ones split line/block.
    comment_types=frozenset({"comment", "line_comment", "block_comment"}),
    doc_skip_types=frozenset({"modifiers", "annotation"}),
    stopwords=COMMON_STOPWORDS | JAVA_KEYWORDS,
)

CSHARP = LanguageSpec(
    name="csharp",
    suffixes=(".cs",),
    grammar_module="tree_sitter_c_sharp",
    node_types={
        "method_declaration": "method",
        "constructor_declaration": "constructor",
        "class_declaration": "class",
        "interface_declaration": "interface",
        "struct_declaration": "class",
        "record_declaration": "class",
    },
    containers=frozenset(
        {
            "class_declaration",
            "interface_declaration",
            "struct_declaration",
            "record_declaration",
        }
    ),
    comment_types=frozenset({"comment"}),
    # C# puts the return type on `.returns`, not `.type` -- the single most
    # likely thing to get wrong when porting the Java spec by eye.
    return_fields=("returns", "type"),
    doc_skip_types=frozenset({"modifier", "attribute_list"}),
    doc_comment_run=True,
    stopwords=COMMON_STOPWORDS | CSHARP_KEYWORDS,
)

PYTHON = LanguageSpec(
    name="python",
    suffixes=(".py", ".pyi"),
    grammar_module="tree_sitter_python",
    node_types={
        "function_definition": "method",
        "class_definition": "class",
    },
    containers=frozenset({"class_definition"}),
    # Python has no doc *comment* node -- `#` comments are not docs, and the
    # real doc is a string expression inside the body. So the comment set is
    # empty and `leading_body_string` does the work.
    comment_types=frozenset(),
    doc_strategy="leading_body_string",
    # No return-type field: annotations exist (`-> X`) but are optional and
    # carry far less domain vocabulary than the parameter names already do.
    return_fields=(),
    stopwords=COMMON_STOPWORDS | PYTHON_KEYWORDS,
)

C = LanguageSpec(
    name="c",
    suffixes=(".c",),
    grammar_module="tree_sitter_c",
    node_types={
        "function_definition": "method",
        "struct_specifier": "class",
    },
    containers=frozenset({"struct_specifier"}),
    comment_types=frozenset({"comment"}),
    # The name is inside a `function_declarator`, possibly wrapped in pointer
    # declarators. `struct_specifier` still uses a plain `.name`, which the
    # declarator walk falls back to.
    name_strategy="declarator",
    doc_comment_run=True,
    stopwords=COMMON_STOPWORDS | C_KEYWORDS,
)

CPP = LanguageSpec(
    name="cpp",
    # `.h` goes here, not to C: C++ headers vastly outnumber C ones in practice,
    # and the C++ grammar parses plain C correctly while the C grammar cannot
    # parse a class.
    suffixes=(".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx", ".h"),
    grammar_module="tree_sitter_cpp",
    node_types={
        "function_definition": "method",
        "class_specifier": "class",
        "struct_specifier": "class",
    },
    containers=frozenset({"class_specifier", "struct_specifier"}),
    comment_types=frozenset({"comment"}),
    name_strategy="declarator",
    doc_comment_run=True,
    stopwords=COMMON_STOPWORDS | CPP_KEYWORDS,
)

GO = LanguageSpec(
    name="go",
    suffixes=(".go",),
    grammar_module="tree_sitter_go",
    node_types={
        "function_declaration": "method",
        "method_declaration": "method",
        "type_declaration": "class",
    },
    containers=frozenset({"type_declaration"}),
    comment_types=frozenset({"comment"}),
    # `type_declaration` has no name of its own; it wraps a `type_spec` that
    # does. Functions and methods use a plain `.name`, so the strategy falls
    # back for them.
    name_strategy="child_field",
    name_child_type="type_spec",
    return_fields=("result",),
    doc_comment_run=True,
    stopwords=COMMON_STOPWORDS | GO_KEYWORDS,
)

RUST = LanguageSpec(
    name="rust",
    suffixes=(".rs",),
    grammar_module="tree_sitter_rust",
    node_types={
        "function_item": "method",
        "struct_item": "class",
        "trait_item": "interface",
        "enum_item": "class",
        # `impl Guide { ... }` groups methods exactly as a class body does, so
        # it is emitted as a `class` named after the type it implements. That is
        # what makes `enclosing_class_map` -- which works by line containment
        # over emitted class nodes -- report `Guide` for the methods inside,
        # without needing a Rust-specific scope mechanism.
        "impl_item": "class",
    },
    containers=frozenset({"impl_item", "trait_item", "struct_item"}),
    comment_types=frozenset({"line_comment", "block_comment", "doc_comment"}),
    name_strategy="type_field",
    return_fields=("return_type",),
    doc_skip_types=frozenset({"attribute_item", "visibility_modifier"}),
    doc_comment_run=True,
    stopwords=COMMON_STOPWORDS | RUST_KEYWORDS,
)


#: Every supported language, by short name.
SPECS: dict[str, LanguageSpec] = {
    spec.name: spec for spec in (JAVA, CSHARP, PYTHON, C, CPP, GO, RUST)
}

#: Suffix -> spec. Built from `SPECS` so a suffix can never drift from its spec.
SUFFIX_TO_SPEC: dict[str, LanguageSpec] = {
    suffix: spec for spec in SPECS.values() for suffix in spec.suffixes
}

#: Every suffix we know how to parse. Used by the repo walker as its default
#: file filter, so adding a language automatically widens the walk.
SUPPORTED_SUFFIXES: tuple[str, ...] = tuple(sorted(SUFFIX_TO_SPEC))


def spec_for_path(path: str | Path) -> LanguageSpec | None:
    """Return the `LanguageSpec` for a file, or None if unsupported.

    None rather than an exception: a real repository contains README files,
    lockfiles, and images, and walking one must not be a minefield. Callers skip
    what they cannot parse.
    """
    return SUFFIX_TO_SPEC.get(Path(path).suffix.lower())


def spec_for_language(name: str) -> LanguageSpec:
    """Look a spec up by short name, raising a helpful error if absent."""
    try:
        return SPECS[name]
    except KeyError:
        raise KeyError(
            f"Unknown language {name!r}. Known: {', '.join(sorted(SPECS))}"
        ) from None


def stopwords_for_path(path: str | Path) -> frozenset[str]:
    """Stopwords appropriate to a file's language, for `index/node_doc.py`.

    Falls back to the shared set for unknown suffixes, which is the safe
    direction: dropping a genuinely common word costs a little signal, whereas
    keeping every reserved word of an unrecognised language costs more.
    """
    spec = spec_for_path(path)
    return spec.stopwords if spec is not None else COMMON_STOPWORDS

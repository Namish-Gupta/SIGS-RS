"""Source -> `CodeNode` objects, for any language with a `LanguageSpec`.

This is the generic form of what `java_parser.py` did for Java alone. The walk,
the node-id convention, the signature format, and the doc-attachment rules are
all unchanged; what used to be module constants are now read off a
`LanguageSpec` (see `parse/languages.py`).

PARITY IS THE ACCEPTANCE CRITERION. This refactor is only correct if the
ablation numbers do not move -- B0 0.233 / E1 0.409 and the rest. A parser
change that silently alters Java parsing would invalidate every published
figure while leaving every test green, so "the numbers are identical" is the
test that actually matters here, and it runs in CI.

WHY tree-sitter, still: it is a pluggable parser family with one API across ~40
grammars. Adding a language means installing a grammar wheel and writing a spec,
not writing a parser. That claim was made in Review 1 and this module is where
it gets cashed in -- seven languages, one walk function.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterable, Sequence
from functools import cache
from pathlib import Path

import tree_sitter

from src.contracts import CodeNode
from src.ingest.text_io import read_text
from src.parse.languages import LanguageSpec, spec_for_path

#: How far to descend a C/C++ declarator chain before giving up. `int **f()` is
#: two levels; anything past a handful is pathological and not worth chasing.
_MAX_DECLARATOR_DEPTH = 8


class GrammarUnavailable(RuntimeError):
    """A language's tree-sitter grammar is not installed.

    Raised rather than swallowed because a silently-skipped language looks
    exactly like a language with no code in it -- you would get an empty index
    and no indication why.
    """


@cache
def _parser_for(spec: LanguageSpec) -> tree_sitter.Parser:
    """Build and cache one parser per language.

    Cached because `Language` construction is the expensive part and it is
    entirely stateless. Keyed on the spec, which hashes by name.

    A grammar wheel encodes an ABI version and a newer core can reject an older
    grammar, so a raised exception here is a version mismatch, not a bug in this
    file. Verified working combinations are recorded in `docs/ROADMAP.md`.
    """
    try:
        module = importlib.import_module(spec.grammar_module)
    except ModuleNotFoundError as exc:
        raise GrammarUnavailable(
            f"Grammar for {spec.name!r} is not installed. Install it with:\n"
            f"    python -m pip install {spec.grammar_module.replace('_', '-')}"
        ) from exc
    return tree_sitter.Parser(tree_sitter.Language(module.language()))


# --------------------------------------------------------------------------
# Text helpers
# --------------------------------------------------------------------------


def _node_text(node: tree_sitter.Node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _field_text(node: tree_sitter.Node, field: str, source: bytes) -> str:
    child = node.child_by_field_name(field)
    return _node_text(child, source) if child is not None else ""


# --------------------------------------------------------------------------
# Name resolution
# --------------------------------------------------------------------------


def _innermost_declarator(node: tree_sitter.Node) -> tree_sitter.Node:
    """Follow the `declarator` chain to its end.

    C and C++ bury the identifier under one declarator per level of
    pointer/reference/array: `int *get_thing(void)` nests
    `pointer_declarator -> function_declarator -> identifier`. Descending until
    there is no `declarator` field left lands on the identifier itself.
    """
    current = node
    for _ in range(_MAX_DECLARATOR_DEPTH):
        child = current.child_by_field_name("declarator")
        if child is None:
            return current
        current = child
    return current


def _declarator_with_parameters(node: tree_sitter.Node) -> tree_sitter.Node | None:
    """Find the declarator in the chain that actually carries a parameter list."""
    current = node
    for _ in range(_MAX_DECLARATOR_DEPTH):
        if current.child_by_field_name("parameters") is not None:
            return current
        child = current.child_by_field_name("declarator")
        if child is None:
            return None
        current = child
    return None


def _resolve_name(node: tree_sitter.Node, spec: LanguageSpec, source: bytes) -> str:
    """Extract a declaration's name, per the spec's strategy.

    Every strategy tries the plain `name` field first. That is not laziness: in
    each language the exotic strategy applies to *one* node type and the rest
    use a normal name field. Rust `impl_item` needs `.type` while `function_item`
    right beside it needs `.name`; C `function_definition` needs the declarator
    walk while `struct_specifier` needs `.name`. Falling back keeps one spec per
    language instead of one per node type.
    """
    direct = _field_text(node, spec.name_field, source)
    if direct:
        return direct

    if spec.name_strategy == "declarator":
        declarator = node.child_by_field_name("declarator")
        if declarator is not None:
            return _node_text(_innermost_declarator(declarator), source)

    elif spec.name_strategy == "type_field":
        # Rust `impl Guide { ... }` -- the type being implemented is the name
        # that its methods should report as their enclosing scope.
        return _field_text(node, "type", source)

    elif spec.name_strategy == "child_field":
        # Go `type_declaration` wraps a `type_spec` that holds the name.
        for child in node.named_children:
            if child.type == spec.name_child_type:
                name = _field_text(child, spec.name_field, source)
                if name:
                    return name
    return ""


# --------------------------------------------------------------------------
# Signature
# --------------------------------------------------------------------------


def _signature(node: tree_sitter.Node, spec: LanguageSpec, name: str, source: bytes):
    """Return type + name + parameter list, as source text.

    Parameter names are the point: they carry real domain vocabulary
    (`int idRefreshmentPoint`), which is exactly the signal `node_doc` mines.

    Format is `"<return> <name><params>"`, unchanged from the Java-only version.

    NOTE: this is a *reconstruction*, not a source slice -- modifiers,
    annotations and `throws` clauses are absent, so it is never a prefix of
    `CodeNode.text`. `node_doc` used to assume otherwise and slice the body by
    `len(signature)`; see the note in `build_node_document` for what that cost.
    """
    return_type = ""
    for field in spec.return_fields:
        return_type = _field_text(node, field, source)
        if return_type:
            break

    params = _field_text(node, spec.params_field, source)
    if not params and spec.name_strategy == "declarator":
        declarator = node.child_by_field_name("declarator")
        if declarator is not None:
            holder = _declarator_with_parameters(declarator)
            if holder is not None:
                params = _field_text(holder, "parameters", source)

    return " ".join(part for part in (return_type, name + params) if part).strip()


# --------------------------------------------------------------------------
# Doc comments
# --------------------------------------------------------------------------


def _preceding_doc(node: tree_sitter.Node, spec: LanguageSpec, source: bytes) -> str:
    """Collect the comment block immediately preceding a declaration.

    Javadoc, XML doc comments, and `///` runs are *preceding siblings* of the
    declaration, not children, so they never appear in `CodeNode.text`. They are
    the single strongest signal available for matching -- eTour has 864 English
    Javadoc blocks and they paraphrase the requirements almost directly.

    Modifiers, annotations, and attributes sit between the comment and the
    declaration keyword, so we walk back over them first.

    Whether to collect a contiguous *run* of comments or only the nearest one is
    per-language (`LanguageSpec.doc_comment_run`), because it is a measured
    accuracy tradeoff rather than a formatting detail -- see the long note on
    that field. Java stays on the nearest-comment rule so the published ablation
    numbers are unchanged by this refactor.
    """
    sibling = node.prev_sibling
    while sibling is not None and sibling.type in spec.doc_skip_types:
        sibling = sibling.prev_sibling

    if sibling is None or sibling.type not in spec.comment_types:
        return ""

    if not spec.doc_comment_run:
        return _node_text(sibling, source)

    collected: list[str] = []
    while sibling is not None and sibling.type in spec.comment_types:
        collected.append(_node_text(sibling, source))
        sibling = sibling.prev_sibling

    collected.reverse()
    return "\n".join(collected)


def _body_string_doc(node: tree_sitter.Node, spec: LanguageSpec, source: bytes) -> str:
    """Extract a Python docstring: the first statement inside the body.

    This is why `LanguageSpec` exists rather than a suffix switch. A Python
    module docstring is the *preceding sibling* of the first class in the file,
    so a sibling-based strategy would not merely miss the class's own docstring
    -- it would confidently attach the module's instead.
    """
    body = node.child_by_field_name("body")
    if body is None or body.named_child_count == 0:
        return ""
    first = body.named_child(0)
    if first.type != "expression_statement" or first.named_child_count == 0:
        return ""
    literal = first.named_child(0)
    if literal.type != "string":
        return ""
    return _node_text(literal, source)


def _extract_doc(node: tree_sitter.Node, spec: LanguageSpec, source: bytes) -> str:
    if spec.doc_strategy == "leading_body_string":
        return _body_string_doc(node, spec, source)
    return _preceding_doc(node, spec, source)


# --------------------------------------------------------------------------
# The walk
# --------------------------------------------------------------------------


def parse_file(
    path: str | Path, repo_root: str | Path
) -> tuple[list[CodeNode], dict[str, str]]:
    """Parse one source file into its constituent AST nodes.

    Language is selected from the file suffix via `spec_for_path`. An
    unsupported suffix yields no nodes rather than an error -- a real repository
    is full of files we cannot parse and walking one must not be a minefield.

    Args:
        path: Path to a source file.
        repo_root: Used to compute the repo-relative `CodeNode.file_path`. For
            eTour this yields a bare filename (`CulturalHeritage.java`), which is
            exactly the form the gold links use.

    Returns:
        `(nodes, docs)` -- one `CodeNode` per declaration of interest in source
        order, plus a `node_id -> raw doc comment` map. Docs are returned
        alongside rather than on the dataclass because `CodeNode` is a frozen
        contract shared with the other half of the project; adding a field to it
        would break that agreement for a detail only the indexer needs.
    """
    path = Path(path)
    spec = spec_for_path(path)
    if spec is None:
        return [], {}

    source = read_text(path).encode("utf-8")
    tree = _parser_for(spec).parse(source)

    try:
        file_path = str(path.resolve().relative_to(Path(repo_root).resolve()))
    except ValueError:
        file_path = path.name
    file_path = file_path.replace("\\", "/")

    nodes: list[CodeNode] = []
    docs: dict[str, str] = {}

    def visit(node: tree_sitter.Node) -> None:
        kind = spec.node_types.get(node.type)
        if kind is not None:
            name = _resolve_name(node, spec, source) or "<anonymous>"
            # tree-sitter rows are 0-based; CodeNode is 1-based.
            start_line = node.start_point[0] + 1
            end_line = node.end_point[0] + 1
            node_id = f"{file_path}::{name}#{start_line}"
            nodes.append(
                CodeNode(
                    node_id=node_id,
                    file_path=file_path,
                    kind=kind,
                    name=name,
                    signature=_signature(node, spec, name, source)
                    if kind in ("method", "constructor")
                    else name,
                    start_line=start_line,
                    end_line=end_line,
                    text=_node_text(node, source),
                )
            )
            doc = _extract_doc(node, spec, source)
            if doc:
                docs[node_id] = doc

        for child in node.children:
            visit(child)

    visit(tree.root_node)
    return nodes, docs


def parse_repo(
    repo_root: str | Path,
    suffix: str | None = None,
    suffixes: Iterable[str] | None = None,
) -> tuple[list[CodeNode], dict[str, str]]:
    """Parse an entire source tree. Returns `(nodes, docs)`.

    Args:
        repo_root: Directory to walk.
        suffix: Single extension, for callers that want one language. Kept for
            backwards compatibility with the Java-only signature.
        suffixes: Explicit set of extensions. Defaults to every supported
            language, so a mixed-language repository indexes in one pass.

    tree-sitter never raises on malformed input -- it produces ERROR nodes and
    keeps going -- so a file that fails to parse yields few or no nodes rather
    than an exception. Callers should sanity-check the returned count.
    """
    from src.ingest.repo_walker import walk_source_files

    if suffix is not None:
        suffixes = (suffix,)

    nodes: list[CodeNode] = []
    docs: dict[str, str] = {}
    for path in walk_source_files(repo_root, suffixes=suffixes):
        file_nodes, file_docs = parse_file(path, repo_root)
        nodes.extend(file_nodes)
        docs.update(file_docs)
    return nodes, docs


def enclosing_class_map(nodes: Sequence[CodeNode]) -> dict[str, str]:
    """Map each node_id to the name of its enclosing class-like scope.

    Derived by containment on line ranges rather than during the parse walk,
    because the walk emits nodes in a flat list and this keeps `parse_file`'s
    return type simple. Cheap: one pass per file, and files are small.
    """
    by_file: dict[str, list[CodeNode]] = {}
    for node in nodes:
        by_file.setdefault(node.file_path, []).append(node)

    result: dict[str, str] = {}
    for file_nodes in by_file.values():
        containers = [n for n in file_nodes if n.kind in ("class", "interface")]
        for node in file_nodes:
            best = ""
            best_span = None
            for container in containers:
                if container.node_id == node.node_id:
                    continue
                if (
                    container.start_line <= node.start_line
                    and container.end_line >= node.end_line
                ):
                    span = container.end_line - container.start_line
                    # Innermost enclosing container wins (smallest span).
                    if best_span is None or span < best_span:
                        best, best_span = container.name, span
            result[node.node_id] = best
    return result

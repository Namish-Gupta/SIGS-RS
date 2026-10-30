"""Turn a `CodeNode` into the text document that actually gets embedded.

This is the highest-leverage module in the project. Get it right and the rest
works; get it wrong and no amount of model tuning rescues it.

The key decision: we do NOT embed raw source code. Raw Java is mostly syntax --
braces, modifiers, type names, boilerplate -- and a sentence embedding model
trained on English prose has no idea what to do with it. The signal that
actually connects "the system shall notify the user of nearby attractions" to
`sendAlert()` is *the words inside the identifiers*, and those words are welded
together in camelCase where no tokenizer will find them.

So we synthesise a pseudo-English document per node from:

    1. The node name, split on camelCase/snake_case ("dispatchEvent" ->
       "dispatch event")
    2. The signature (parameter names carry real domain vocabulary)
    3. Attached comments / Javadoc (already English -- the strongest signal
       available, and eTour has 864 of them)
    4. Identifiers from the body, also split
    5. The enclosing class name, split

Identifier splitting is the single highest-impact trick here. It is what turns
an embedding model that has never seen Java into a usable code retriever, and it
is why this project can use a general-purpose sentence model rather than
needing a code-specific one.
"""

from __future__ import annotations

import re

from src.contracts import CodeNode

#: Splits an identifier into word-ish pieces.
#:
#: The four alternatives, in order, and why each is needed:
#:   [A-Z]+(?![a-z])  a run of capitals NOT followed by a lowercase letter --
#:                    this is the acronym case. "HTTPServer" must give
#:                    "HTTP" + "Server", never "H","T","T","P". The negative
#:                    lookahead is what stops the greedy run from eating the
#:                    "S" that belongs to the next word.
#:   [A-Z][a-z]*      a normal capitalised word ("Server")
#:   \d+[a-z]*        a number with an optional suffix, so "2nd" stays together
#:   [a-z]+           a plain lowercase run ("dispatch")
#:
#: Anything not matched (underscores, dots, punctuation) acts as a separator by
#: simply not being captured.
_WORD_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z]*|\d+[a-z]*|[a-z]+")

#: Java reserved words plus the boilerplate types that appear in nearly every
#: node. These are pure noise: they carry no domain meaning and, being uniformly
#: distributed, they dilute every vector by the same amount.
JAVA_STOPWORDS = frozenset(
    """
    abstract assert boolean break byte case catch char class const continue
    default do double else enum extends final finally float for goto if
    implements import instanceof int interface long native new package private
    protected public return short static strictfp super switch synchronized this
    throw throws transient try void volatile while var record yield
    true false null
    string object integer exception override
    """.split()
)

#: Cap on body identifier words. A 300-line method would otherwise produce a
#: vector dominated by whatever it happens to loop over. MiniLM truncates at 256
#: tokens anyway, so anything past this is discarded downstream regardless.
MAX_BODY_WORDS = 160


def split_identifier(identifier: str) -> list[str]:
    """Split a programming identifier into its English words, lowercased.

    Examples:
        ``dispatchEvent``      -> ``["dispatch", "event"]``
        ``send_alert``         -> ``["send", "alert"]``
        ``HTTPServerHandler``  -> ``["http", "server", "handler"]``
        ``getURLFor2ndUser``   -> ``["get", "url", "for", "2nd", "user"]``

    The acronym case is the one that catches people out: a naive split on every
    uppercase letter shreds ``HTTPServer`` into single letters and destroys the
    signal. See `_WORD_RE` for how the boundary is handled.
    """
    return [match.group(0).lower() for match in _WORD_RE.finditer(identifier)]


def clean_doc_comment(raw: str) -> str:
    """Strip Javadoc markup, leaving the prose.

    Drops the comment delimiters, the leading ``*`` on continuation lines, and
    any ``@tag`` line. The tag lines are dropped because in this corpus they are
    almost entirely fully-qualified package paths
    (``@See unisa.gps.etour.control.ManagerCulturalHeritage#clear``) -- structural
    noise that repeats across hundreds of nodes rather than domain vocabulary.
    """
    text = re.sub(r"^\s*/\*+|\*+/\s*$", " ", raw.strip())
    lines: list[str] = []
    for line in text.splitlines():
        line = re.sub(r"^\s*\*+\s?", "", line).strip()
        if not line or line.startswith("@"):
            continue
        lines.append(line)
    return " ".join(lines)


def _identifier_words(
    source: str, limit: int | None = None, exclude: set[str] | None = None
) -> list[str]:
    """Extract and split every identifier-like token from a blob of source.

    Order is preserved and duplicates are dropped: repetition inside a body is
    usually a loop variable, not emphasis, so counting it would skew the vector
    toward whatever the method iterates over.

    Args:
        source: Raw source text to mine.
        limit: Stop after this many words.
        exclude: Words already emitted by an earlier part of the document, so
            they are not repeated. This is how the body is prevented from
            re-stating the signature -- see `build_node_document`.
    """
    seen: set[str] = set(exclude) if exclude else set()
    words: list[str] = []
    for token in re.findall(r"[A-Za-z_$][A-Za-z0-9_$]*", source):
        for word in split_identifier(token):
            if len(word) < 2 or word in JAVA_STOPWORDS or word in seen:
                continue
            seen.add(word)
            words.append(word)
            if limit is not None and len(words) >= limit:
                return words
    return words


def build_node_document(
    node: CodeNode, enclosing_class: str = "", doc_comment: str = ""
) -> str:
    """Assemble the embeddable pseudo-English document for one node.

    Args:
        node: The parsed AST node.
        enclosing_class: Name of the containing class, for extra context.
        doc_comment: Raw Javadoc for this node, if any (from `parse_file`).

    Returns:
        A space-joined bag of words. Not grammatical English, and that is fine --
        sentence embedders degrade gracefully on keyword soup, and we are after
        topical similarity, not syntax.

    Ordering matters slightly: name and doc come first because MiniLM truncates
    at 256 tokens, so the most discriminating signal must survive truncation.

    Note we do NOT stem here. The baseline (TfidfVectorizer) applies its own
    preprocessing, and if the two pipelines normalised differently the B-vs-E
    comparison would stop being apples-to-apples and the ablation would lose its
    meaning.
    """
    parts: list[str] = []

    # 1. The name -- the most concentrated signal. Kept even if it repeats
    #    elsewhere, because repetition in a name is genuine emphasis.
    parts.extend(split_identifier(node.name))

    # 2. Enclosing class, for context ("BeanRefreshmentPoint" tells you the
    #    domain even when the method is called "get").
    if enclosing_class:
        parts.extend(split_identifier(enclosing_class))

    # 3. Javadoc prose -- already English, the strongest signal when present.
    if doc_comment:
        cleaned = clean_doc_comment(doc_comment)
        if cleaned:
            parts.extend(w for w in split_identifier(cleaned) if len(w) > 1)

    # 4. Signature: parameter names carry real domain vocabulary.
    signature_words = _identifier_words(node.signature)
    parts.extend(signature_words)

    # 5. Body identifiers, capped, excluding anything the signature already said.
    #
    # NOTE: this used to slice the body positionally --
    #     body = node.text[len(node.signature):]
    # -- on the assumption that `node.text` starts with `node.signature`. It
    # never does. `signature` is *reconstructed* by the parser ("int getX()"),
    # while `text` is the raw source ("public int getX()"), so the lengths do
    # not correspond and the slice cut the body at an arbitrary offset. Measured
    # on eTour it mis-sliced 1210 of 1210 nodes, discarding real body content and
    # injecting 185 distinct word fragments into the corpus -- "ourist" (from
    # Tourist) 64 times, "erence" 28, "ritage" 20.
    #
    # Excluding by word is what the positional slice was trying to approximate,
    # and unlike the slice it is correct by construction.
    parts.extend(
        _identifier_words(node.text, limit=MAX_BODY_WORDS, exclude=set(signature_words))
    )

    return " ".join(parts)


def build_all_documents(
    nodes: list[CodeNode],
    enclosing: dict[str, str] | None = None,
    docs: dict[str, str] | None = None,
) -> list[str]:
    """Build documents for every node, in the same order as `nodes`.

    Index alignment between this list and `nodes` is load-bearing -- the vector
    store returns row indices, and those are mapped straight back to nodes.
    """
    enclosing = enclosing or {}
    docs = docs or {}
    return [
        build_node_document(
            node,
            enclosing_class=enclosing.get(node.node_id, ""),
            doc_comment=docs.get(node.node_id, ""),
        )
        for node in nodes
    ]


def tokenize_requirement(text: str) -> list[str]:
    """Tokenise requirement prose the same way node identifiers are tokenised.

    Used for the Jaccard lexical signal in retrieve/scorer.py. Both sides must go
    through the same normalisation or the overlap measure is meaningless.
    """
    return _identifier_words(text)

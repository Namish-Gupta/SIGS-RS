# req2code

**Requirement-to-Code Traceability via AST-Aware Retrieval**

Recovering the lost mapping between what software was supposed to do and the
code that actually does it — by matching meaning rather than keywords, and by
tracing to individual methods rather than whole files.

> **Status: working prototype.** The full pipeline runs end to end on eTour —
> parse → index → retrieve → evaluate → justify — offline, in ~1.7s. It also
> runs live inside an editor via MCP, at 0.25ms per query. The parser covers
> seven languages; accuracy is measured on Java, the only corpus with an answer
> key. All numbers below are measured, reproducible from committed scripts, and
> include the negative results — including a defect we found in our own pipeline
> that made an earlier published figure too high.

---

## The problem

Every project starts with requirements and then spends years drifting from them.
The link between requirement #37 and the code satisfying it lives in someone's
head, and then that person leaves. That matters when you need to answer *"we're
changing this requirement — what code breaks?"*, *"what is this method for?"*,
or *"prove every safety requirement is implemented"* — an audit question with
legal weight in regulated domains.

The classic approach — TF-IDF over requirement text vs. source text — has a hard
ceiling, because the two describe the same behaviour in almost disjoint
vocabulary. A requirement says *"the system shall notify the user"*; the code
says `sendAlert()`. No keyword overlap, same idea. This is the **vocabulary
gap**, and it is what the whole project is aimed at.

## What this does

**1. Traces to AST nodes, not files.** Existing tools point at
`TourGuide.java`, a 600-line file. This points at
`TourGuide.findNearbyAttractions()` at lines 142–171. Each method, constructor,
and class is independently retrievable.

**2. Runs bidirectionally, and flags orphans.** As well as requirement → code, it
runs code → requirement and flags nodes that *no* requirement appears to claim —
dead code, undocumented features, or scope that crept in unrecorded. No standard
traceability toolkit reports them.

**3. Explains its traces.** Each link comes with a natural-language argument for
why that method satisfies that requirement, grounded in specific identifiers. A
ranked list of scores is not reviewable by a human; an argument is.

### How it works

The central trick is how code is represented. Raw source is a poor embedding
input — mostly syntax, in a vocabulary no English sentence model understands. So
instead of embedding source, we synthesise a *pseudo-English document* per node
from: the node name split on camelCase (`dispatchEvent` → "dispatch event"), the
signature including parameter names, attached Javadoc, body identifiers (also
split), and the enclosing class name.

**Identifier splitting is the highest-impact step in the pipeline.** It is what
bridges *"notify the user"* ↔ `sendAlert()`, and why a general-purpose sentence
model works here without a code-specific one.

Nodes are then scored against each requirement:

```
score = α · cosine(requirement, node_document) + β · jaccard(req_tokens, node_identifiers)
```

α weights the semantic signal, β the lexical one. The baseline starts at α=1,
β=0; adding β is a measured ablation, not a tuning exercise.

**Nothing is trained.** The embedding model (`all-MiniLM-L6-v2`) is pre-trained
and frozen — no training loop, no gradients, no GPU. With 58 requirements and
308 links there is nowhere near enough data to fine-tune without overfitting. The
accuracy work lives in text preparation, not in the model.

## Quickstart

> Day-to-day operation, tuning, and troubleshooting: **[docs/OPERATING.md](docs/OPERATING.md)**.

Requires **Python 3.13** and **git**. On Windows use the python.org interpreter
explicitly — bare `python` often resolves to the Store build, whose sandbox
breaks `torch` with a cryptic `WinError 126`.

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows;  source .venv/bin/activate on Unix
python -m pip install -r requirements.txt
python spikes/spike_treesitter.py    # expect: "OK - tree-sitter works, proceed"
```

`data/` is gitignored — the corpus is not redistributed here (see
[Dataset & attribution](#dataset--attribution)). Fetch it once:

```bash
git clone https://github.com/tobhey/finegrained-traceability.git ../finegrained-traceability
mkdir -p data && cp -r ../finegrained-traceability/datasets/etour data/
```

### Running

```bash
python -m scripts.run_demo        # the demo -- offline, ~1.7s
python -m scripts.run_ablation    # full evaluation, writes results/
python -m scripts.bench_latency   # interactive-latency benchmark
pytest                            # tests -- offline, no model, ~1.3s
pytest -m slow                    # + real-model integration tests
```

The first run downloads the model (~80 MB) into `models/`. Everything after is
fully offline.

## Results

Measured on eTour: 58 requirements, 1210 AST nodes across 116 files, 308 gold
links. Each row changes exactly one variable from the rows above it, so any gain
is attributable to a specific cause rather than to "the system".

| Run | Representation | Granularity | Isolates | MAP | P@5 | R@10 |
|-----|----------------|-------------|----------|-----|-----|------|
| B0 | TF-IDF | file | classic baseline | 0.233 | 0.263 | 0.398 |
| B1 | TF-IDF | AST node | does node granularity alone help? | 0.263 | 0.298 | 0.409 |
| E0 | embeddings | file | does semantics alone help? | 0.358 | 0.393 | 0.514 |
| E1 | embeddings | AST node | **our core method** | **0.398** | 0.421 | 0.547 |
| E2 | E1 + query expansion | AST node | does requirement rewriting help? | 0.397 | 0.411 | **0.588** |
| E3 | E2 + identifier overlap | AST node | does lexical signal add on top? | 0.397 | 0.411 | 0.581 |

**B0 → E1 is +0.165 MAP, a 71% relative improvement.** B1 and E0 are what make
that defensible: they separately rule out "the gain is just smaller chunks" and
"the gain is just embeddings".

The decomposition is the interesting part — granularity alone (B0→B1) is
**+0.030**, semantics alone (B0→E0) is **+0.125**, both together (B0→E1) is
**+0.165**. But 0.030 + 0.125 = 0.155, so the two effects are **more than
additive**: finer granularity gives the embedding model a cleaner unit to match
against, making the techniques complementary rather than redundant.

**E2 and E3 are honest negatives on the headline metric.** Both sit within
0.001 MAP of E1 while winning elsewhere — E2 the best recall (R@10 0.588).
Query expansion and lexical overlap help find links, but neither improves the
ranked-order quality MAP measures. Reported as measured, not filtered to the
flattering subset. (Note E2's "query expansion" is a mechanical stripper for
use-case boilerplate identical across all 58 requirements, not LLM rewriting —
see `retrieve/query_expansion.py`.)

### These numbers are lower than the ones we first published

An earlier revision of this table reported E1 MAP **0.409**. That figure was
inflated by a defect in `index/node_doc.py`, found while generalising the parser
to seven languages, and it is corrected here.

The document builder mined body identifiers from `node.text[len(node.signature):]`,
assuming the raw source begins with the signature. It does not: `signature` is
*reconstructed* by the parser (`int getX()`) while `text` is raw source
(`public int getX()`), so the lengths never correspond. On eTour the slice
landed mid-word in **1210 of 1210 nodes**, discarding real body content and
injecting 185 distinct word fragments into the corpus — `ourist` (from
*Tourist*) 64 times, `erence` 28, `ritage` 20.

Removing the fragments *lowers* MAP by 0.011, which is above the ~0.005 noise
floor on this corpus. That is a genuinely uncomfortable result and it is
reported rather than buried:

| Body extraction | E1 MAP | E1 P@5 | E1 R@10 |
|---|---|---|---|
| positional slice (defective, previously published) | 0.409 | 0.407 | 0.556 |
| **exclude signature words (correct, current)** | **0.398** | **0.421** | 0.547 |
| true body after the opening brace | 0.395 | 0.418 | 0.551 |

Note precision moves the other way: P@5 *improves* to 0.421. The fragments were
helping rank-order and hurting top-5 precision.

**Why corrupted text helped is unresolved.** The obvious explanation — that
fragments act as accidental repetition of domain-salient words, MiniLM's
subword tokeniser mapping `ourist` near `tourist` — predicts that repeating the
method name deliberately should recover the loss. Measured, it does not: name
×2 gives 0.387 and ×3 gives 0.384, both *worse*. So the mechanism is something
else, and characterising it is open work rather than a settled story.

The headline claim is unaffected in kind: B0 → E1 remains a large, decomposed
improvement over the classic baseline. The previous snapshot is preserved at
`docs/results/2026-07-30/` as the provenance for anything already quoted from
it.

### Fair-comparison note

Node-level runs retrieve a deep pool (20× the evaluated *k*), aggregate to file
level, then truncate to exactly *k* files — the same number file-level runs get.
Without this, 10 retrieved nodes collapse into only ~5.8 distinct files and
node-level runs are silently penalised on recall for reasons unrelated to
retrieval quality. See `NODE_FETCH_MULTIPLIER` in [ablation.py](src/eval/ablation.py).

## Editor integration

The ablation answers *"is it accurate?"*. `python -m scripts.bench_latency`
answers *"is it deployable?"* — measured on eTour:

| | Cost | Budget |
|---|---|---|
| Cold start (once, at server boot) | ~3–6 s, almost all model loading¹ | — |
| Requirement → ranked methods | **0.25 ms** | 100 ms |
| Method → ranked requirements | **0.02 ms** | 100 ms |
| Orphan scan, whole corpus | **0.34 ms** | 100 ms |
| New free-text query (embed + search) | **6.2 ms** | 100 ms |
| Re-index one edited file | **46 ms** | 100 ms |

¹ Dominated by loading the model off disk, so it swings with the OS page cache —
observed 3.3 s warm to 21.8 s fully cold. It is paid once, behind the editor's
own startup, which is exactly why the server is long-lived rather than
shelled out to per query.

100 ms is the threshold below which a response is perceived as instantaneous.
Every interactive path is inside it; boot is not and needn't be, since it is paid
once behind the editor's own startup. A full corpus rebuild is 2.6 s, so
per-file incremental re-indexing is ~57× cheaper — that is what makes live
re-indexing viable, and why the embedding cache is keyed **per node** rather than
per corpus.

`python -m scripts.mcp_server` exposes retrieval over the **Model Context
Protocol**, so one server reaches every MCP-speaking editor — Claude Code,
Cursor, Zed, Windsurf, Claude Desktop — rather than needing a VSCode extension
(which would miss Zed) plus a native Zed extension in Rust. Requires
`pip install mcp` (2.x); nothing else in the project imports it. Tools exposed:
`state_requirement`, `list_requirements`, `trace_requirement`, `search_code`,
`whose_requirement`, `find_orphans`, `justify_link`. `state_requirement` is the
primary path — the user describes behaviour, it is logged and traced in one call.

Every call first refreshes the index, so results never go stale. Where the target
is a git working tree that costs three read-only git commands whose price does
not grow with the repository; otherwise it is an mtime scan of every source file
(~9 ms on eTour). Either way it is polling on the read path rather than a
filesystem watcher, which cannot miss an event.

### Running on your own repository

Point it at any source tree. There is nothing to prepare — no requirement
folder, no answer key, no config:

```bash
python -m scripts.req2code --path /path/to/your/project index
```

**Requirements are stated, not read from a folder.** On a real project nobody
has a directory of numbered use-case documents, so the requirement set is built
by asking:

```bash
python -m scripts.req2code --path . state "the system shall notify the traveller about nearby attractions"
```

Each stated requirement is appended to `.req2code/requirements.jsonl` (gitignored,
local, never leaves the machine), so the tool accretes its own requirement corpus
as you use it. That is also what sharpens the orphan claim: an orphan stops
meaning "unclaimed by the corpus we shipped" and starts meaning *code that
nothing you have ever asked for touches*.

| Command | |
|---|---|
| `index` | build the index, report what was found |
| `state "<text>"` | state a requirement, log it, and trace it |
| `search "<text>"` | one-off query; does **not** log |
| `trace SR0001` | re-trace a stated requirement |
| `whose <file> <line>` | what is the method under this cursor for? |
| `orphans` | code no stated requirement claims |
| `watch` | keep the index live while you work |

Every query command takes `--json`, which is the shape the editor integration
consumes.

**The map follows the repository.** Where the target is a git working tree,
change detection is driven by git rather than by scanning: an edit, a new file, a
deletion, a commit, a branch switch and a revert all land in the index, each
costing one re-parse and one embed of the file that actually changed. Measured on
a scratch repo: 75 ms for an edit, 77 ms for a `git checkout` to a commit without
the method, 34 ms for an idle check that finds nothing. Git is only used to
decide *which files are worth looking at* — mtime still decides whether to
reindex — so a target that is not under version control falls back to the
filesystem walk and behaves exactly as before. Git is invoked through a
read-only, remote-free allowlist (`ingest/vcs.py`); nothing here can reach a
network.

Gold links remain an **evaluation** input, never a retrieval one — nothing in
`parse/`, `index/`, `retrieve/` or `justify/` reads them. A directory with
`req/*.txt` beside `code/` is still detected as a research corpus and behaves as
it always did, answer key or not; strict validation of the documented eTour shape
stays on for the bundled corpus, so the published figures keep their guard rails.
A research corpus is read-only — live requirements are refused there, so a
published number can never depend on local state that is not in the repository.

The same applies to the MCP server, which serves whatever `REQ2CODE_ROOT` points
at:

```bash
REQ2CODE_ROOT=/path/to/your/project python -m scripts.mcp_server
```

The full path from a source file to a ranked mapping — every function and what
it costs — is in **[docs/PIPELINE.md](docs/PIPELINE.md)**.

### VS Code extension

Download the `.vsix` from [Releases](https://github.com/Cygnus-27/req2code/releases)
and install it:

```bash
code --install-extension req2code-0.1.0.vsix
```

Or build it yourself from source:

```bash
python -m pip install mcp
python editors/vscode/build_vsix.py
code --install-extension editors/vscode/req2code-0.1.0.vsix
```

Zero npm dependencies and no build step — the extension is plain JavaScript, and
`build_vsix.py` zips it into an installable package without `vsce`. Set
`req2code.enginePath` to this checkout, open any repository, and the panel gives
you:

- **Trace** — state a requirement, get ranked methods, click to jump to the line
- **Method** — follows the cursor: which requirements does *this* method serve?
- **Orphans** — code nothing asks for, with the score distribution beside it
- **Requirements** — the session log, retrace or forget
- inline **CodeLens** labels naming the requirement above every method, and a
  status bar showing engine state and index size as it updates live

It is a client of the MCP server, not a second implementation — so the editor
and any AI client always see the same answers. Setup, settings and
troubleshooting: **[editors/vscode/req2code/README.md](editors/vscode/req2code/README.md)**.

## Limitations

Stated up front, because a prototype that hides its caveats is worth less than
one that names them.

- **Evaluation granularity ≠ retrieval granularity.** eTour gold links map
  requirements to *files*; we retrieve *methods*. Node scores are therefore
  max-aggregated to file level for scoring (a file scores as well as its best
  node), keeping numbers comparable with the baseline and published results.
  Node-level output is reported qualitatively, in the demo. This is a limitation
  of the evaluation, not the method — and the absence of node-level gold data is
  precisely why finer-grained traceability is under-studied.
- **Seven languages parsed, one evaluated.** The parser handles Java, C#,
  Python, C, C++, Go and Rust (see `parse/languages.py`), but every accuracy
  number on this page is measured on Java alone, because eTour is the only
  corpus with an answer key. Retrieval demonstrably *runs* on the other six;
  it is not yet shown to be equally *accurate* on them. A second labelled
  corpus is what would close that gap.
- **Small corpus.** ~58 requirements, ~116 artifacts, ~308 links. Enough to
  compare configurations, too small for claims about industrial codebases.
- **Workspace mode is demonstrated, not measured.** Running on an arbitrary
  repository works and returns plausible answers — dogfooded on this project,
  where *"detect which files changed since the last check, using version
  control"* returns `diff_names()`, `Corpus._git_candidates()` and
  `changed_since()` as its top three. But there is no answer key for an
  arbitrary repository, so there is no MAP for it, and the failures are visible
  too: a vaguely worded requirement (*"requirements must be saved locally so
  they survive a restart"*) ranks `store_justification()` above
  `append_requirement()`. Scores are honest about it — 0.52 for the good trace,
  0.33 for the bad one — but "the score is low when it is wrong" is an
  observation, not a calibration. Measuring this without labels is open work;
  the promising direction is mining `(docstring, node)` pairs from the target
  repository as free positives.
- **Latency is measured at eTour's scale, not a real one.** The live-update
  figures (75 ms per edit, 34 ms idle) come from a scratch repository. The idle
  cost is dominated by Windows subprocess spawn — three `git` calls — and has
  not been re-measured at 10k+ nodes. That is roadmap A10 and it is still open.
- **The orphan threshold is uncalibrated**, and more so in workspace mode, where
  the requirement set is whatever the user has stated so far — early on, almost
  everything is legitimately unclaimed. The 0.30 default flags 311 of 995
  methods (31%) on eTour — far too many to review. The 5th percentile of the observed
  distribution (0.17) flags 49 (5%), which is reviewable, but that is an
  observation rather than a principled cutoff. The demo prints the full
  distribution so a reader can pick their own. Calibrating it is open work.
- **Most orphans are boring.** In any real codebase most unclaimed nodes are
  getters, logging, and framework glue that legitimately implement no
  requirement — eTour's most-orphaned method is `getFont()`. The claim is that
  this surfaces a small reviewable set, not that everything flagged is a defect.
- **One requirement has no gold links.** eTour ships 58 use cases but only 57
  appear in the answer set — UC37 ("Logout") was never linked by the original
  annotators. It is excluded from MAP rather than scored 0.0, which would drag
  every configuration down by the same constant and break comparability.
- **Justifications are not yet evaluated.** They are generated and cached, but
  scoring them against human rationale is future work. The committed cache was
  authored by Claude in the session that built the pipeline rather than through
  the API script; each entry records its own provenance in a `model` field.
- **Italian-language datasets excluded.** SMOS, eAnci, and Albergate were
  originally Italian; translation artifacts would confound the vocabulary-gap
  analysis the method rests on.

## Dataset & attribution

Uses the **eTour** dataset, obtained via the
[finegrained-traceability](https://github.com/tobhey/finegrained-traceability)
(FTLR) repository by Tobias Hey et al. eTour originates with the **Center of
Excellence for Software & Systems Traceability (CoEST)**. Full credit for the
corpus belongs to its original authors and to the FTLR authors for the cleaned,
packaged form.

> **We use their data only — never their code.** FTLR is **GPL-3.0**; this
> project is **Apache-2.0**. Copying their source here would create a license
> conflict, so we do not. `data/` is gitignored, so no part of their corpus
> enters this repository's history.

See [NOTICE](NOTICE) for the full attribution statement.

## Project layout

```
src/
  contracts.py   ← frozen data contracts; everything depends on these
  ingest/        workspace layout detection, session requirement log, git change
                 oracle, requirements loader, repo walker
  parse/         languages.py (per-language specs) + parser.py (one walk, 7 langs)
  index/         node-document builder, per-node embedding cache, vector store
  retrieve/      req→code, code→req, orphans, cursor→node, scorer, query expansion
  justify/       LLM prompt + committed cache/
  eval/          gold loader, metrics, TF-IDF baseline, ablation runner
  pipeline.py    corpus loading + incremental re-index + live requirements
  api.py         the JSON shapes every front end speaks -- defined once
  cli.py         the command line
scripts/         req2code (CLI), run_demo, run_ablation, bench_latency,
                 mcp_server, snapshot_results
editors/vscode/  the VS Code extension (plain JS, no dependencies) + build_vsix
spikes/          throwaway learning scripts (not imported by src/)
tests/
docs/            PIPELINE.md (how a mapping is made), OPERATING.md, ROADMAP.md,
                 notes/ (one per roadmap item, written for the other author)
```

`src/contracts.py` defines `Requirement`, `CodeNode`, and the results CSV schema.
`src/parse/languages.py` is the second frozen interface, added when the parser
went multi-language: it is what the walker and the indexer agree on, so a new
language is a spec rather than a code change.
These are frozen by agreement between both authors: freezing the interface is
what lets the two halves of the project be built in parallel without blocking on
each other. It is also why the retrieval engine and the presentation layer were
never coupled — the MCP server is a client of that contract, not of internals.

## Non-functional requirements

- **Offline** — models and LLM outputs cached locally; no network calls at demo
  or serve time (the MCP server pins `HF_HUB_OFFLINE`, which also cuts boot from
  15 s to 3.5 s)
- **Reproducible** — pinned dependencies, fixed seeds, single entry-point scripts
- **Fast** — demo under 60s; every interactive query under 100ms
- **Traceable** — every number maps to a committed script and a logged config

## License

Apache-2.0. See [LICENSE](LICENSE).

# req2code — Roadmap to the final product

Written after the first assessment was approved. This document is the agreement
between the two authors about *what is left*, *who owns it*, and *how each of us
stays able to explain the other's half*.

It is deliberately in the repository rather than in a chat log, for the same
reason `src/contracts.py` is: an agreement that is not written down is not an
agreement.

---

## 1. What changed after Review 1

Review 1 validated the research claim. The next assessment asks three things
Review 1 did not:

1. **Numbers against what people actually use.** Not just "we beat our own
   TF-IDF baseline" — beat the methods the traceability literature and industry
   actually use, and show the difference is statistically real rather than noise
   on 57 requirements.
2. **A product that works anywhere.** Java-only, on a bundled academic corpus,
   is a study. The deliverable is a tool that runs on *your own editor, on your
   own repository, in your own language*, in real time.
3. **A live demo.** Fully built, testable, changeable — not a script that prints
   a table.

### The reframe that matters most

**Requirements are stated live by the user, not read from a folder.**

This is the central design decision and it changes the architecture. The product
flow is:

```
user types a requirement in English  ->  ranked methods that implement it
```

That is the `search_code` path (embed one query, search the matrix), **not** the
`trace_requirement` path (look up a pre-embedded requirement from a corpus).

`data/etour/req/*.txt` and the gold links do not disappear — they become
*exclusively the evaluation harness*. Their job is to prove, with MAP and
recall, that the live path returns the right code. They are never a runtime
input on a real repository.

**Consequence to solve:** orphan detection and code→requirement both need a
*set* of requirements to score against, and a fresh repository has none. The
answer is a **session requirement log** — every requirement you state is
appended to a local, gitignored store, so the tool accretes its own requirement
corpus as you use it. "Orphan" then means *code that nothing you have ever asked
for touches*, which is a more honest definition than the current one anyway.

---

## 2. Verified starting state

Re-verified from a clean environment on 2026-08-20, after rebuilding the venv:

| Check | Result |
|---|---|
| `pytest -q` | 75 passed, 0.80s |
| `pytest -m slow` | 2 passed (real model loads) |
| `python -m scripts.run_ablation` | 2.1s, all six runs reproduce README numbers exactly |
| `python -m scripts.run_demo` | 1.8s, fully offline |

Grammar wheels for every target language install with no C toolchain and parse
cleanly against tree-sitter core 0.26.0:

| Grammar | Version | ABI | Status |
|---|---|---|---|
| java | 0.23.5 | 14 | in use |
| c_sharp | 0.23.5 | 15 | verified |
| python | 0.25.0 | 15 | verified |
| cpp | 0.23.4 | 14 | verified |
| c | 0.24.2 | 15 | verified |
| go | 0.25.0 | 15 | verified |
| rust | 0.24.2 | 15 | verified |

---

## 3. The two tracks

The work divides into two halves of comparable difficulty and comparable size,
along a seam that already exists in the codebase.

**Track A — Reach.** Make it work on any repository, in any language, live.
Mostly `parse/`, `ingest/`, `scripts/`. Difficulty is *breadth*: seven grammars
with genuinely different shapes, real-world filesystem mess, latency at scale.

**Track B — Rigour.** Make every claim survive an examiner. Mostly `eval/`,
`retrieve/orphans.py`, `justify/`. Difficulty is *depth*: information-retrieval
baselines, statistical inference, evaluation design.

Neither track is the easy one. They fail in different ways: Track A fails by
crashing on a real repo, Track B fails by making a claim that does not hold.

### Phase 0 — Joint foundation (both, before either track starts)

Nothing in either track can start until these are frozen, exactly as
`contracts.py` was frozen before Review 1.

| # | Item | Why joint |
|---|---|---|
| 0.1 | `LanguageSpec` contract | **Done.** `src/parse/languages.py` |
| 0.2 | Session-requirement-log format | **Done, needs Atharv's signature.** `src/ingest/requirement_store.py`; shape in `docs/notes/A6-A8-workspace-mode.md`. Track A writes it, Track B evaluates against it |
| 0.3 | Extend `contracts.py` with a `language` field on `CodeNode` | **Rejected.** Language is derivable from the suffix via `spec_for_path`, so it does not earn a place in a frozen contract. Reasoning in `languages.py` |

### Track A — Reach

| # | Item | Notes |
|---|---|---|
| A1 | `LanguageSpec` registry; refactor `java_parser.py` → generic `parser.py` | **Regression gate: the ablation numbers must not move.** If MAP changes, the refactor changed Java behaviour |
| A2 | Python support | The hard one — docstrings live *inside* the body, not as a preceding sibling |
| A3 | C# support | Structurally closest to Java; `///` XML doc comments |
| A4 | C and C++ support | Free functions, namespaces, header/impl split |
| A5 | Go and Rust support | Clean grammars; `///` and `//` doc conventions |
| A6 | Workspace mode | **Done.** Arbitrary repo root via `ingest/workspace.py`; `.gitignore`-awareness comes from `git ls-files` rather than a hand-written ignore parser. `SKIP_DIRS`, size caps and suffix filters still apply on top |
| A7 | Live requirement flow | **Done.** `state_requirement(text)` over MCP, `req2code state` on the CLI |
| A8 | Session requirement store | **Done.** Append-only JSONL at `.req2code/requirements.jsonl`; deletion is a tombstone, not a rewrite. Format frozen — see 0.2 |
| A9 | Language-aware `node_doc` | **Still open.** `node_doc.py` hard-codes `JAVA_STOPWORDS` while `languages.py` already exposes `stopwords_for_path`. Silently degrades the six non-Java languages |
| A10 | Scale benchmark | Re-run `bench_latency` on a 10k+ node real repository. The "real time" claim is currently proven only at 1210 nodes. Attack the ~34 ms idle refresh first: it is three git subprocesses, and the HEAD check can become a stat of `.git/HEAD` |
| A11 | Git-driven change detection | **Done, unplanned.** `ingest/vcs.py`. Fell out of A6: the mtime walk is O(files) per query and does not survive a real checkout. Git supplies candidates; mtime still decides |

### Track B — Rigour

| # | Item | Notes |
|---|---|---|
| B1 | Per-requirement AP export | Everything below needs per-query scores, not just the mean |
| B2 | **BM25 baseline** | The modern IR standard. Stronger than TF-IDF and the obvious "why didn't you try..." objection |
| B3 | **LSI/LSA baseline** | The *classic* traceability baseline (Marcus & Maletic, ICSE 2003). This is the "widely used approach" a reviewer will name |
| B4 | Significance testing | Paired bootstrap + Wilcoxon vs E1, with confidence intervals. Turns "+0.176" into "+0.176, p<0.01" |
| B5 | Orphan threshold calibration | Derive the cutoff from gold instead of guessing 0.30; report precision/recall of the flagging itself |
| B6 | Call-graph structural expansion (E4) | `data/etour/etour_method_callgraph.json` **is in the repo and read by nothing.** Propagate score along call edges |
| B7 | Justification evaluation | Rubric + blind scoring + inter-rater agreement. Currently the one claim with no evidence at all |
| B8 | `node_doc` ablations | The six ranked ideas in OPERATING.md §7, each as its own row |
| B9 | Code-aware model row (E5) | CodeBERT / GraphCodeBERT vs MiniLM |
| B10 | CI | GitHub Actions: lint, tests, **and an ablation-numbers regression guard** |

### Phase 3 — Product, split

| Owner | Item |
|---|---|
| A | CLI (`req2code index/trace/search/orphans`), packaging, console entry point |
| A | MCP hardening + per-editor setup docs (Claude Code, Cursor, Zed, VS Code) |
| B | Results snapshots, report-ready tables, scale numbers |
| Both | The live demo itself |

### Phase 4 — Report, split by section

Each author writes up the sections they built. Neither writes up work they did
not do — that is how the explain-back rule below stays honest.

---

## 4. Suggested assignment (swappable)

**Anant → Track A. Atharv → Track B.**

Rationale, stated plainly: Atharv wrote `eval/` and has the context to extend it
into statistics quickly, and Track A is the larger source of independent,
non-conflicting commits — which matters, because the history currently reads
15 commits to 1 and that gap can only be closed by future work.

If you would both rather swap, swap. The tracks are balanced on purpose so that
the choice is about interest, not about who gets the easier half.

---

## 5. How we both stay able to explain the whole thing

The stated goal is that when the halves come together, each of us understands
why the other's half works. Four rules make that automatic rather than
aspirational:

1. **Every PR is reviewed by the other person before merge.** Not a rubber
   stamp — the reviewer must be able to say what the change does.
2. **Every track item ships a `docs/notes/<id>-<topic>.md`**: half a page,
   written *for the other person*, answering "what did I build, why that way,
   what did I reject". This is not overhead — it is the raw material for the
   Phase 4 report, written while it is fresh instead of reconstructed at the end.
3. **Weekly explain-back.** Each of us explains the other's most recent item back
   to them. Anything you cannot explain is a note that needs rewriting.
4. **The contracts need both signatures.** `src/contracts.py` and `LanguageSpec`
   cannot change unilaterally. This is already the rule for `contracts.py`; it
   is what let the two halves be built in parallel before Review 1.

### The interlock

The two tracks are not independent, and that is deliberate:

- **A1's regression gate is B10's CI job.** If Track A's parser refactor
  silently changes Java parsing, Track B's ablation guard fails the build. Track
  A cannot break Track B's numbers without finding out immediately.
- **B5's calibrated threshold ships in A7's live flow.** Track B's research
  result becomes Track A's product default.
- **A2–A5's new languages are what B4's significance testing eventually runs
  on**, if a second corpus lands.

Neither half is finished until the other half can use it.

---

## 6. Git conventions

- Branches: `a/<topic>` and `b/<topic>`.
- PRs into `main`; both review; no direct pushes to `main`.
- When you genuinely pair, use `Co-Authored-By:` so the history says so.
- Result snapshots are committed via `scripts/snapshot_results.py`, never edited
  by hand. The one rule from `docs/README.md` still holds: **never edit a
  snapshot after taking it.**

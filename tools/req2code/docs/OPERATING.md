# req2code — Operating Guide

For someone opening this repo with no prior context. Read once, ~10 minutes.

---

## 1. What this project does

Software projects lose track of which code implements which requirement. This
tool recovers that mapping automatically. You give it (a) requirement documents
in English and (b) a codebase — Java, C#, Python, C, C++, Go or Rust; it tells
you, for each requirement, which **methods** most likely implement it — ranked,
with line numbers.

It does this by *meaning*, not keyword matching. A requirement saying *"notify
the user"* matches a method called `sendAlert()` even though they share no words.

Three things distinguish it from existing tools: it points at individual methods
rather than whole files, it runs backwards too (flagging code no requirement
asks for — "orphans"), and it explains its answers in English.

---

## 2. There is no training step — read this first

**Nothing in this project is trained.** This is the most common misunderstanding,
and it is worth being precise about because it will come up in review.

We use a **pre-trained, frozen** sentence-embedding model (`all-MiniLM-L6-v2`,
downloaded once, ~80 MB). It converts text into 384 numbers ("a vector") that
capture meaning. We never update its weights. There is no training loop, no
gradient descent, no epochs, no loss curve, no GPU.

The only `fit()` call in the entire codebase is `TfidfVectorizer.fit_transform`
in the baseline — and that just counts word frequencies across the corpus. It is
statistics, not learning.

**What actually happens** is *retrieval*:

1. Turn each requirement into a vector.
2. Turn each code method into a vector.
3. Compare every requirement vector to every method vector (cosine similarity).
4. Rank by score.

**Why no training is the right choice here:** we have 58 requirements and 308
gold links. That is nowhere near enough data to fine-tune a language model
without badly overfitting. A frozen model plus good text preparation is both more
honest and — on a corpus this size — more accurate. If asked "why didn't you
train it?", that is the answer.

The accuracy work happens in **how we prepare the text**, not in the model.

---

## 3. Setup (once per machine)

> **CRITICAL — use the full path to python.org 3.13, not bare `python`.** On
> Windows, bare `python` often resolves to the Microsoft Store Python, whose
> sandbox breaks `torch` with a cryptic `WinError 126` DLL error. Build the venv
> from the python.org interpreter explicitly:

```bash
cd <wherever you cloned this>
"%LOCALAPPDATA%\Programs\Python\Python313\python.exe" -m venv .venv
.venv\Scripts\activate
python --version                        # MUST say 3.13.x -- stop if it doesn't
python -m pip install -r requirements.txt
```

If `python --version` reports 3.12 (the Store Python), the venv is wrong. Delete
it (`rmdir /s /q .venv`, after closing any editor that has it open — see §11) and
recreate with the full path above.

> **If the venv already exists but its Python has moved or been removed** — you
> renamed your Windows user, moved the folder, or reinstalled Python — you do
> *not* need to reinstall the packages. A venv records an absolute path to its
> base interpreter in `.venv/pyvenv.cfg`, and only that pointer is broken:
>
> ```bash
> "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" -m venv --upgrade .venv
> ```
>
> This rewrites `pyvenv.cfg` and the launchers in place and keeps
> `site-packages` — seconds, instead of re-downloading torch.

Then fetch the dataset (not in the repo — see the README for why):

```bash
cd ..
git clone https://github.com/tobhey/finegrained-traceability.git
cd req2code
mkdir data
cp -r ../finegrained-traceability/datasets/eTour data/etour
```

**Every new terminal, activate the venv first:**

```bash
.venv\Scripts\activate
```

Your prompt gains a `(.venv)` prefix. If it doesn't, nothing else will work —
your machine has a second Python without these packages installed. Sanity check:

```bash
python -c "import torch, tree_sitter; print('venv OK')"
```

---

## 4. The commands

| Command | What it does | Time |
|---|---|---|
| `python -m scripts.run_demo` | The showcase. One requirement, all three claims. | ~1 s |
| `python -m scripts.run_ablation` | All six configurations, real numbers. | ~5 s |
| `python spikes/spike_split.py` | Inspect how one method becomes searchable text. | ~1 s |
| `python -m scripts.bench_latency` | Interactive-latency numbers (query, re-index). | ~5 s |
| `python -m scripts.mcp_server` | Serve retrieval to an MCP editor. Needs `pip install mcp`. Set `REQ2CODE_DATA_DIR` to point at another repo. | — |
| `pytest -q` | Contract + synthetic-repo tests. Offline, no model. | ~1 s |
| `pytest -m slow` | Real-model integration tests (excluded by default). | ~20 s |
| `ruff check . ; ruff format .` | Lint and auto-format. | <1 s |

First run downloads the model (~80 MB, one time). Everything after that is fully
offline — no internet, no API key.

---

## 5. Where results go

| Location | Tracked by git? | Purpose |
|---|---|---|
| `results/` | No | Scratch output, overwritten every run |
| `docs/results/<date>/` | **Yes** | Permanent record of numbers you have quoted |

To freeze the current numbers for your report:

```bash
python -m scripts.run_ablation
python -m scripts.snapshot_results --note "why this run matters"
git add docs/results && git commit -m "Snapshot results <date>"
```

**Never edit a snapshot.** If numbers change, take a new one — the old one is the
proof behind whatever you already wrote down.

`results/ablation.md` is the table to paste into the report.
`results/config.json` records the model, versions, timings, and every setting, so
any number can be traced back to the exact run that produced it.

---

## 6. Current results

| Run | Method | MAP | R@10 |
|---|---|---|---|
| B0 | TF-IDF, whole files (*the standard baseline*) | 0.233 | 0.398 |
| B1 | TF-IDF, methods | 0.263 | 0.409 |
| E0 | Embeddings, whole files | 0.358 | 0.514 |
| **E1** | **Embeddings, methods — our method** | **0.398** | 0.547 |
| E2 | E1 + requirement rewriting | 0.397 | **0.588** |
| E3 | E2 + word-overlap boost | 0.397 | 0.581 |

> **These are lower than the numbers first published** (E1 was 0.409). A defect
> in `node_doc.py` was injecting word fragments into every node document and
> inflating MAP by 0.011. It is fixed; the README's *"These numbers are lower
> than the ones we first published"* section has the full account. If you are
> holding a printout that says 0.409, it came from `docs/results/2026-07-30/`.

**MAP** (Mean Average Precision) rewards putting correct answers near the top of
the list — 0 is worst, 1 is perfect. **R@10** is what fraction of correct answers
appear in the top 10.

Read the table as an argument, not a leaderboard. Each row changes exactly one
thing from a row above, so any gain can be attributed to a specific cause:

- B0 → B1 (+0.030): smaller chunks help a little
- B0 → E0 (+0.125): understanding meaning helps a lot
- B0 → **E1 (+0.165)**: both together — *more than the sum*, so they reinforce
  each other

E2 and E3 sit within 0.001 MAP of E1 while E2 improves recall.
Reported as measured, not filtered to the flattering subset.

---

## 7. What to change to improve accuracy

All of it lives in text preparation and scoring. Ranked by likely payoff:

| # | Change | File | Why it might help |
|---|---|---|---|
| 1 | ~~Weight the method **name** more (repeat it 2–3×)~~ **Tried; it is worse.** | `src/index/node_doc.py` → `build_node_document` | Measured: name ×2 gives E1 MAP 0.387, ×3 gives 0.384, against 0.398 unweighted. Do not spend time here — the idea is refuted, which is itself a reportable negative result |
| 2 | Raise/lower `MAX_BODY_WORDS` (currently 160) | `src/index/node_doc.py` | Less body = less noise but less context. Try 80 and 240 |
| 3 | Expand the stopword sets with eTour-specific noise (`bean`, `db`, `manager`) | `src/parse/languages.py` → `JAVA_KEYWORDS` / `COMMON_STOPWORDS` | These appear almost everywhere, so they add nothing but dilute vectors. Stopwords moved here when the parser went multi-language — they are per-language now |
| 4 | Try `β = 0.15` and `β = 0.5` for the word-overlap term | `src/eval/ablation.py` → `E3` config | Currently 0.3. **Try at most two values** — heavy tuning reads as overfitting |
| 5 | Swap in a code-aware embedding model | `src/index/embedder.py` → `MODEL_NAME` | e.g. `microsoft/codebert-base`. A legitimate extra ablation row |
| 6 | Keep `@param` text in Javadoc instead of dropping it | `src/index/node_doc.py` → `clean_doc_comment` | Parameter descriptions carry domain words; currently discarded |

**Do not change** the aggregation rule (`max`) or `NODE_FETCH_MULTIPLIER` in
`src/eval/ablation.py` without reading the comments there first — they exist to
keep the node-vs-file comparison fair, and breaking them silently invalidates
every number in the table.

---

## 8. How to see what your change did

Three levels, cheapest first:

**Level 1 — did the text change?**
```bash
python spikes/spike_split.py
```
Prints the document for one method, stage by stage. Run before and after your
edit and compare. If this output didn't move, nothing downstream will.

**Level 2 — does it still work end to end?**
```bash
python -m scripts.run_demo
```
Look at section [4]: are the top-ranked methods still sensible, and still marked
`GOLD`?

**Level 3 — did accuracy move?**
```bash
python -m scripts.run_ablation
```
Compare the MAP column against §6 above. **A change is only real if MAP moves by
more than ~0.005** — smaller differences are noise on a 57-requirement corpus.

Then: `pytest -q ; ruff check .` and commit.

> **Useful signal:** the ablation normally takes ~5 s because embeddings are
> cached. If it suddenly takes ~15 s, the cache correctly detected that you
> changed the text pipeline and is recomputing. **That slowdown is proof your
> edit actually reached the vectors.** If you edited `node_doc.py` and it stayed
> fast, your change isn't being used — check you saved the file.

---

## 9. Vocabulary

| Term | Meaning |
|---|---|
| **AST node** | A syntactic unit of code — here, one method, constructor, or class |
| **Embedding / vector** | A list of 384 numbers representing a piece of text's meaning |
| **Cosine similarity** | How close two vectors point; 1 = identical meaning, 0 = unrelated |
| **Gold links** | The human-made correct answers used for grading (308 of them) |
| **MAP** | Mean Average Precision — rewards correct answers ranked near the top |
| **Ablation** | Turning one feature off at a time to prove which part causes the gain |
| **Baseline** | The standard existing method (TF-IDF) our approach must beat |
| **Orphan** | A method no requirement appears to ask for |
| **Corpus** | The whole dataset: 58 requirements + 116 Java files |
| **LanguageSpec** | Per-language parsing rules (`src/parse/languages.py`) — node types, doc placement, name resolution |

---

## 10. Known limitations (state these before you're asked)

- **Gold links are file-level; we retrieve methods.** Scores are aggregated up to
  file level for grading so the comparison is fair. Method-level output is shown
  qualitatively in the demo.
- **The orphan threshold is uncalibrated.** 0.30 flags 31% of methods — too many.
  The demo prints the full distribution so a reader can pick their own cutoff.
- **Seven languages parsed, one evaluated.** Java, C#, Python, C, C++, Go and
  Rust all parse; every accuracy number is Java, because eTour is the only
  corpus with an answer key.
- **UC37 has no gold links.** eTour ships 58 use cases but only 57 are graded.
- **Justifications aren't evaluated.** They're generated and cached; scoring them
  against human reasoning is future work.

---

## 11. When something breaks

| Symptom | Cause | Fix |
|---|---|---|
| `ModuleNotFoundError: torch` | venv not activated | `.venv\Scripts\activate` |
| `torch ... WinError 126 ... DLL` | venv built on Store Python 3.12 | Rebuild venv with the full python.org 3.13 path — see §3 |
| `python --version` says 3.12 in the venv | Same cause | Same fix — §3 |
| `Access denied` deleting `.venv` | An editor (VS Code) or a running Python has it open | Fully close the editor, then `rmdir /s /q .venv` |
| `FileNotFoundError: data/etour` | Dataset not fetched | See §3 |
| `Parsed N gold links, expected 308` | Dataset incomplete or wrong file | Re-copy `data/etour` |
| Demo very slow first run | Downloading the model | One-time; later runs are offline |
| Ablation stays fast after editing `node_doc.py` | Edit not saved / not reached | Re-save; confirm with `spike_split.py` |
| Numbers differ from §6 | You changed something | That's the point — snapshot and note it |

# A6–A8 — Workspace mode, the session requirement log, and git-driven updates

*Written for Atharv, per the rule in `docs/ROADMAP.md` §5.2. Half a page on what
I built, why that way, and what I rejected.*

---

## What was actually blocking us

The roadmap said the tool had to run "on your own repository". I assumed the
blocker was the gold links, and it was not — they were already optional and
nothing outside `eval/` reads them. The real blocker was the **corpus layout**:

```python
code_root = data_dir / "code"            # pipeline.py
load_requirements(data_dir / "req")      # raises FileNotFoundError if absent
```

Point the MCP server at a real project and it died before parsing a single file,
because a real project has no `req/` folder. That is the whole of what "Java-only
on a bundled academic corpus" was hiding.

## Three pieces

**`ingest/workspace.py` — layout detection.** Two shapes, told apart by looking:
*dataset* (`req/` **and** `code/` both present) or *workspace* (anything else,
where the directory *is* the source). No `--mode` flag, because the tool is
launched by an editor with the workspace root as its only argument and there is
nobody there to pass one. The sniff needs both directories, so a project that
happens to have a `code/` folder is still read as a workspace — the false
positive is the dangerous direction and it cannot happen by accident.

**`ingest/requirement_store.py` — the session log (0.2, the joint contract).**
JSONL at `<repo>/.req2code/requirements.jsonl`, append-only, one record per line:

```json
{"v": 1, "req_id": "SR0001", "text": "...", "created_at": "...", "source": "session"}
```

Append-only over a single JSON document because three processes may have this
open — CLI, editor, MCP server — and a crash mid-append costs the last line,
which the reader skips, rather than the whole log. **Deletion is a tombstone
line, not a rewrite.** I wrote the rewrite first and it was wrong twice: it can
lose everything if interrupted, and it lets `next_req_id` hand out the id of a
deleted requirement, so a stale reference resolves silently to a *different*
requirement. Both tested.

Prefix is `SR`, not `UC`: a stated requirement is not a use case from a spec
document, and output has to keep them distinguishable.

**`ingest/vcs.py` — git as the change oracle.** `refresh()` used to stat every
source file on every query. That is ~9ms on eTour's 116 files and unacceptable at
20k. Three constant-cost git commands replace the linear walk: `rev-parse HEAD`,
`diff --name-only`, `status --porcelain`.

The layering is the part worth arguing with me about: **git only supplies
candidates; mtime still decides.** If git is absent, lies, or over-reports, the
worst case is a redundant `stat` — there is no path where a git answer alone
mutates the index. That is also why the fallback walk is still there and still
tested.

Two subtleties that cost me time:

- **The union with the *previous* dirty set is load-bearing.** A file that was
  dirty and is now clean (undo, `git checkout -- file`) appears in *no* current
  listing: not in `status`, because it matches HEAD again; not in `diff`, because
  HEAD did not move. Without carrying the old dirty set forward, the index serves
  the edited version forever. `test_revert_is_observable` pins this.
- **`git ls-files` must only be used when the target *is* the working tree root.**
  `git_root()` walks upward, so asking it about `data/etour/code` answers with
  *req2code's* root — and `data/` is gitignored, so `ls-files` would correctly
  report none of eTour. A corpus checked out inside another repository has to
  keep being read as a directory of files.

When it does apply, `ls-files` gives us `.gitignore`-awareness for free, which is
A6's requirement without writing an ignore-file parser (precedence, negation,
nested files, `info/exclude`, the global file — getting any of it subtly wrong
means indexing `node_modules`).

## What this changes for your half

- `Corpus` gained `layout`, `git_root`, `git_state`. `contracts.py` is untouched
  — no signature needed.
- **Datasets have no session store.** `Layout.store_path` is `None` for a
  dataset, and `add_requirement` raises. eTour stays read-only so a published
  number can never depend on local state that is not in the repository. If you
  want live requirements against a labelled corpus for B-track work, that is a
  contract change and needs both signatures.
- `--json` on every CLI query command is the shape the extension will consume.
  It is exercised by `tests/test_cli.py` so it cannot drift silently.
- **I could not run the ablation regression gate** — `data/` is not on this
  machine. Nothing I touched should move a number (the parser import now goes to
  `parse.parser` instead of `parse.java_parser`, which is a re-export of the same
  functions; eTour still detects as a dataset and takes the same code path), but
  *should* is not *did*. Please run `python -m scripts.run_ablation` before this
  merges. That is B10's job and this is exactly the change it exists to catch.

## Rejected

- **A filesystem watcher.** Polling on the read path cannot miss an event; a
  watcher can, and then the index is quietly stale with no way to notice.
- **Caching `git_root`.** Would save ~11ms per fallback refresh. Caching a
  filesystem fact silently is how you get a bug that only reproduces after
  someone runs `git init`. Instead `_refresh_by_walk` skips the probe when we
  already established at load time that there is no repository.
- **Requiring a config file to point at a project.** Anything that needs setup
  before it does something is a tool people do not try.

## Measured

On a scratch git repo, real model, Windows:

| | |
|---|---|
| edit a file → mapping follows | 75 ms |
| `git checkout` to a commit without that method | 77 ms |
| back onto the branch | 100 ms |
| idle refresh, nothing changed | 34 ms |

The 34ms idle figure is worse than I would like and it is all Windows subprocess
spawn — three `git` invocations at ~11ms each. Still inside the 100ms budget, but
it is the number to attack first if A10's scale benchmark comes back tight. The
obvious fix is to skip the HEAD check when the mtime of `.git/HEAD` has not
moved, which trades one subprocess for one stat.

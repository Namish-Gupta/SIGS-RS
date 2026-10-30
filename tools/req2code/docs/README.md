# docs/

Committed, human-facing material: frozen result snapshots and report notes.

**New to this project? Start with [OPERATING.md](OPERATING.md)** — setup, the
commands, what to change to improve accuracy, and how to tell whether a change
did anything. It also explains why there is no training step.

## Why this exists alongside `results/`

| | `results/` | `docs/results/` |
|---|---|---|
| Tracked by git? | **No** — gitignored | **Yes** — committed |
| Written by | `python -m scripts.run_ablation` | `python -m scripts.snapshot_results` |
| Lifetime | Overwritten on every run | Permanent |
| Purpose | Working output | Provenance for numbers you have quoted |

`results/` is deliberately gitignored: it is regenerated output, and committing
it invites hand-editing a number until it says what you want. But a report needs
figures that stay put. If you write "MAP 0.409" in Review 1 and later change the
scorer, you must still be able to point at the exact run that produced 0.409.

So `results/` is the scratch pad and `docs/results/` is the record.

## Taking a snapshot

```bash
python -m scripts.run_ablation
python -m scripts.snapshot_results --note "baseline before query-expansion tuning"
git add docs/results && git commit -m "Snapshot results 2026-07-30"
```

Each snapshot is a dated directory holding:

- `ablation.md` — the table, pasteable into the report
- `config.json` — Python version, model, timings, every run config, all metrics
- `README.md` — auto-generated summary so the directory explains itself later

## The one rule

**Never edit a snapshot after taking it.** If the numbers change, take a new
one. The old snapshot is the provenance for whatever you already wrote down —
editing it destroys the audit trail this whole arrangement exists to provide.

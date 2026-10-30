# req2code in SIGS-RS

req2code is a development-time tool, not part of the gateway. It traces a
requirement (for example `REQ-API-2`) to the methods that implement it, and
flags code that no stated requirement claims. It is self-contained in this
folder: it has its own `requirements.txt`, nothing under `src/sigs_rs` imports
it, and the root `pytest` run does not collect it.

## Setup

Use a separate virtual environment. req2code needs `torch` and
`sentence-transformers`, which the gateway does not.

```bash
cd tools/req2code
python -m venv .venv
.venv\Scripts\activate          # Windows;  source .venv/bin/activate on Unix
python -m pip install -r requirements.txt
```

The first run downloads the embedding model (~80 MB) into `tools/req2code/models/`
(gitignored). Every run after that is offline.

## Use

Run from `tools/req2code`, pointing `--path` at the repository root:

```bash
python -m scripts.req2code --path ../.. index
python -m scripts.req2code --path ../.. search "The system shall drop any packet exceeding 50 packets per 10 seconds"
python -m scripts.req2code --path ../.. orphans
```

`state` records a requirement into `.req2code/` (gitignored); `search` does not.

## Checking the hand-written RTM

`docs/phase14/requirements-traceability-matrix.md` maps each `REQ-*` to the
files and symbols that implement it. `scripts/validate_sigs_rs_rtm.py` treats
that mapping as ground truth and scores req2code against it:

```bash
python scripts/validate_sigs_rs_rtm.py ../..
```

Measured on 21 requirements whose RTM entries name existing files:

| Metric | Result |
|---|---|
| A file the RTM names is in the top 5 | 16 / 21 (0.76) |
| A file the RTM names is in the top 10 | 18 / 21 (0.86) |
| The exact symbol the RTM names is in the top 5 | 9 / 21 (0.43) |
| File-level MRR | 0.487 |

Reading the numbers: file-level is the figure to trust. Symbol-level is strict,
because the RTM sometimes names a different method in the right file (`inspect`
where req2code returns `check`), which counts as a miss. Misses cluster on
cross-cutting requirements such as silent drop (`REQ-API-5`) and the payload
size gate (`REQ-TI-3`), where the behaviour is spread across several files.

## Limits

- Accuracy is measured on Java (eTour) in the req2code README; the SIGS-RS
  figures above are the first Python measurement and come from one repository.
- The RTM is written by the same people as the code, so agreement is evidence,
  not proof.
- req2code is Apache-2.0 (see `LICENSE` and `NOTICE` in this folder).

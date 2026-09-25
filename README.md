# schedules

Tools that prepare Indian ITR schedules for foreign (US-listed) equity — RSUs, stock
options and stocks. USD → INR uses SBI TT Buy rates from
[sbi-fx-ratekeeper](https://github.com/sahilgupta/sbi-fx-ratekeeper), cloned into
`sbi-fx-ratekeeper/` at the repo root on first run.

```
.
├── fa/                # Schedule FA — foreign assets, calendar year
│   ├── v1/            #   prices from yfinance
│   └── v2/            #   prices from a CSV you maintain (recommended)
├── cg/                # Schedule CG — capital gains, financial year
└── sbi-fx-ratekeeper/ # shared SBI rates (git-ignored)
```

| Schedule | Period | Rate date | Docs |
|---|---|---|---|
| FA | Jan 1 – Dec 31 | the event date (next available rate) | `fa/v1/README.md`, `fa/v2/README.md` |
| CG | Apr 1 – Mar 31 | last day of the previous month (Rule 115) | `cg/README.md` |

## Setup

```bash
poetry install              # or: python3 -m venv .venv && .venv/bin/pip install pandas yfinance requests pytest
```

## Run

```bash
.venv/bin/python fa/v2/main.py --year 2026
.venv/bin/python cg/main.py --fy 2026-27
.venv/bin/python -m pytest -q          # all tests
```

Each tool reads from its own `inputs/` and writes to `outputs/` (the FA tools also write
`logs/`); these folders are git-ignored so personal data is never committed.

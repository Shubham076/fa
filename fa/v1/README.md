# Schedule FA Generator — v1 (yfinance prices)

Generates the **Schedule FA** (foreign assets) table for the Indian ITR from your
US equity lots (RSUs, stock options, stocks). Prices are fetched from **yfinance**;
USD → INR uses the **SBI TT Buy** rate from
[sbi-fx-ratekeeper](https://github.com/sahilgupta/sbi-fx-ratekeeper) (cloned automatically
next to `v1/` and `v2/`).

> yfinance's `auto_adjust` dividend-adjusts historical prices. If you need
> split-only prices (usual Schedule FA convention), use **v2** with a prices CSV.

Schedule FA is reported per **calendar year** (Jan 1 – Dec 31).

## Folder layout

```
v1/
├── main.py      # Schedule FA generator
├── inputs/      # YOUR data — git-ignored, never committed
├── outputs/     # generated CSVs — git-ignored
├── logs/        # schedule_fa.log — git-ignored
└── tests/       # pytest suite (yfinance mocked, dummy data only)
```

## Files needed in `inputs/`

| File | Required | Purpose |
|---|---|---|
| `input.csv` | yes | One row per acquired lot (vest / purchase) |
| `sales.csv` | no | One row per sale, all years (used automatically if present) |

### `inputs/input.csv`

```csv
symbol,benefit_type,units,acquisition_date,acquisition_price,company_name,address,zip_code,nature,country,country_code,dividends_usd
ACME,RSU,100,2025-03-05,20.00,Acme Corp,1 Main Street Springfield CA,94000,Company,UNITED STATES OF AMERICA,2,0
```

- Required: `symbol` (a valid yfinance ticker), `units`, `acquisition_date (YYYY-MM-DD)`,
  `acquisition_price`, `company_name`, `address`, `zip_code`
- Optional: `benefit_type` (e.g. `SO`, `RSU` — sales only match lots of the same type),
  `nature`, `country`, `country_code`, `dividends_usd` (for the reporting year; blank = 0)
- Units held at the start / end of the year are derived from `sales.csv`.

### `inputs/sales.csv`

```csv
symbol,benefit_type,sale_date,units,sale_price
ACME,RSU,2025-09-15,40,24.00
```

- Optional: `acquisition_date` to sell from a specific lot; otherwise sales are matched
  **FIFO** (oldest lot first) among lots with the same `symbol` + `benefit_type` acquired on
  or before the sale date. Selling more than held stops the run with an error.

## How values are computed

| Schedule FA column | Formula |
|---|---|
| Initial value | lot acquired in the year: acquisition price × SBI rate on acquisition date; held from before: yfinance close on Jan 1 × SBI rate on Jan 1 — × units held at start of year |
| Peak value | highest yfinance intraday High in the year × SBI rate on that date × units held at start of year |
| Closing balance | yfinance close on Dec 31 × SBI rate on Dec 31 × units held at year end |
| Dividends | `dividends_usd` × SBI rate on Dec 31 |
| Gross proceeds | Σ (units × sale price × SBI rate on sale date) for sales in the year |

Lots acquired after the year, or fully sold before it, are skipped. Missing prices / SBI
rates on weekends or holidays roll forward to the next available date.

## Setup

```bash
python3 -m venv .venv            # from the repo root
.venv/bin/pip install pandas yfinance curl_cffi certifi pytest
```

## Run

From the repo root (paths default to `v1/inputs/…`):

```bash
.venv/bin/python v1/main.py --year 2025
.venv/bin/python v1/main.py --year 2026 --output output_2026.csv
.venv/bin/python v1/main.py --year 2025 --skip-update     # don't git pull SBI rates
```

Outputs:

- `outputs/output.csv` — Schedule FA table (paste into the ITR)
- `outputs/output_audit_trail.csv` — every price, rate, unit count and sale used
- `logs/schedule_fa.log` — step-by-step calculation log

### Audit trail columns

One row per lot, in the same order as `output.csv`:

| Column | Example |
|---|---|
| `Symbol`, `Type` | `S`, `RSU` |
| `Units` | `150 (carry forward)` — units held on Jan 1 (lots from earlier years are marked) |
| `Initial / Peak / Closing / Dividends Date` | `2026-03-02`, or `2026-01-01 (SBI date: 2026-01-02)` when that day had no SBI rate |
| `… Value (USD)` | `150 × $14.00 = $2,100.00` |
| `… Value (INR)` | `$2,100.00 × ₹83.00 (TT Buy) = ₹174,300.00` |
| `Sale Dates`, `Sale Value (USD/INR)` | one entry per sale, separated by ` \| `, with `→ total …` when there are several |

For carry-forward lots, `Initial Date` is Jan 1 (the yfinance close used), not the
acquisition date. `Closing Value` reads `Fully sold` when no units are left on Dec 31.

## Trying other values without committing them

Everything in `inputs/`, `outputs/` and `logs/` is git-ignored, so experiment freely:

```bash
cp v1/inputs/input.csv v1/inputs/input_test.csv          # edit the copy
.venv/bin/python v1/main.py v1/inputs/input_test.csv --year 2026 --output output_test.csv
```

Run `git status` before committing — no file under `inputs/` should ever appear.

## Tests

```bash
.venv/bin/python -m pytest v1/tests -q       # v1 only
.venv/bin/python -m pytest -q                # v1 + v2 (from repo root)
```

| File | Covers |
|---|---|
| `tests/test_core.py` | SBI rates, yfinance helpers (mocked), sales loading, CLI defaults, folders, logging |
| `tests/test_options.py` | Stock options: FIFO matching, sold / partially sold / skipped lots |
| `tests/test_rsu.py` | RSUs: held lots, carry-forward initial value, closing balance |
| `tests/test_dividends.py` | Dividend conversion and blank handling |

The tests never call yfinance or read `inputs/`.

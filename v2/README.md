# Schedule FA Generator — v2 (manual prices)

Generates the **Schedule FA** (foreign assets) table for the Indian ITR from your
US equity lots (RSUs, stock options, stocks). Prices come from a CSV you maintain
(e.g. Google Sheets `GOOGLEFINANCE`); USD → INR uses the **SBI TT Buy** rate from
[sbi-fx-ratekeeper](https://github.com/sahilgupta/sbi-fx-ratekeeper) (cloned automatically
next to `v1/` and `v2/`).

Schedule FA is reported per **calendar year** (Jan 1 – Dec 31).

## Folder layout

```
v2/
├── main.py            # Schedule FA generator
├── cash_balance.py    # cash peak/closing from an E*TRADE statement PDF
├── inputs/            # YOUR data — git-ignored, never committed
├── outputs/           # generated CSVs — git-ignored
├── logs/              # schedule_fa.log — git-ignored
└── tests/             # pytest suite (uses dummy data only)
```

## Files needed in `inputs/`

| File | Required | Purpose |
|---|---|---|
| `input.csv` | yes | One row per acquired lot (vest / purchase) |
| `prices.csv` | yes | Peak and Dec 31 closing price per symbol **per year** |
| `sales.csv` | no | One row per sale, all years (used automatically if present) |
| `expected_<year>.csv` | no | A filed Schedule FA output, used as a regression baseline by `tests/test_golden.py` |

### `inputs/input.csv`

```csv
symbol,benefit_type,units,acquisition_date,acquisition_price,company_name,address,zip_code,nature,country,country_code,dividends_usd
ACME,RSU,100,2025-03-05,20.00,Acme Corp,1 Main Street Springfield CA,94000,Company,UNITED STATES OF AMERICA,2,0
```

- Required: `symbol, units, acquisition_date (YYYY-MM-DD), acquisition_price, company_name, address, zip_code`
- Optional: `benefit_type` (e.g. `SO`, `RSU` — sales only match lots of the same type),
  `nature`, `country`, `country_code`, `dividends_usd` (for the reporting year; blank = 0)
- Units held at the start / end of the year are derived from `sales.csv`.

### `inputs/prices.csv`

```csv
symbol,year,peak_price,peak_date,closing_price
ACME,2025,25.00,2025-02-14,22.00
```

- One row per symbol per year; the row matching `--year` is used.
- `closing_price` may be blank if every lot of that symbol is sold by Dec 31.

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
| Initial value | acquisition price × SBI rate on acquisition date × units held at start of year |
| Peak value | `prices.csv` peak price × SBI rate on peak date × units held at start of year |
| Closing balance | `prices.csv` closing price × SBI rate on Dec 31 × units held at year end |
| Dividends | `dividends_usd` × SBI rate on Dec 31 |
| Gross proceeds | Σ (units × sale price × SBI rate on sale date) for sales in the year |

Lots acquired after the year, or fully sold before it, are skipped. If there's no SBI
rate on a date (weekend/holiday), the next available rate within 10 days is used.

## Setup

```bash
python3 -m venv .venv            # from the repo root
.venv/bin/pip install pandas pytest
```

## Run

From the repo root (paths default to `v2/inputs/…`):

```bash
.venv/bin/python v2/main.py --year 2025
.venv/bin/python v2/main.py --year 2026 --output output_2026.csv
.venv/bin/python v2/main.py --year 2025 --skip-update     # don't git pull SBI rates
```

Outputs:

- `outputs/output.csv` — Schedule FA table (paste into the ITR)
- `outputs/output_audit_trail.csv` — every price, rate, unit count and sale used
- `logs/schedule_fa.log` — step-by-step calculation log

## Trying other values without committing them

Everything in `inputs/`, `outputs/` and `logs/` is git-ignored, so experiment freely:

```bash
cp v2/inputs/input.csv v2/inputs/input_test.csv          # edit the copy
.venv/bin/python v2/main.py v2/inputs/input_test.csv \
    --sales v2/inputs/sales_test.csv --year 2026 --output output_test.csv
```

Run `git status` before committing — no file under `inputs/` should ever appear.

## Tests

```bash
.venv/bin/python -m pytest v2/tests -q       # v2 only
.venv/bin/python -m pytest -q                # v1 + v2 (from repo root)
```

| File | Covers |
|---|---|
| `tests/test_core.py` | SBI rates, prices/sales loading, CLI defaults, folders, logging |
| `tests/test_options.py` | Stock options: FIFO matching, sold / partially sold / skipped lots |
| `tests/test_rsu.py` | RSUs: held lots, closing balance, year-specific prices |
| `tests/test_dividends.py` | Dividend conversion and blank handling |
| `tests/test_golden.py` | Re-runs each `inputs/expected_<year>.csv` against your real data (skipped if absent) |

The unit tests use synthetic rates and dummy data only; they don't touch `inputs/`,
`outputs/` or `logs/`.

## `cash_balance.py`

Rebuilds the daily cash balance from an E*TRADE / Morgan Stanley client statement PDF to
find the true cash peak and closing balance:

```bash
.venv/bin/python v2/cash_balance.py /path/to/ClientStatements.pdf --year 2025 --inr
```

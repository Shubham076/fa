# Schedule CG Generator (foreign shares)

Generates the **Schedule CG** (capital gains) figures for the Indian ITR from sales of
US-listed shares (RSUs, exercised stock options, stocks). USD → INR uses the **SBI TT Buy**
rate from [sbi-fx-ratekeeper](https://github.com/sahilgupta/sbi-fx-ratekeeper), shared with
`fa/` at the repo root.

Schedule CG is reported per **financial year** (Apr 1 – Mar 31).

## Which part of Schedule CG this fills

| Holding period | Schedule CG item | Taxed at |
|---|---|---|
| 24 months or less | **A5** — "From sale of assets other than at A1 or A2 or A3 or A4 above" (short-term) | your slab rate |
| More than 24 months | **B8** — "From sale of assets where B1 to B7 above are not applicable" (long-term) | 12.5% without indexation (sales from 2024-07-23) |
| Both | **Table F** — "Information about accrual/receipt of capital gain": row 3 (short-term at applicable rates) and row 5 (long-term at 12.5%) | used for advance-tax interest |

Why these items and not the equity-share items:

- A1–A4 and B1–B7 cover land and buildings, **shares listed on an Indian stock exchange** where STT
  is paid (sections 111A / 112A), and special cases for non-residents and FIIs.
- US-listed shares are not listed in India and no STT is paid on them. For Indian tax they
  are **unlisted shares**, so they fall in the catch-all "other assets" items A5 and B8.

**Options and RSUs go in the same item.** What you sell is always a share of the same
foreign company. Whether you got it by exercising an option or when an RSU vested only
changes two inputs, not where it is reported:

| | Exercised options | RSUs | Shares bought yourself |
|---|---|---|---|
| `acquisition_date` | exercise date | vest date | purchase date |
| `cost_basis_usd` (cost of acquisition) | market value on exercise date (the value taxed as salary), not the strike price | market value on vest date (the value taxed as salary) | purchase price |
| Reported in | A5 / B8 | A5 / B8 | A5 / B8 |

The 24-month clock starts at exercise or vest, not at grant. An option that is exercised and
sold the same day (cashless exercise) is a short-term sale in A5, usually with a small gain
or loss from fees and rounding. Rows for different lots are added together into one A5 and one B8
entry.

## Rules applied

| Topic | Rule |
|---|---|
| Asset type | US-listed shares are **unlisted** for Indian tax → "sale of assets other than …" rows |
| Short vs long term | Long-term only if held **more than 24 months** (sale date > acquisition date + 24 months) |
| Full value of consideration | `proceeds_usd` × SBI TT Buy on the **last day of the month before the sale** (Rule 115) |
| Cost of acquisition | `cost_basis_usd` × SBI TT Buy on the **last day of the month before acquisition** |
| Expenditure on transfer | `fees_usd` × the sale's Rule 115 rate |
| No SBI rate on that day | The latest rate within the 10 days **before** it (never a later rate) |
| Cost of improvement, section 94(7)/(8) loss | Always 0 |

- **Options:** `acquisition_date` is the **exercise date**; `cost_basis_usd` is the market
  value on that date (the value your salary tax was based on), **not** the strike price
  (the broker's "Acquisition Cost").
- **RSUs:** `acquisition_date` is the **vest date**; `cost_basis_usd` is the market value on vest.
- Long-term gains are reported **without indexation** (12.5% regime for sales after
  2024-07-23). Earlier long-term sales log a warning.

## Folder layout

```
cg/
├── main.py      # Schedule CG generator
├── inputs/      # YOUR data — git-ignored, never committed
├── outputs/     # output log (form rows) and audit trail — git-ignored
└── tests/       # pytest suite (dummy data only)
```

## `inputs/sales.csv`

```csv
symbol,benefit_type,acquisition_date,sale_date,units,proceeds_usd,cost_basis_usd,fees_usd
ACME,SO,2026-04-13,2026-04-13,416,5287.25,5287.36,
ACME,RSU,2025-04-15,2026-04-20,10,200.00,150.00,
```

- Required: `symbol, acquisition_date, sale_date, units, proceeds_usd, cost_basis_usd`
  (dates as `YYYY-MM-DD`; amounts are USD **totals for the row**, copied from the broker's
  gains & losses report: `proceeds_usd` = "Total Proceeds", `cost_basis_usd` =
  "Adjusted Cost Basis", `acquisition_date` = "Date Acquired")
- Optional: `benefit_type`, `fees_usd` (blank = 0). Leave it blank when the broker's
  proceeds are already net of commission and fees, otherwise the fees count twice.
- Put all years in one file; only sales dated inside `--fy` are used.
- One row per lot, exactly as the report lists them (one order can appear as several lots).

## Running

From the repo root:

```bash
.venv/bin/python cg/main.py --fy 2026-27
.venv/bin/python cg/main.py --fy 2025-26 --skip-update           # don't git pull SBI rates
.venv/bin/python cg/main.py cg/inputs/sales_test.csv --fy 2026-27 --output cg_test.txt
```

`--fy` defaults to the most recently completed financial year.

## Outputs

The tool writes exactly two files to `outputs/`:

| File | Use |
|---|---|
| `cg_<fy>.txt` (or `--output`) | The run log: each sale, then the **Schedule CG rows** for A5 and B8 with values, Table F and the validation results. It is also printed to the console |
| `cg_<fy>_audit_trail.csv` (`<output>_audit_trail.csv`) | One row per sale with every rate and calculation, then a TOTAL row for A5 and one for B8 |

### Audit trail

One row per sale (paise kept):

| Column | Example |
|---|---|
| `Term` | `Short-term (0 days)` |
| `Acquisition Date` / `Sale Date` | `2026-04-13 (SBI date: 2026-03-31)`, or `2024-04-10 (SBI date: 2024-03-28, no rate on 2024-03-31)` |
| `Sale Value (USD)` | `$5,287.25 (416 @ $12.7097)` |
| `Sale Value (INR)` | `$5,287.25 × ₹93.15 (TT Buy) = ₹492,507.34` |
| `Gain (INR)` | `₹492,507.34 − ₹492,517.58 − ₹0.00 = −₹10.24` |

After a blank row come two **TOTAL** rows: `Schedule CG A5` (short-term) and `Schedule CG B8`
(long-term). They are always present, even if there were no sales of that kind. Amounts are
**whole rupees** (rounded half-up) with Indian digit grouping, as the portal shows them.

| TOTAL row column | Enter in Schedule CG (A5 / B8) |
|---|---|
| `Sale Value (INR)` | a(ii) Full value of consideration in respect of assets other than unquoted shares |
| `Cost (INR)` | b(i) Cost of acquisition without indexation |
| `Fees (INR)` | b(iii) Expenditure wholly and exclusively in connection with transfer |
| `Gain (INR)` | e, the capital gain (`-₹72` means a loss). The portal works out biv, c and e itself |
| `Quarter` | Table F row 3 (A5) or row 5 (B8), one amount per period |

Leave b(ii) Cost of improvement and d at 0. Many filers put US-listed shares in a(ii). If your
CA prefers a(i) "unquoted shares" (section 50CA), enter the same sale value in a(i) a, b and c
instead. The gain is the same either way.

**Table F:** the portal accepts no negative amounts, and each row must equal the gain after
set-off (Schedule BFLA). So the periods add up to the gain, or to 0 for a loss. A loss in one
period is set off against the same and later periods first. These rows only cover the sales
in this file: if you have other capital gains (for example Indian mutual funds), combine them
before filling Table F.

### Validation

Before writing, the tool checks the short-term and long-term totals, each with its Table F
row. The results go at the end of the output file with ✓ or ✗. Every check runs for both:

1. If nothing was received from the sale, no costs or expenses are claimed.
2. Total deductions = cost of acquisition + cost of improvement + transfer expenses.
3. Capital gain = full value – total deductions.
4. The five Table F periods add up to the capital gain, or to 0 for a loss.
5. Every amount is in whole rupees and no longer than 14 digits.
6. Only the capital gain can be negative, including in Table F.

Checks 1–4 come from the CBDT e-Filing ITR 2 Validation Rules for AY 2026-27. Checks 5 and 6
are the amount formats the portal accepts. If any check fails, both files are still written
and the command exits with an error.

### Schedule CG vs Schedule FA

The same sales appear in both, but the INR amounts differ on purpose: Schedule FA converts
proceeds at the SBI rate on the **sale date**, Schedule CG at the rate on the **last day
of the previous month** (Rule 115). CG also follows the financial year, FA the calendar year.

## Testing

```bash
.venv/bin/python -m pytest cg/tests -q
```

This tool is not tax advice — confirm the final figures with your CA.

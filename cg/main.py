#!/usr/bin/env python3
"""
Schedule CG Generator for Indian ITR — capital gains on sale of foreign (US-listed) shares.

Schedule CG is reported per Indian FINANCIAL YEAR (Apr 1 – Mar 31).

US-listed shares are "unlisted" for Indian tax purposes:
    - Short-term if held for not more than 24 months, otherwise long-term.
    - They go in the "sale of assets other than ..." rows of Schedule CG.

USD → INR follows Rule 115: SBI TT Buy rate on the last day of the month preceding
the month of the event (sale for proceeds and fees, acquisition for cost). If SBI
published no rate on that day, the latest rate within the 10 days before is used.

Usage (defaults read inputs/sales.csv). Writes two files to outputs/: the log with the
Schedule CG form rows (cg_<fy>.txt, or --output) and <output>_audit_trail.csv:
    python3 main.py --fy 2026-27
    python3 main.py inputs/sales_test.csv --fy 2026-27 --output cg_test.txt
    python3 main.py --fy 2025-26 --skip-update

sales.csv columns (one row per lot sold, any years; only sales in --fy are used):
    symbol, acquisition_date, sale_date, units, proceeds_usd, cost_basis_usd
    Optional: benefit_type, fees_usd (total for the row, default 0)

    acquisition_date : exercise date (options) or vest date (RSU)
    proceeds_usd     : TOTAL sale proceeds for the row — broker's "Total Proceeds"
    cost_basis_usd   : TOTAL market value on acquisition_date — broker's "Adjusted
                       Cost Basis" (the value salary tax was based on)
"""

import argparse
import logging
import re
import subprocess
import sys
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pandas as pd

# ─── Logging ──────────────────────────────────────────────────────────────────
log = logging.getLogger(__name__)


def setup_logging(output_txt: Path | None = None) -> None:
    """Log to the console and, once its path is known, to the output file."""
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if output_txt:
        handlers.append(logging.FileHandler(output_txt, mode="w", encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(message)s", handlers=handlers, force=True
    )


# ─── Global Config (overridden by CLI args) ───────────────────────────────────
# Schedule CG reports on the Indian FINANCIAL year (Apr 1 – Mar 31).
FY_START = datetime(2025, 4, 1)
FY_END = datetime(2026, 3, 31)

BASE_DIR = Path(__file__).parent
REPO_ROOT = BASE_DIR.parent
INPUTS_DIR = BASE_DIR / "inputs"
OUTPUTS_DIR = BASE_DIR / "outputs"
RATEKEEPER_REPO = "https://github.com/sahilgupta/sbi-fx-ratekeeper"
RATEKEEPER_DIR = REPO_ROOT / "sbi-fx-ratekeeper"

LONG_TERM_MONTHS = 24
RATE_LOOKBACK_DAYS = 10

# Cached SBI rate data by currency
_RATE_CACHE: dict[str, pd.DataFrame] = {}


# ─── SBI FX Ratekeeper ────────────────────────────────────────────────────────


def setup_ratekeeper(skip_update: bool = False) -> None:
    """Clone or pull the SBI FX Ratekeeper repo."""
    if not RATEKEEPER_DIR.exists():
        log.info("Cloning sbi-fx-ratekeeper (first run)…")
        subprocess.run(
            ["git", "clone", "--depth=1", RATEKEEPER_REPO, str(RATEKEEPER_DIR)],
            check=True,
        )
    elif not skip_update:
        log.info("Pulling latest sbi-fx-ratekeeper data…")
        try:
            subprocess.run(["git", "-C", str(RATEKEEPER_DIR), "pull"], check=True)
        except subprocess.CalledProcessError as exc:
            if (RATEKEEPER_DIR / "csv_files").exists():
                log.warning(
                    "Could not update sbi-fx-ratekeeper; using existing local data. "
                    "Run again with network access for the latest rates."
                )
            else:
                raise exc
    else:
        log.info("Skipping sbi-fx-ratekeeper update (--skip-update).")


def _find_rate_csv(currency: str = "USD") -> Path:
    csv_path = RATEKEEPER_DIR / "csv_files" / f"SBI_REFERENCE_RATES_{currency}.csv"
    if csv_path.exists():
        return csv_path
    matches = list(RATEKEEPER_DIR.rglob(f"SBI_REFERENCE_RATES_{currency}.csv"))
    if matches:
        return matches[0]
    raise FileNotFoundError(
        f"No SBI reference-rate CSV found for {currency} in {RATEKEEPER_DIR}."
    )


def _load_rates(currency: str = "USD") -> pd.DataFrame:
    currency = currency.upper()
    if currency in _RATE_CACHE:
        return _RATE_CACHE[currency]

    csv_path = _find_rate_csv(currency)
    rates = pd.read_csv(csv_path)
    rates.columns = rates.columns.str.strip().str.upper()

    required = {"DATE", "TT BUY"}
    missing = required - set(rates.columns)
    if missing:
        raise ValueError(
            f"{csv_path} is missing expected columns: {missing}. "
            f"Found columns: {list(rates.columns)}"
        )

    rates["DATE"] = pd.to_datetime(rates["DATE"]).dt.normalize()
    rates["TT BUY"] = pd.to_numeric(rates["TT BUY"], errors="coerce")
    rates = rates.dropna(subset=["DATE", "TT BUY"])
    rates = rates[rates["TT BUY"] > 0]
    rates = rates.sort_values("DATE").drop_duplicates("DATE", keep="last")

    if rates.empty:
        raise ValueError(f"No usable TT BUY rates found in {csv_path}.")

    log.info(
        f"Loaded {len(rates)} SBI {currency} TT BUY rates from {csv_path} "
        f"({rates['DATE'].min().date()} to {rates['DATE'].max().date()})"
    )

    _RATE_CACHE[currency] = rates
    return rates


def rule115_date(event: datetime) -> datetime:
    """Last day of the month preceding the month of the event."""
    first_of_month = pd.Timestamp(event).normalize().replace(day=1)
    return (first_of_month - pd.Timedelta(days=1)).to_pydatetime()


def get_rule115_rate(event: datetime) -> tuple[float, datetime, datetime]:
    """SBI TT Buy for Rule 115: (rate, specified date, date of the rate actually used).

    Uses the rate on the specified date, or the latest one within the
    RATE_LOOKBACK_DAYS before it when SBI published none that day.
    """
    specified = pd.Timestamp(rule115_date(event))
    rates = _load_rates("USD")
    window = rates[
        (rates["DATE"] <= specified)
        & (rates["DATE"] >= specified - pd.Timedelta(days=RATE_LOOKBACK_DAYS))
    ]
    if window.empty:
        raise ValueError(
            f"No SBI TT Buy rate for USD on or up to {RATE_LOOKBACK_DAYS} days before "
            f"{specified.date()} (Rule 115 date for {pd.Timestamp(event).date()}). "
            f"Is the ratekeeper CSV up to date?"
        )
    row = window.iloc[-1]
    return float(row["TT BUY"]), specified.to_pydatetime(), row["DATE"].to_pydatetime()


# ─── Financial year ───────────────────────────────────────────────────────────


def parse_fy(label: str) -> tuple[datetime, datetime]:
    """'2026-27' → (2026-04-01, 2027-03-31)."""
    match = re.fullmatch(r"(\d{4})-(\d{2})", label.strip())
    if not match or (int(match.group(1)) + 1) % 100 != int(match.group(2)):
        raise ValueError(f"Financial year must look like 2026-27, got '{label}'.")
    start_year = int(match.group(1))
    return datetime(start_year, 4, 1), datetime(start_year + 1, 3, 31)


def default_fy(today: date | None = None) -> str:
    """Most recently completed financial year (the one you file for)."""
    today = today or date.today()
    start_year = today.year - 1 if today.month >= 4 else today.year - 2
    return f"{start_year}-{(start_year + 1) % 100:02d}"


def fy_label() -> str:
    return f"{FY_START.year}-{FY_END.year % 100:02d}"


# ITR "Information about accrual/receipt of capital gain" periods.
QUARTERS = ["Upto 15/6", "16/6 to 15/9", "16/9 to 15/12", "16/12 to 15/3", "16/3 to 31/3"]


def quarter_index(d: datetime) -> int:
    m, day = d.month, d.day
    if m in (4, 5) or (m == 6 and day <= 15):
        return 0
    if m in (6, 7, 8) or (m == 9 and day <= 15):
        return 1
    if m in (9, 10, 11) or (m == 12 and day <= 15):
        return 2
    if m in (12, 1, 2) or (m == 3 and day <= 15):
        return 3
    return 4


def is_long_term(acquisition_date: datetime, sale_date: datetime) -> bool:
    """Long-term when held for more than LONG_TERM_MONTHS months."""
    return pd.Timestamp(sale_date) > pd.Timestamp(acquisition_date) + pd.DateOffset(
        months=LONG_TERM_MONTHS
    )


# ─── Sales CSV ────────────────────────────────────────────────────────────────


def _benefit_type(value) -> str:
    return str(value).strip().upper() if pd.notna(value) else ""


def load_sales(sales_csv: Path) -> pd.DataFrame:
    """Load and validate the sales CSV, sorted chronologically."""
    df = pd.read_csv(sales_csv)
    df.columns = df.columns.str.strip().str.lower()

    required = {"symbol", "acquisition_date", "sale_date", "units", "proceeds_usd", "cost_basis_usd"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(
            f"Sales CSV {sales_csv} is missing required columns: {missing}.\n"
            f"Found columns: {list(df.columns)}"
        )

    df["symbol"] = df["symbol"].astype(str).str.strip().str.upper()
    df["benefit_type"] = (
        df["benefit_type"].map(_benefit_type) if "benefit_type" in df.columns else ""
    )
    df["acquisition_date"] = pd.to_datetime(df["acquisition_date"])
    df["sale_date"] = pd.to_datetime(df["sale_date"])
    df["units"] = df["units"].astype(float)
    df["proceeds_usd"] = df["proceeds_usd"].astype(float)
    df["cost_basis_usd"] = df["cost_basis_usd"].astype(float)
    df["fees_usd"] = df["fees_usd"].fillna(0).astype(float) if "fees_usd" in df.columns else 0.0

    problems = []
    for i, r in df.iterrows():
        where = f"row {i + 2} ({r['symbol']} sold {r['sale_date'].date() if pd.notna(r['sale_date']) else '?'})"
        if pd.isna(r["acquisition_date"]) or pd.isna(r["sale_date"]):
            problems.append(f"{where}: acquisition_date and sale_date are required")
        elif r["acquisition_date"] > r["sale_date"]:
            problems.append(f"{where}: acquisition_date is after sale_date")
        if pd.isna(r["proceeds_usd"]):
            problems.append(f"{where}: proceeds_usd is blank")
        if pd.isna(r["cost_basis_usd"]):
            problems.append(f"{where}: cost_basis_usd is blank")
        if not r["units"] > 0:
            problems.append(f"{where}: units must be positive")
    if problems:
        raise ValueError(f"Sales CSV {sales_csv} has invalid rows:\n  " + "\n  ".join(problems))

    df = df.sort_values("sale_date", kind="stable").reset_index(drop=True)
    log.info(f"Loaded {len(df)} sale(s) from {sales_csv}")
    return df


# ─── Formatting ───────────────────────────────────────────────────────────────


def _amount(value: float) -> str:
    """Thousands separators with 2–4 decimals (trailing zeros trimmed)."""
    text = f"{value:,.4f}".rstrip("0")
    return text + "0" * (2 - len(text.split(".")[1]))


def _units(value: float) -> str:
    return f"{value:,.6f}".rstrip("0").rstrip(".")


def _inr(value: float) -> str:
    return f"−₹{-value:,.2f}" if value < 0 else f"₹{value:,.2f}"


def _rs(value: int) -> str:
    """Whole rupees with Indian digit grouping, as the portal shows them (₹31,89,241)."""
    digits = str(abs(value))
    if len(digits) > 3:
        head = ",".join(re.findall(r"\d{1,2}", digits[:-3][::-1]))[::-1]
        digits = f"{head},{digits[-3:]}"
    return f"{'-' if value < 0 else ''}₹{digits}"


def _audit_date(event: datetime, specified: datetime, rate_date: datetime) -> str:
    text = f"{event:%Y-%m-%d} (SBI date: {rate_date:%Y-%m-%d}"
    if rate_date.date() != specified.date():
        text += f", no rate on {specified:%Y-%m-%d}"
    return text + ")"


def _audit_usd(total_usd: float, units: float) -> str:
    return f"${total_usd:,.2f} ({_units(units)} @ ${total_usd / units:,.4f})"


def _audit_inr(value_usd: float, fx: float, inr: float) -> str:
    return f"${value_usd:,.2f} × ₹{_amount(fx)} (TT Buy) = ₹{inr:,.2f}"


# ─── Calculation ──────────────────────────────────────────────────────────────

SHORT_TERM = "Short-term"
LONG_TERM = "Long-term"

# Schedule CG item and Table F row for each term.
ITEM_REFS = {SHORT_TERM: "A5", LONG_TERM: "B8"}
TABLE_F_ROWS = {SHORT_TERM: "3", LONG_TERM: "5"}
MAX_AMOUNT = 99_999_999_999_999

AUDIT_TRAIL_COLS = [
    "Symbol",
    "Type",
    "Units",
    "Term",
    "Acquisition Date",
    "Sale Date",
    "Sale Value (USD)",
    "Sale Value (INR)",
    "Cost (USD)",
    "Cost (INR)",
    "Fees (USD)",
    "Fees (INR)",
    "Gain (INR)",
    "Quarter",
]


def process_sale(sale: pd.Series) -> dict:
    """Compute INR full value, cost, fees and gain for one sale row."""
    acq_date = sale["acquisition_date"].to_pydatetime()
    sale_date = sale["sale_date"].to_pydatetime()
    units = float(sale["units"])

    sale_fx, sale_spec, sale_fx_date = get_rule115_rate(sale_date)
    acq_fx, acq_spec, acq_fx_date = get_rule115_rate(acq_date)

    sale_usd = float(sale["proceeds_usd"])
    cost_usd = float(sale["cost_basis_usd"])
    fees_usd = float(sale["fees_usd"])

    full_inr = round(sale_usd * sale_fx, 2)
    cost_inr = round(cost_usd * acq_fx, 2)
    fees_inr = round(fees_usd * sale_fx, 2)
    gain_inr = round(full_inr - cost_inr - fees_inr, 2)

    term = LONG_TERM if is_long_term(acq_date, sale_date) else SHORT_TERM
    held_days = (sale_date - acq_date).days
    quarter = quarter_index(sale_date)

    log.info(
        f"{sale['symbol']} {units:g} sold {sale_date.date()} (acquired {acq_date.date()}, "
        f"{term.lower()}): ₹{full_inr:,.2f} − ₹{cost_inr:,.2f} − ₹{fees_inr:,.2f} "
        f"= ₹{gain_inr:,.2f}"
    )
    if term == LONG_TERM and sale_date < datetime(2024, 7, 23):
        log.warning(
            f"  Long-term sale before 2024-07-23: the old 20% rate with indexation may "
            f"apply. This tool reports cost without indexation."
        )

    audit = {
        "Symbol": sale["symbol"],
        "Type": sale["benefit_type"],
        "Units": _units(units),
        "Term": f"{term} ({held_days} days)",
        "Acquisition Date": _audit_date(acq_date, acq_spec, acq_fx_date),
        "Sale Date": _audit_date(sale_date, sale_spec, sale_fx_date),
        "Sale Value (USD)": _audit_usd(sale_usd, units),
        "Sale Value (INR)": _audit_inr(sale_usd, sale_fx, full_inr),
        "Cost (USD)": _audit_usd(cost_usd, units),
        "Cost (INR)": _audit_inr(cost_usd, acq_fx, cost_inr),
        "Fees (USD)": f"${fees_usd:,.2f}" if fees_usd else "",
        "Fees (INR)": _audit_inr(fees_usd, sale_fx, fees_inr) if fees_usd else "",
        "Gain (INR)": f"{_inr(full_inr)} − {_inr(cost_inr)} − {_inr(fees_inr)} = {_inr(gain_inr)}",
        "Quarter": QUARTERS[quarter],
    }

    return {
        "term": term,
        "quarter": quarter,
        "units": units,
        "sale_usd": sale_usd,
        "cost_usd": cost_usd,
        "fees_usd": fees_usd,
        "full_inr": full_inr,
        "cost_inr": cost_inr,
        "fees_inr": fees_inr,
        "gain_inr": gain_inr,
        "audit": audit,
    }


def _rupees(value: float) -> int:
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def build_item(results: list[dict], term: str) -> dict:
    """Schedule CG A5 (short-term) or B8 (long-term) totals, in whole rupees."""
    rs = [r for r in results if r["term"] == term]
    item = {
        "full_value": _rupees(sum(r["full_inr"] for r in rs)),
        "cost_of_acquisition": _rupees(sum(r["cost_inr"] for r in rs)),
        "cost_of_improvement": 0,
        "transfer_expenses": _rupees(sum(r["fees_inr"] for r in rs)),
    }
    item["total_deductions"] = (
        item["cost_of_acquisition"] + item["cost_of_improvement"] + item["transfer_expenses"]
    )
    item["capital_gain"] = item["full_value"] - item["total_deductions"]
    return item


def build_schedule(results: list[dict]) -> dict[str, dict]:
    return {term: build_item(results, term) for term in (SHORT_TERM, LONG_TERM)}


def allocate_quarters(net: list[float]) -> list[float]:
    """Non-negative quarterly split whose total is max(sum(net), 0).

    A quarter's loss is set off against the same and later quarters first; a loss
    left at year end reduces the latest quarters.
    """
    out, carry = [], 0.0
    for value in net:
        value += carry
        out.append(max(value, 0.0))
        carry = min(value, 0.0)
    for i in reversed(range(len(out))):
        take = min(out[i], -carry)
        out[i] -= take
        carry += take
    return out


def build_quarterly(results: list[dict], schedule: dict[str, dict]) -> dict[str, list[int]]:
    """Table F amounts per period (whole rupees, never negative) matching the item's gain."""
    quarterly = {}
    for term, item in schedule.items():
        net = [
            sum(r["gain_inr"] for r in results if r["term"] == term and r["quarter"] == q)
            for q in range(len(QUARTERS))
        ]
        split = [_rupees(v) for v in allocate_quarters(net)]
        target = max(item["capital_gain"], 0)
        if target:
            split[max(range(len(split)), key=lambda q: split[q])] += target - sum(split)
        else:
            split = [0] * len(split)
        quarterly[term] = split
    return quarterly


# ─── Validation ───────────────────────────────────────────────────────────────


def validate(schedule: dict[str, dict], quarterly: dict[str, list[int]]) -> list[tuple[str, bool]]:
    """CBDT validation rules and portal amount formats: (description, passed)."""
    checks = []
    for term, item in schedule.items():
        ref, row, quarters = ITEM_REFS[term], TABLE_F_ROWS[term], quarterly[term]
        amounts = [*item.values(), *quarters]
        checks += [
            (
                f"{ref}: no expenses claimed when the full value is zero",
                item["full_value"] != 0 or item["total_deductions"] == 0,
            ),
            (
                f"{ref}: total deductions = cost of acquisition + improvement + transfer expenses",
                item["total_deductions"]
                == item["cost_of_acquisition"] + item["cost_of_improvement"] + item["transfer_expenses"],
            ),
            (
                f"{ref}: capital gain = full value – total deductions",
                item["capital_gain"] == item["full_value"] - item["total_deductions"],
            ),
            (
                f"Table F row {row}: periods add up to the {ref} gain, or 0 for a loss",
                sum(quarters) == max(item["capital_gain"], 0),
            ),
            (
                f"{ref} and Table F row {row}: amounts are whole rupees, at most 14 digits",
                all(type(v) is int and abs(v) <= MAX_AMOUNT for v in amounts),
            ),
            (
                f"{ref} and Table F row {row}: only the capital gain may be negative",
                all(v >= 0 for k, v in item.items() if k != "capital_gain") and min(quarters) >= 0,
            ),
        ]
    return checks


# ─── Output ───────────────────────────────────────────────────────────────────


def _table_f(term: str, quarters: list[int]) -> str:
    periods = " | ".join(f"{q} {_rs(v)}" for q, v in zip(QUARTERS, quarters))
    return f"Table F row {TABLE_F_ROWS[term]}: {periods}"


def total_row(results: list[dict], term: str, item: dict, quarters: list[int]) -> dict:
    """Audit trail TOTAL row: the values to enter in Schedule CG for one term."""
    rs = [r for r in results if r["term"] == term]
    total = {k: sum(r[k] for r in rs) for k in ("units", "sale_usd", "cost_usd", "fees_usd")}
    full, cost, fees = item["full_value"], item["cost_of_acquisition"], item["transfer_expenses"]
    return {
        "Symbol": "TOTAL",
        "Type": f"Schedule CG {ITEM_REFS[term]}",
        "Units": _units(total["units"]),
        "Term": term,
        "Acquisition Date": "",
        "Sale Date": "",
        "Sale Value (USD)": f"${total['sale_usd']:,.2f}",
        "Sale Value (INR)": _rs(full),
        "Cost (USD)": f"${total['cost_usd']:,.2f}",
        "Cost (INR)": _rs(cost),
        "Fees (USD)": f"${total['fees_usd']:,.2f}",
        "Fees (INR)": _rs(fees),
        "Gain (INR)": f"{_rs(full)} − {_rs(cost)} − {_rs(fees)} = {_rs(item['capital_gain'])}",
        "Quarter": _table_f(term, quarters),
    }


HEADINGS = {
    SHORT_TERM: "A5. Short-term: from sale of assets other than at A1 or A2 or A3 or A4 above",
    LONG_TERM: "B8. Long-term: from sale of assets where B1 to B7 above are not applicable",
}


def form_rows(term: str, item: dict) -> list[tuple[str, int]]:
    """Schedule CG rows for item A5 / B8: (label, value)."""
    short = term == SHORT_TERM
    return [
        ("a(i)   Unquoted shares: a, b and c (section 50CA)", 0),
        ("a(ii)  Full value of consideration, assets other than unquoted shares", item["full_value"]),
        ("a(iii) Total (ic + ii)", item["full_value"]),
        ("b(i)   Cost of acquisition without indexation", item["cost_of_acquisition"]),
        ("b(ii)  Cost of improvement without indexation", item["cost_of_improvement"]),
        ("b(iii) Expenditure wholly and exclusively in connection with transfer", item["transfer_expenses"]),
        ("b(iv)  Total (bi + bii + biii)", item["total_deductions"]),
        ("c      Balance (aiii – biv)", item["capital_gain"]),
        ("d      Loss disallowed u/s 94(7) or 94(8)" if short else "d      Deduction under section 54F", 0),
        ("e      Capital gain (c + d)" if short else "e      Capital gain (c – d)", item["capital_gain"]),
    ]


def log_form(schedule: dict[str, dict], quarterly: dict[str, list[int]], checks: list) -> None:
    """Log the Schedule CG rows, Table F and the validation results."""
    for term, item in schedule.items():
        log.info(HEADINGS[term])
        for label, value in form_rows(term, item):
            log.info(f"  {label:<72}{_rs(value):>14}")
    log.info("F. Information about accrual/receipt of capital gain")
    for term, quarters in quarterly.items():
        log.info(f"  Row {TABLE_F_ROWS[term]} ({term.lower()})")
        for period, value in zip(QUARTERS, quarters):
            log.info(f"    {period:<70}{_rs(value):>14}")
    for text, ok in checks:
        (log.info if ok else log.error)(f"{'✓' if ok else '✗'} {text}")


def generate(sales_csv: Path, output_txt: Path) -> tuple[dict, dict, list]:
    """Log the form rows and write the audit trail next to output_txt.

    Returns (schedule, quarterly, checks). Raises ValueError if any validation check fails.
    """
    sales = load_sales(sales_csv)
    in_fy = sales[(sales["sale_date"] >= FY_START) & (sales["sale_date"] <= FY_END)]
    log.info(
        f"{len(in_fy)} of {len(sales)} sale(s) fall in FY {fy_label()} "
        f"({FY_START.date()} → {FY_END.date()})"
    )

    results = [process_sale(s) for _, s in in_fy.iterrows()]

    schedule = build_schedule(results)
    quarterly = build_quarterly(results, schedule)
    checks = validate(schedule, quarterly)

    rows = [r["audit"] for r in results]
    rows.append(dict.fromkeys(AUDIT_TRAIL_COLS, ""))
    rows += [total_row(results, t, schedule[t], quarterly[t]) for t in schedule]
    audit_csv = output_txt.with_name(f"{output_txt.stem}_audit_trail.csv")
    pd.DataFrame(rows, columns=AUDIT_TRAIL_COLS).to_csv(audit_csv, index=False, encoding="utf-8-sig")

    log_form(schedule, quarterly, checks)

    print(f"\n📋  Output      → {output_txt.resolve()}")
    print(f"🧾  Audit trail → {audit_csv.resolve()}")

    failed = [text for text, ok in checks if not ok]
    if failed:
        raise ValueError(f"{len(failed)} validation check(s) failed:\n  " + "\n  ".join(failed))
    return schedule, quarterly, checks


# ─── CLI ──────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate Schedule CG (capital gains on foreign shares) for Indian ITR."
    )
    parser.add_argument(
        "sales_csv",
        nargs="?",
        default=str(INPUTS_DIR / "sales.csv"),
        help="Path to sales CSV (default: inputs/sales.csv)",
    )
    parser.add_argument(
        "--fy",
        default=default_fy(),
        help=f"Financial year to report, e.g. 2026-27 (default: {default_fy()})",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output (log) filename, written to outputs/ (default: cg_<fy>.txt); the audit "
        "trail is written next to it as <name>_audit_trail.csv",
    )
    parser.add_argument(
        "--skip-update", action="store_true", help="Skip git pull for sbi-fx-ratekeeper"
    )
    args = parser.parse_args()

    setup_logging()

    global FY_START, FY_END
    try:
        FY_START, FY_END = parse_fy(args.fy)
    except ValueError as e:
        log.error(str(e))
        sys.exit(1)

    sales_csv = Path(args.sales_csv)
    if not sales_csv.exists():
        log.error(f"Sales file not found: {sales_csv}")
        sys.exit(1)

    output_txt = OUTPUTS_DIR / (args.output or f"cg_{fy_label()}.txt")
    output_txt.parent.mkdir(parents=True, exist_ok=True)
    setup_logging(output_txt)

    log.info(f"Financial Year: {FY_START.date()} → {FY_END.date()}")

    setup_ratekeeper(skip_update=args.skip_update)

    try:
        generate(sales_csv, output_txt)
    except ValueError as e:
        log.error(str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()

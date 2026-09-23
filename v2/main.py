#!/usr/bin/env python3
"""
Schedule FA Generator for Indian ITR — variant that uses a manually-supplied
price CSV (e.g. exported from Google Sheets / GOOGLEFINANCE) instead of yfinance.

This is useful because GOOGLEFINANCE adjusts only for splits (not dividends),
which is the correct convention for Schedule FA — while yfinance's auto_adjust
silently dividend-adjusts historical prices.

Schedule FA in Indian ITR is reported on a CALENDAR-YEAR basis (Jan 1 – Dec 31).

Usage (defaults read inputs/input.csv, inputs/prices.csv, inputs/sales.csv if present;
write outputs/output.csv and logs/schedule_fa.log):
    python3 main.py --year 2025
    python3 main.py --year 2026 --output output_2026.csv
    python3 main.py inputs/input_test.csv --prices inputs/prices.csv --sales inputs/sales.csv --year 2026
    python3 main.py --year 2025 --skip-update

input.csv columns (one row per acquired lot):
    symbol, units, acquisition_date, acquisition_price, company_name, address, zip_code
    Optional: benefit_type, nature, country, country_code, dividends_usd

sales.csv columns (one row per sale, all years):
    symbol, sale_date, units, sale_price
    Optional: benefit_type, acquisition_date (sell from that lot; otherwise FIFO
              across lots with the same symbol + benefit_type)

prices.csv columns (one row per symbol per year):
    symbol, year, peak_price, peak_date, closing_price
"""

import argparse
import logging
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

# ─── Logging ──────────────────────────────────────────────────────────────────
log = logging.getLogger(__name__)


def setup_logging() -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(LOG_FILE, mode="w"),
        ],
        force=True,
    )


# ─── Global Config (overridden by CLI args) ───────────────────────────────────
# Schedule FA reports on the CALENDAR year (Jan 1 – Dec 31), not the Indian FY.
CY_START = datetime(2024, 1, 1)
CY_END = datetime(2024, 12, 31)

BASE_DIR = Path(__file__).parent
REPO_ROOT = BASE_DIR.parent
INPUTS_DIR = BASE_DIR / "inputs"
OUTPUTS_DIR = BASE_DIR / "outputs"
LOGS_DIR = BASE_DIR / "logs"
LOG_FILE = LOGS_DIR / "schedule_fa.log"
RATEKEEPER_REPO = "https://github.com/sahilgupta/sbi-fx-ratekeeper"
RATEKEEPER_DIR = REPO_ROOT / "sbi-fx-ratekeeper"

# Cached SBI rate data by currency
_RATE_CACHE: dict[str, pd.DataFrame] = {}

# Loaded prices.csv keyed by (symbol, year)
_PRICES: dict[tuple[str, int], dict] = {}


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


def get_sbi_tt_buy(target: datetime) -> tuple[float, datetime]:
    """SBI TT Buy USD → INR for target date (forward-fills up to 10 days)."""
    rates = _load_rates("USD")
    ts = pd.Timestamp(target)
    if ts.tzinfo is not None:
        ts = ts.tz_localize(None)
    target_date = ts.normalize()
    end_date = target_date + pd.Timedelta(days=10)
    matches = rates[(rates["DATE"] >= target_date) & (rates["DATE"] <= end_date)]

    if not matches.empty:
        row = matches.iloc[0]
        return float(row["TT BUY"]), row["DATE"].to_pydatetime()

    raise ValueError(
        f"No SBI TT Buy rate found for USD starting {target.date()} "
        f"(checked 10 days). Is the ratekeeper CSV up to date?"
    )


# ─── Prices CSV ───────────────────────────────────────────────────────────────


def load_prices(prices_csv: Path) -> None:
    """Load the manually-supplied per-symbol, per-year prices into _PRICES."""
    df = pd.read_csv(prices_csv)
    df.columns = df.columns.str.strip().str.lower()

    required = {"symbol", "year", "peak_price", "peak_date", "closing_price"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(
            f"Prices CSV {prices_csv} is missing required columns: {missing}.\n"
            f"Found columns: {list(df.columns)}"
        )

    for _, row in df.iterrows():
        symbol = str(row["symbol"]).strip().upper()
        year = int(row["year"])
        if (symbol, year) in _PRICES:
            raise ValueError(f"Prices CSV {prices_csv}: duplicate row for {symbol} {year}.")
        peak_date = pd.to_datetime(row["peak_date"]).to_pydatetime()
        entry = {
            "peak_price": float(row["peak_price"]),
            "peak_date": peak_date,
            "closing_price": (
                float(row["closing_price"]) if pd.notna(row["closing_price"]) else None
            ),
        }
        _PRICES[(symbol, year)] = entry

    log.info(f"Loaded {len(_PRICES)} symbol-year price row(s) from {prices_csv}")


# ─── Sales CSV ────────────────────────────────────────────────────────────────


def _benefit_type(value) -> str:
    return str(value).strip().upper() if pd.notna(value) else ""


def load_sales(sales_csv: Path) -> pd.DataFrame:
    """Load the sales CSV, sorted chronologically."""
    df = pd.read_csv(sales_csv)
    df.columns = df.columns.str.strip().str.lower()

    required = {"symbol", "sale_date", "units", "sale_price"}
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
    df["sale_date"] = pd.to_datetime(df["sale_date"])
    df["acquisition_date"] = (
        pd.to_datetime(df["acquisition_date"])
        if "acquisition_date" in df.columns
        else pd.NaT
    )
    df["units"] = df["units"].astype(float)
    df["sale_price"] = df["sale_price"].astype(float)
    df = df.sort_values("sale_date", kind="stable").reset_index(drop=True)

    log.info(f"Loaded {len(df)} sale(s) from {sales_csv}")
    return df


def allocate_sales(lots: pd.DataFrame, sales: pd.DataFrame) -> dict:
    """Match each sale to lots with the same symbol + benefit_type.

    Uses the lot given by the sale's acquisition_date if set, otherwise FIFO
    (oldest acquisition first). Returns {lot_index: [sale allocations]}.
    """
    remaining = lots["units"].astype(float).to_dict()
    allocations = {idx: [] for idx in lots.index}
    fifo = lots.sort_values("acq_date", kind="stable")

    for _, sale in sales.iterrows():
        candidates = fifo[
            (fifo["symbol_norm"] == sale["symbol"])
            & (fifo["benefit_type_norm"] == sale["benefit_type"])
            & (fifo["acq_date"] <= sale["sale_date"])
        ]
        if pd.notna(sale["acquisition_date"]):
            candidates = candidates[candidates["acq_date"] == sale["acquisition_date"]]

        to_sell = sale["units"]
        for idx in candidates.index:
            if to_sell <= 1e-9:
                break
            take = min(remaining[idx], to_sell)
            if take <= 1e-9:
                continue
            remaining[idx] -= take
            to_sell -= take
            allocations[idx].append(
                {
                    "sale_date": sale["sale_date"].to_pydatetime(),
                    "units": take,
                    "sale_price": sale["sale_price"],
                }
            )

        if to_sell > 1e-9:
            raise ValueError(
                f"Sale of {sale['units']:g} {sale['symbol']} "
                f"(benefit_type='{sale['benefit_type']}') on {sale['sale_date'].date()}: "
                f"{to_sell:g} unit(s) could not be matched to any lot held on that date."
            )

    return allocations


def _get_price_entry(symbol: str, year: int) -> dict:
    key = (symbol, year)
    if key not in _PRICES:
        raise ValueError(
            f"{symbol}: no price entry for {year} in prices CSV. "
            f"Add a row with year={year}, peak_price, peak_date, closing_price for this symbol."
        )
    return _PRICES[key]


# ─── Core Processing ──────────────────────────────────────────────────────────

SCHEDULE_FA_COLS = [
    "Country/Region name",
    "Country Name and Code",
    "Name of entity",
    "Address of entity",
    "ZIP Code",
    "Nature of entity",
    "Date of acquiring the interest",
    "Initial value of the investment",
    "Peak value of investment during the Period",
    "Closing balance",
    "Total gross amount paid/credited with respect to the holding during the period",
    "Total gross proceeds from sale or redemption of investment during the period",
]


AUDIT_TRAIL_COLS = [
    "Symbol",
    "Type",
    "Units",
    "Initial Date",
    "Initial Value (USD)",
    "Initial Value (INR)",
    "Peak Date",
    "Peak Value (USD)",
    "Peak Value (INR)",
    "Closing Date",
    "Closing Value (USD)",
    "Closing Value (INR)",
    "Dividends Date",
    "Dividends Value (USD)",
    "Dividends Value (INR)",
    "Sale Dates",
    "Sale Value (USD)",
    "Sale Value (INR)",
]


def _amount(value: float) -> str:
    """Thousands separators with 2–4 decimals (trailing zeros trimmed)."""
    text = f"{value:,.4f}".rstrip("0")
    return text + "0" * (2 - len(text.split(".")[1]))


def _units(value: float) -> str:
    return f"{value:,.6f}".rstrip("0").rstrip(".")


def _audit_date(date: datetime, fx_date: datetime) -> str:
    """Date, plus the SBI rate date when a different day's rate was used."""
    text = date.strftime("%Y-%m-%d")
    if fx_date.date() != date.date():
        text += f" (SBI date: {fx_date:%Y-%m-%d})"
    return text


def _audit_usd(units: float, price_usd: float) -> str:
    return f"{_units(units)} × ${_amount(price_usd)} = ${units * price_usd:,.2f}"


def _audit_inr(value_usd: float, fx: float, inr: float) -> str:
    return f"${value_usd:,.2f} × ₹{_amount(fx)} (TT Buy) = ₹{inr:,.2f}"


def _audit_join(parts: list[str], total: str) -> str:
    text = " | ".join(parts)
    return f"{text} → total {total}" if len(parts) > 1 else text


def process_row(row: pd.Series, sales: list[dict]) -> tuple[dict, dict] | None:
    """Compute all Schedule FA fields (in INR) for one holding.

    Returns a tuple of (schedule_fa_row, audit_trail_row), or None if the lot
    was not held during the CY.
    """
    symbol = str(row["symbol"]).strip().upper()
    units = float(row["units"])
    # acquisition_date in the input CSV is expected as YYYY-MM-DD (e.g. 2024-03-01)
    acq_date = pd.to_datetime(row["acquisition_date"]).to_pydatetime()

    sold_before = sum(s["units"] for s in sales if s["sale_date"] < CY_START)
    sales_in_cy = [s for s in sales if CY_START <= s["sale_date"] <= CY_END]
    units_at_start = units - sold_before
    units_sold = sum(s["units"] for s in sales_in_cy)
    units_at_end = units_at_start - units_sold

    log.info(f"\n{'─' * 60}")
    log.info(
        f"  {symbol}  |  units={units}  |  acquired={acq_date.date()}  |  "
        f"held at start={units_at_start}  sold in CY={units_sold}  held at end={units_at_end}"
    )

    if acq_date > CY_END:
        log.info(f"  Skipped:  acquired after {CY_END.date()}")
        return None
    if units_at_start <= 1e-9:
        log.info(f"  Skipped:  fully sold before {CY_START.date()}")
        return None

    prices = _get_price_entry(symbol, CY_START.year)

    # 1. Initial value of the investment
    #    The "initial value" is the ORIGINAL acquisition cost of the lot and is
    #    constant for every year the lot is held:
    #        Acquisition price × SBI rate on acq_date × units.
    #    • Acquired during the CY   → cost basis for this year's schedule.
    #    • Held before CY (carry-forward, acq_date < CY_START) → same original
    #      cost basis carried forward unchanged from the previous year's schedule
    #      (only peak & closing below are recomputed for the current year).
    is_carry_forward = acq_date < CY_START
    initial_price_usd = float(row["acquisition_price"])
    initial_date = acq_date
    initial_source = (
        "Acquisition price (carry-forward)"
        if is_carry_forward
        else "Acquisition price"
    )
    initial_units = units_at_start
    initial_fx, initial_fx_date = get_sbi_tt_buy(initial_date)
    initial_inr = round(initial_price_usd * initial_fx * initial_units, 2)
    log.info(
        f"  Initial:  ${initial_price_usd:.4f} ({initial_source}) × ₹{initial_fx:.4f} "
        f"× {initial_units} = ₹{initial_inr:,.2f}  (SBI date used: {initial_fx_date.date()})"
    )

    # 2. Peak value — from prices CSV
    peak_price_usd = prices["peak_price"]
    peak_date = prices["peak_date"]
    peak_fx, peak_fx_date = get_sbi_tt_buy(peak_date)
    peak_inr = round(peak_price_usd * peak_fx * units_at_start, 2)
    log.info(
        f"  Peak:     ${peak_price_usd:.4f} on {peak_date.date()} (prices.csv) "
        f"× ₹{peak_fx:.4f} × {units_at_start} = ₹{peak_inr:,.2f}  (SBI date used: {peak_fx_date.date()})"
    )

    # 3. Closing balance — price on Dec 31 from prices CSV
    closing_units = units_at_end
    if closing_units > 1e-9:
        closing_price_usd = prices["closing_price"]
        if closing_price_usd is None:
            raise ValueError(
                f"{symbol}: {closing_units} units held at {CY_END.date()} but closing_price "
                f"for {CY_START.year} is blank in prices CSV."
            )
        closing_fx, closing_fx_date = get_sbi_tt_buy(CY_END)
        closing_inr = round(closing_price_usd * closing_fx * closing_units, 2)
        log.info(
            f"  Closing:  ${closing_price_usd:.4f} (prices.csv) × ₹{closing_fx:.4f} × {closing_units} "
            f"= ₹{closing_inr:,.2f}  (SBI date used: {closing_fx_date.date()})"
        )
    else:
        closing_price_usd = None
        closing_fx = None
        closing_fx_date = None
        closing_inr = 0.0
        log.info("  Closing:  ₹0  (position fully sold/closed during CY)")

    # 4. Dividends (USD → INR at SBI TT Buy on CY_END = Dec 31)
    dividends_raw = row.get("dividends_usd")
    dividends_usd = float(dividends_raw) if pd.notna(dividends_raw) else 0.0
    dividends_inr = 0.0
    dividends_fx = dividends_fx_date = None
    if dividends_usd:
        dividends_fx, dividends_fx_date = get_sbi_tt_buy(CY_END)
        dividends_inr = round(dividends_usd * dividends_fx, 2)

    # 5. Gross sale proceeds (each sale: units × sale price × SBI TT Buy on sale date)
    proceeds_usd = 0.0
    proceeds_inr = 0.0
    sale_dates, sale_usd, sale_inr = [], [], []
    for s in sales_in_cy:
        sale_fx, sale_fx_date = get_sbi_tt_buy(s["sale_date"])
        gross_usd = s["units"] * s["sale_price"]
        gross_inr = round(gross_usd * sale_fx, 2)
        proceeds_usd += gross_usd
        proceeds_inr += gross_inr
        sale_dates.append(_audit_date(s["sale_date"], sale_fx_date))
        sale_usd.append(_audit_usd(s["units"], s["sale_price"]))
        sale_inr.append(_audit_inr(gross_usd, sale_fx, gross_inr))
        log.info(
            f"  Sale:     {s['units']:g} × ${s['sale_price']:.4f} on {s['sale_date'].date()} "
            f"× ₹{sale_fx:.4f} = ₹{gross_inr:,.2f}  (SBI date used: {sale_fx_date.date()})"
        )
    proceeds_usd = round(proceeds_usd, 4)
    proceeds_inr = round(proceeds_inr, 2)
    log.info(f"  Dividends: ₹{dividends_inr:,.2f}   Proceeds: ₹{proceeds_inr:,.2f}")

    fa_row = {
        "Country/Region name": row.get("country", "UNITED STATES OF AMERICA"),
        "Country Name and Code": row.get("country_code", 2),
        "Name of entity": row["company_name"],
        "Address of entity": row["address"],
        "ZIP Code": row["zip_code"],
        "Nature of entity": row.get("nature", "Company"),
        "Date of acquiring the interest": acq_date.strftime("%Y-%m-%d"),
        "Initial value of the investment": initial_inr,
        "Peak value of investment during the Period": peak_inr,
        "Closing balance": closing_inr,
        "Total gross amount paid/credited with respect to the holding during the period": dividends_inr,
        "Total gross proceeds from sale or redemption of investment during the period": proceeds_inr,
    }

    held_at_end = closing_price_usd is not None
    audit_row = {
        "Symbol": symbol,
        "Type": _benefit_type(row.get("benefit_type")),
        "Units": (
            f"{_units(units_at_start)} (carry forward)" if is_carry_forward else _units(units_at_start)
        ),
        "Initial Date": _audit_date(initial_date, initial_fx_date),
        "Initial Value (USD)": _audit_usd(initial_units, initial_price_usd),
        "Initial Value (INR)": _audit_inr(initial_units * initial_price_usd, initial_fx, initial_inr),
        "Peak Date": _audit_date(peak_date, peak_fx_date),
        "Peak Value (USD)": _audit_usd(units_at_start, peak_price_usd),
        "Peak Value (INR)": _audit_inr(units_at_start * peak_price_usd, peak_fx, peak_inr),
        "Closing Date": _audit_date(CY_END, closing_fx_date) if held_at_end else "",
        "Closing Value (USD)": (
            _audit_usd(closing_units, closing_price_usd) if held_at_end else "Fully sold"
        ),
        "Closing Value (INR)": (
            _audit_inr(closing_units * closing_price_usd, closing_fx, closing_inr)
            if held_at_end
            else "Fully sold"
        ),
        "Dividends Date": _audit_date(CY_END, dividends_fx_date) if dividends_usd else "",
        "Dividends Value (USD)": f"${_amount(dividends_usd)}" if dividends_usd else "",
        "Dividends Value (INR)": (
            _audit_inr(dividends_usd, dividends_fx, dividends_inr) if dividends_usd else ""
        ),
        "Sale Dates": " | ".join(sale_dates),
        "Sale Value (USD)": _audit_join(sale_usd, f"${proceeds_usd:,.2f}"),
        "Sale Value (INR)": _audit_join(sale_inr, f"₹{proceeds_inr:,.2f}"),
        "_units_at_start": units_at_start,
        "_units_at_end": closing_units,
    }

    return fa_row, audit_row


def generate(input_csv: Path, output_csv: Path, sales_csv: Path | None = None) -> None:
    df = pd.read_csv(input_csv)
    df.columns = df.columns.str.strip().str.lower()

    required = {
        "symbol",
        "units",
        "acquisition_date",
        "acquisition_price",
        "company_name",
        "address",
        "zip_code",
    }
    missing = required - set(df.columns)
    if missing:
        raise ValueError(
            f"Input CSV is missing required columns: {missing}\n"
            f"Found columns: {list(df.columns)}\n"
            f"See input_template.csv for the expected format."
        )

    df["symbol_norm"] = df["symbol"].astype(str).str.strip().str.upper()
    df["benefit_type_norm"] = (
        df["benefit_type"].map(_benefit_type) if "benefit_type" in df.columns else ""
    )
    df["acq_date"] = pd.to_datetime(df["acquisition_date"])

    if sales_csv is not None:
        try:
            allocations = allocate_sales(df, load_sales(sales_csv))
        except ValueError as e:
            log.error(str(e))
            sys.exit(1)
    else:
        allocations = {idx: [] for idx in df.index}

    rows, audit_rows, errors, units_records = [], [], [], []
    for i, (idx, row) in enumerate(df.iterrows(), 1):
        try:
            result = process_row(row, allocations[idx])
            if result is None:
                continue
            fa_row, audit_row = result
            rows.append(fa_row)
            audit_rows.append(audit_row)
            units_records.append(
                {
                    "symbol": audit_row["Symbol"],
                    "start": audit_row["_units_at_start"],
                    "end": audit_row["_units_at_end"],
                }
            )
        except Exception as e:
            log.error(f"Row {i} ({row.get('symbol', '?')}): {e}")
            errors.append((i, row.get("symbol", "?"), str(e)))

    out_df = pd.DataFrame(rows, columns=SCHEDULE_FA_COLS)
    out_df.to_csv(output_csv, index=False)

    audit_csv = output_csv.with_name(f"{output_csv.stem}_audit_trail.csv")
    audit_df = pd.DataFrame(audit_rows, columns=AUDIT_TRAIL_COLS)
    audit_df.to_csv(audit_csv, index=False, encoding="utf-8-sig")

    if not out_df.empty:
        total_initial = out_df["Initial value of the investment"].sum()
        total_peak = out_df["Peak value of investment during the Period"].sum()
        total_closing = out_df["Closing balance"].sum()
        total_dividends = out_df[
            "Total gross amount paid/credited with respect to the holding during the period"
        ].sum()
        total_proceeds = out_df[
            "Total gross proceeds from sale or redemption of investment during the period"
        ].sum()
        print(f"\n{'─' * 70}")
        print(f"TOTALS across {len(out_df)} holding(s) (INR)")
        print(f"{'─' * 70}")
        print(f"  Initial value     : ₹{total_initial:>20,.2f}")
        print(f"  Peak value        : ₹{total_peak:>20,.2f}")
        print(f"  Closing balance   : ₹{total_closing:>20,.2f}")
        print(f"  Dividends (gross) : ₹{total_dividends:>20,.2f}")
        print(f"  Proceeds (gross)  : ₹{total_proceeds:>20,.2f}")

        summary = (
            pd.DataFrame(units_records)
            .groupby("symbol")[["start", "end"]]
            .sum()
            .sort_index()
        )
        print(f"\n{'─' * 70}")
        print(f"UNITS per symbol")
        print(f"{'─' * 70}")
        print(f"  {'Symbol':<10} {'Start':>12} {'End':>12} {'Change':>12}")
        for sym, urow in summary.iterrows():
            change = urow["end"] - urow["start"]
            sign = "+" if change > 0 else ""
            print(
                f"  {sym:<10} {urow['start']:>12,.4f} {urow['end']:>12,.4f} "
                f"{sign}{change:>11,.4f}"
            )

    if errors:
        print(f"\n⚠️  Errors for {len(errors)} row(s):")
        for i, sym, msg in errors:
            print(f"   Row {i} ({sym}): {msg}")

    print(f"\n✅  Saved → {output_csv.resolve()}")
    print(f"🧾  Audit → {audit_csv.resolve()}")
    print(f"📋  Log   → {LOG_FILE.resolve()}")


# ─── CLI ──────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate Schedule FA CSV for Indian ITR from IB holdings "
        "using a manually-supplied prices CSV (no yfinance)."
    )
    parser.add_argument(
        "input_csv",
        nargs="?",
        default=str(INPUTS_DIR / "input.csv"),
        help="Path to input holdings CSV (default: inputs/input.csv)",
    )
    parser.add_argument(
        "--prices",
        default=str(INPUTS_DIR / "prices.csv"),
        help="Path to prices CSV (symbol, year, peak_price, peak_date, closing_price) "
        "(default: inputs/prices.csv)",
    )
    parser.add_argument(
        "--sales",
        default=None,
        help="Path to sales CSV (symbol, sale_date, units, sale_price[, benefit_type, acquisition_date]) "
        "(default: inputs/sales.csv if it exists)",
    )
    parser.add_argument(
        "--output",
        default="output.csv",
        help="Output CSV filename, written to outputs/ (default: output.csv)",
    )
    parser.add_argument(
        "--year",
        type=int,
        default=2024,
        help="Calendar year to report (default: 2024)",
    )
    parser.add_argument(
        "--skip-update", action="store_true", help="Skip git pull for sbi-fx-ratekeeper"
    )
    args = parser.parse_args()

    setup_logging()

    global CY_START, CY_END
    CY_START = datetime(args.year, 1, 1)
    CY_END = datetime(args.year, 12, 31)

    log.info(f"Calendar Year: {CY_START.date()} → {CY_END.date()}")

    setup_ratekeeper(skip_update=args.skip_update)

    prices_csv = Path(args.prices)
    if not prices_csv.exists():
        log.error(f"Prices file not found: {prices_csv}")
        sys.exit(1)
    load_prices(prices_csv)

    input_csv = Path(args.input_csv)
    output_csv = OUTPUTS_DIR / args.output
    output_csv.parent.mkdir(parents=True, exist_ok=True)

    if not input_csv.exists():
        log.error(f"Input file not found: {input_csv}")
        sys.exit(1)

    if args.sales:
        sales_csv = Path(args.sales)
        if not sales_csv.exists():
            log.error(f"Sales file not found: {sales_csv}")
            sys.exit(1)
    else:
        default_sales = INPUTS_DIR / "sales.csv"
        sales_csv = default_sales if default_sales.exists() else None

    generate(input_csv, output_csv, sales_csv)


if __name__ == "__main__":
    main()

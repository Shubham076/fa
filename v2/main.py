#!/usr/bin/env python3
"""
Schedule FA Generator for Indian ITR — variant that uses a manually-supplied
price CSV (e.g. exported from Google Sheets / GOOGLEFINANCE) instead of yfinance.

This is useful because GOOGLEFINANCE adjusts only for splits (not dividends),
which is the correct convention for Schedule FA — while yfinance's auto_adjust
silently dividend-adjusts historical prices.

Schedule FA in Indian ITR is reported on a CALENDAR-YEAR basis (Jan 1 – Dec 31).

Usage:
    python main_v2.py holdings.csv --prices prices.csv
    python main_v2.py holdings.csv --prices prices.csv --cy-start 2024-01-01 --cy-end 2024-12-31
    python main_v2.py holdings.csv --prices prices.csv --skip-update

holdings.csv columns (same as main.py):
    symbol, units, acquisition_date, acquisition_price, company_name, address, zip_code
    Optional: nature, country, country_code,
              units_at_year_end (defaults to units if not set),
              dividends_usd, proceeds_usd

prices.csv columns:
    symbol, peak_price, peak_date, closing_price
    Optional: initial_price   (close on CY_START — only needed for carry-forward holdings)
"""

import argparse
import logging
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("schedule_fa.log", mode="w"),
    ],
)
log = logging.getLogger(__name__)

# ─── Global Config (overridden by CLI args) ───────────────────────────────────
# Schedule FA reports on the CALENDAR year (Jan 1 – Dec 31), not the Indian FY.
CY_START = datetime(2024, 1, 1)
CY_END = datetime(2024, 12, 31)

BASE_DIR = Path(__file__).parent
REPO_ROOT = BASE_DIR.parent
RATEKEEPER_REPO = "https://github.com/sahilgupta/sbi-fx-ratekeeper"
RATEKEEPER_DIR = REPO_ROOT / "sbi-fx-ratekeeper"

# Cached SBI rate data by currency
_RATE_CACHE: dict[str, pd.DataFrame] = {}

# Loaded prices.csv keyed by symbol
_PRICES: dict[str, dict] = {}


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
    """Load the manually-supplied per-symbol price CSV into _PRICES."""
    df = pd.read_csv(prices_csv)
    df.columns = df.columns.str.strip().str.lower()

    required = {"symbol", "peak_price", "peak_date", "closing_price"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(
            f"Prices CSV {prices_csv} is missing required columns: {missing}.\n"
            f"Found columns: {list(df.columns)}"
        )

    for _, row in df.iterrows():
        symbol = str(row["symbol"]).strip().upper()
        peak_date = pd.to_datetime(row["peak_date"]).to_pydatetime()
        entry = {
            "peak_price": float(row["peak_price"]),
            "peak_date": peak_date,
            "closing_price": float(row["closing_price"]),
        }
        if "initial_price" in df.columns and pd.notna(row.get("initial_price")):
            entry["initial_price"] = float(row["initial_price"])
        _PRICES[symbol] = entry

    log.info(f"Loaded prices for {len(_PRICES)} symbol(s) from {prices_csv}")


def _get_price_entry(symbol: str) -> dict:
    if symbol not in _PRICES:
        raise ValueError(
            f"{symbol}: no price entry in prices CSV. "
            f"Add a row with peak_price, peak_date, closing_price for this symbol."
        )
    return _PRICES[symbol]


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


def process_row(row: pd.Series) -> dict:
    """Compute all Schedule FA fields (in INR) for one holding."""
    symbol = str(row["symbol"]).strip().upper()
    units = float(row["units"])
    # acquisition_date in the input CSV is expected as YYYY-MM-DD (e.g. 2024-03-01)
    acq_date = pd.to_datetime(row["acquisition_date"]).to_pydatetime()

    log.info(f"\n{'─' * 60}")
    log.info(f"  {symbol}  |  units={units}  |  acquired={acq_date.date()}")

    prices = _get_price_entry(symbol)

    # 1. Initial value of the investment
    #    • Acquired during the CY  → IB acquisition price × SBI rate on acq_date × units.
    #    • Held before CY (carry-forward) → close on CY_START × SBI rate on Jan 1
    #      × units (held at start of CY).
    if acq_date >= CY_START:
        initial_price_usd = float(row["acquisition_price"])
        initial_date = acq_date
        initial_source = "IB acquisition price"
    else:
        if "initial_price" not in prices:
            raise ValueError(
                f"{symbol}: carry-forward holding (acquired {acq_date.date()}, "
                f"before CY_START {CY_START.date()}) needs an `initial_price` "
                f"column in prices CSV (closing price on {CY_START.date()})."
            )
        initial_price_usd = prices["initial_price"]
        initial_date = CY_START
        initial_source = f"prices.csv close on {CY_START.date()}"
    initial_units = units
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
    peak_inr = round(peak_price_usd * peak_fx * units, 2)
    log.info(
        f"  Peak:     ${peak_price_usd:.4f} on {peak_date.date()} (prices.csv) "
        f"× ₹{peak_fx:.4f} × {units} = ₹{peak_inr:,.2f}  (SBI date used: {peak_fx_date.date()})"
    )

    # 3. Closing balance — price on Dec 31 from prices CSV
    # Sanity check: if there were sale proceeds during the CY, the user MUST
    # set units_at_year_end explicitly (else we silently use `units`, which
    # would double-count the sold shares in the closing balance).
    proceeds_usd_val = float(row.get("proceeds_usd", 0) or 0)
    has_year_end = (
        "units_at_year_end" in row.index and pd.notna(row.get("units_at_year_end"))
    )
    if proceeds_usd_val > 0 and not has_year_end:
        raise ValueError(
            f"{symbol}: proceeds_usd={proceeds_usd_val} indicates a sale during the CY, "
            "but units_at_year_end is not set. Provide units_at_year_end explicitly "
            "(0 if fully sold, remaining units if partial sale)."
        )
    closing_units = float(row.get("units_at_year_end", units))
    if closing_units > 0:
        closing_price_usd = prices["closing_price"]
        closing_fx, closing_fx_date = get_sbi_tt_buy(CY_END)
        closing_inr = round(closing_price_usd * closing_fx * closing_units, 2)
        log.info(
            f"  Closing:  ${closing_price_usd:.4f} (prices.csv) × ₹{closing_fx:.4f} × {closing_units} "
            f"= ₹{closing_inr:,.2f}  (SBI date used: {closing_fx_date.date()})"
        )
    else:
        closing_inr = 0.0
        log.info("  Closing:  ₹0  (position fully sold/closed during CY)")

    # 4. Dividends & proceeds (USD → INR at SBI TT Buy on CY_END = Dec 31)
    cy_end_fx, _ = get_sbi_tt_buy(CY_END)
    dividends_inr = round(float(row.get("dividends_usd", 0) or 0) * cy_end_fx, 2)
    proceeds_inr = round(float(row.get("proceeds_usd", 0) or 0) * cy_end_fx, 2)
    log.info(f"  Dividends: ₹{dividends_inr:,.2f}   Proceeds: ₹{proceeds_inr:,.2f}")

    return {
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


def generate(input_csv: Path, output_csv: Path) -> None:
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

    rows, errors, units_records = [], [], []
    for i, (_, row) in enumerate(df.iterrows(), 1):
        try:
            rows.append(process_row(row))
            units_val = float(row["units"])
            end_raw = row.get("units_at_year_end")
            end_val = float(end_raw) if pd.notna(end_raw) else units_val
            units_records.append(
                {
                    "symbol": str(row["symbol"]).strip().upper(),
                    "start": units_val,
                    "end": end_val,
                }
            )
        except Exception as e:
            log.error(f"Row {i} ({row.get('symbol', '?')}): {e}")
            errors.append((i, row.get("symbol", "?"), str(e)))

    out_df = pd.DataFrame(rows, columns=SCHEDULE_FA_COLS)
    out_df.to_csv(output_csv, index=False)

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
    print(f"📋  Log   → {(BASE_DIR / 'schedule_fa.log').resolve()}")


# ─── CLI ──────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate Schedule FA CSV for Indian ITR from IB holdings "
        "using a manually-supplied prices CSV (no yfinance)."
    )
    parser.add_argument("input_csv", help="Path to input holdings CSV")
    parser.add_argument(
        "--prices",
        required=True,
        help="Path to prices CSV (symbol, peak_price, peak_date, closing_price[, initial_price])",
    )
    parser.add_argument(
        "--output",
        default="schedule_fa_output.csv",
        help="Output CSV filename (default: schedule_fa_output.csv)",
    )
    parser.add_argument(
        "--cy-start",
        default="2024-01-01",
        help="Calendar year start date (default: 2024-01-01)",
    )
    parser.add_argument(
        "--cy-end",
        default="2024-12-31",
        help="Calendar year end date   (default: 2024-12-31)",
    )
    parser.add_argument(
        "--skip-update", action="store_true", help="Skip git pull for sbi-fx-ratekeeper"
    )
    args = parser.parse_args()

    global CY_START, CY_END
    CY_START = datetime.strptime(args.cy_start, "%Y-%m-%d")
    CY_END = datetime.strptime(args.cy_end, "%Y-%m-%d")

    log.info(f"Calendar Year: {CY_START.date()} → {CY_END.date()}")

    setup_ratekeeper(skip_update=args.skip_update)

    prices_csv = Path(args.prices)
    if not prices_csv.exists():
        log.error(f"Prices file not found: {prices_csv}")
        sys.exit(1)
    load_prices(prices_csv)

    input_csv = Path(args.input_csv)
    output_csv = BASE_DIR / args.output

    if not input_csv.exists():
        log.error(f"Input file not found: {input_csv}")
        sys.exit(1)

    generate(input_csv, output_csv)


if __name__ == "__main__":
    main()

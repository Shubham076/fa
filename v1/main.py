#!/usr/bin/env python3
"""
Schedule FA Generator for Indian ITR
Converts Interactive Brokers US equity holdings → Schedule FA CSV (INR)

Usage:
    python3 main.py input.csv
    python3 main.py input.csv --year 2024
    python3 main.py input.csv --skip-update   # skip git pull

Schedule FA in Indian ITR is reported on a CALENDAR-YEAR basis (Jan 1 – Dec 31)
of the calendar year ending during the previous year. For AY 2025-26 the
reporting period is CY 2024 (2024-01-01 → 2024-12-31).

Input CSV columns (see input_template.csv):
    symbol, units, acquisition_date, acquisition_price, company_name, address, zip_code
    Optional: nature, country, country_code,
              units_at_year_end (defaults to units if not set),
              dividends_usd, proceeds_usd
"""

import argparse
import logging
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import certifi
import pandas as pd
from curl_cffi import requests as _curl_requests  # noqa: E402
import yfinance as yf  # noqa: E402

_YF_SESSION = _curl_requests.Session(
    impersonate="chrome", verify=certifi.where()
)

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
    """Locate the ratekeeper CSV for a currency."""
    csv_path = RATEKEEPER_DIR / "csv_files" / f"SBI_REFERENCE_RATES_{currency}.csv"
    if csv_path.exists():
        return csv_path

    matches = list(RATEKEEPER_DIR.rglob(f"SBI_REFERENCE_RATES_{currency}.csv"))
    if matches:
        return matches[0]

    raise FileNotFoundError(
        f"No SBI reference-rate CSV found for {currency} in {RATEKEEPER_DIR}.\n"
        "Did the clone succeed? Check the sbi-fx-ratekeeper/csv_files directory."
    )


def _load_rates(currency: str = "USD") -> pd.DataFrame:
    """Load and normalize SBI reference rates from the ratekeeper CSV."""
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
    """
    Return (rate_inr, actual_date) for SBI TT Buy USD → INR.
    If no rate exists on target date, tries up to 10 subsequent days.
    """
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


# ─── Stock Price (via yfinance) ───────────────────────────────────────────────


def _history(symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    """Download OHLCV history with retries."""
    ticker = yf.Ticker(symbol, session=_YF_SESSION)
    for _ in range(3):
        hist = ticker.history(
            start=start.strftime("%Y-%m-%d"),
            end=(end + timedelta(days=1)).strftime("%Y-%m-%d"),
            auto_adjust=True,
        )
        if not hist.empty:
            return hist
    raise ValueError(
        f"No price data from yfinance for '{symbol}' "
        f"between {start.date()} and {end.date()}. "
        f"Is the ticker symbol correct?"
    )


def get_closing_price(symbol: str, target: datetime) -> float:
    """
    Closing price on or nearest to target date.
    Falls forward to next trading day if target is a weekend/holiday.
    """
    hist = _history(symbol, target - timedelta(days=5), target + timedelta(days=5))
    forward = hist[hist.index.date >= target.date()]
    subset = forward if not forward.empty else hist
    return float(subset.iloc[0]["Close"])


def get_peak_in_period(
    symbol: str, start: datetime, end: datetime
) -> tuple[float, datetime]:
    """
    Return (highest intraday High, date of that High) across [start, end].
    Uses the High column to capture true peak, not just closing prices.
    """
    hist = _history(symbol, start, end)
    idx = hist["High"].idxmax()
    return float(hist.loc[idx, "High"]), idx.to_pydatetime()


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

    # 1. Initial value of the investment
    #    • Acquired during the CY  → Acquisition price × SBI rate on acq_date × units.
    #    • Held before CY (carry-forward) → close on first CY trading day × SBI rate
    #      on Jan 1 × units (held at start of CY).
    if acq_date >= CY_START:
        initial_price_usd = float(row["acquisition_price"])
        initial_date = acq_date
        initial_source = "Acquisition price"
    else:
        initial_price_usd = get_closing_price(symbol, CY_START)
        initial_date = CY_START
        initial_source = f"yfinance close on {CY_START.date()}"
    initial_units = units
    initial_fx, initial_fx_date = get_sbi_tt_buy(initial_date)
    initial_inr = round(initial_price_usd * initial_fx * initial_units, 2)
    log.info(
        f"  Initial:  ${initial_price_usd:.4f} ({initial_source}) × ₹{initial_fx:.4f} "
        f"× {initial_units} = ₹{initial_inr:,.2f}  (SBI date used: {initial_fx_date.date()})"
    )

    # 2. Peak value — highest intraday high across the full CY (CY_START → CY_END)
    peak_price_usd, peak_date = get_peak_in_period(symbol, CY_START, CY_END)
    peak_fx, peak_fx_date = get_sbi_tt_buy(peak_date)
    peak_inr = round(peak_price_usd * peak_fx * units, 2)
    log.info(
        f"  Peak:     ${peak_price_usd:.4f} on {peak_date.date()} (yfinance) "
        f"× ₹{peak_fx:.4f} × {units} = ₹{peak_inr:,.2f}  (SBI date used: {peak_fx_date.date()})"
    )

    # 3. Closing balance — price on Dec 31 of the calendar year (CY_END)
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
    closing_units = float(row.get("units_at_year_end", units))  # 0 if fully sold
    if closing_units > 0:
        closing_price_usd = get_closing_price(symbol, CY_END)
        closing_fx, closing_fx_date = get_sbi_tt_buy(CY_END)
        closing_inr = round(closing_price_usd * closing_fx * closing_units, 2)
        log.info(
            f"  Closing:  ${closing_price_usd:.4f} (yfinance) × ₹{closing_fx:.4f} × {closing_units} "
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
        description="Generate Schedule FA CSV for Indian ITR from IB holdings."
    )
    parser.add_argument("input_csv", help="Path to input holdings CSV")
    parser.add_argument(
        "--output",
        default="schedule_fa_output.csv",
        help="Output CSV filename (default: schedule_fa_output.csv)",
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

    global CY_START, CY_END
    CY_START = datetime(args.year, 1, 1)
    CY_END = datetime(args.year, 12, 31)

    log.info(f"Calendar Year: {CY_START.date()} → {CY_END.date()}")

    setup_ratekeeper(skip_update=args.skip_update)

    input_csv = Path(args.input_csv)
    output_csv = BASE_DIR / args.output

    if not input_csv.exists():
        log.error(f"Input file not found: {input_csv}")
        sys.exit(1)

    generate(input_csv, output_csv)


if __name__ == "__main__":
    main()

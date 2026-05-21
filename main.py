#!/usr/bin/env python3
"""
Schedule FA Generator for Indian ITR
Converts Interactive Brokers US equity holdings → Schedule FA CSV (INR)

Usage:
    python generate_schedule_fa.py holdings.csv
    python generate_schedule_fa.py holdings.csv --fy-start 2024-04-01 --fy-end 2025-03-31
    python generate_schedule_fa.py holdings.csv --skip-update   # skip git pull

Input CSV columns (see input_template.csv):
    symbol, units, acquisition_date, acquisition_price, company_name, address, zip_code
    Optional: nature, country, country_code, units_at_year_end, dividends_usd, proceeds_usd
"""

import argparse
import logging
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

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
FY_START = datetime(2024, 4, 1)
FY_END = datetime(2025, 3, 31)

BASE_DIR = Path(__file__).parent
RATEKEEPER_REPO = "https://github.com/sahilgupta/sbi-fx-ratekeeper"
RATEKEEPER_DIR = BASE_DIR / "sbi-fx-ratekeeper"

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
    target_date = pd.Timestamp(target).normalize()
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
    ticker = yf.Ticker(symbol)
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
    acq_date = pd.to_datetime(row["acquisition_date"]).to_pydatetime()

    log.info(f"\n{'─' * 60}")
    log.info(f"  {symbol}  |  units={units}  |  acquired={acq_date.date()}")

    # ── 1. Initial value — acquisition price from IB (user-provided) ──────────
    price_acq = float(row["acquisition_price"])
    rate_acq, d_acq = get_sbi_tt_buy(acq_date)
    initial_inr = round(price_acq * rate_acq * units, 2)
    log.info(
        f"  Initial:  ${price_acq:.4f} (IB) × ₹{rate_acq:.4f} × {units} "
        f"= ₹{initial_inr:,.2f}  (SBI date used: {d_acq.date()})"
    )

    # ── 2. Peak value — highest intraday high during FY via yfinance ──────────
    period_start = max(FY_START, acq_date)
    peak_price, peak_dt = get_peak_in_period(symbol, period_start, FY_END)
    rate_peak, d_peak = get_sbi_tt_buy(peak_dt)
    peak_inr = round(peak_price * rate_peak * units, 2)
    log.info(
        f"  Peak:     ${peak_price:.4f} on {peak_dt.date()} (yfinance) "
        f"× ₹{rate_peak:.4f} × {units} = ₹{peak_inr:,.2f}  (SBI date used: {d_peak.date()})"
    )

    # ── 3. Closing balance — Mar 31 price via yfinance ────────────────────────
    units_close = float(row.get("units_at_year_end", units))  # 0 if fully sold
    if units_close > 0:
        price_close = get_closing_price(symbol, FY_END)
        rate_close, d_cl = get_sbi_tt_buy(FY_END)
        closing_inr = round(price_close * rate_close * units_close, 2)
        log.info(
            f"  Closing:  ${price_close:.4f} (yfinance) × ₹{rate_close:.4f} × {units_close} "
            f"= ₹{closing_inr:,.2f}  (SBI date used: {d_cl.date()})"
        )
    else:
        closing_inr = 0.0
        log.info("  Closing:  ₹0  (position fully sold/closed during FY)")

    # ── 4. Dividends & proceeds (USD → INR at FY-end rate) ───────────────────
    rate_fy_end, _ = get_sbi_tt_buy(FY_END)

    if "dividends_inr" in row.index and pd.notna(row.get("dividends_inr")):
        div_inr = float(row["dividends_inr"])
    else:
        div_inr = round(float(row.get("dividends_usd", 0) or 0) * rate_fy_end, 2)

    if "proceeds_inr" in row.index and pd.notna(row.get("proceeds_inr")):
        proc_inr = float(row["proceeds_inr"])
    else:
        proc_inr = round(float(row.get("proceeds_usd", 0) or 0) * rate_fy_end, 2)

    log.info(f"  Dividends: ₹{div_inr:,.2f}   Proceeds: ₹{proc_inr:,.2f}")

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
        "Total gross amount paid/credited with respect to the holding during the period": div_inr,
        "Total gross proceeds from sale or redemption of investment during the period": proc_inr,
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

    rows, errors = [], []
    for i, (_, row) in enumerate(df.iterrows(), 1):
        try:
            rows.append(process_row(row))
        except Exception as e:
            log.error(f"Row {i} ({row.get('symbol', '?')}): {e}")
            errors.append((i, row.get("symbol", "?"), str(e)))

    out_df = pd.DataFrame(rows, columns=SCHEDULE_FA_COLS)
    out_df.to_csv(output_csv, index=False)

    print(f"\n{'═' * 70}")
    print("SCHEDULE FA — FINAL OUTPUT")
    print(f"{'═' * 70}")
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 200)
    pd.set_option("display.float_format", "{:,.2f}".format)
    print(out_df.to_string(index=False))

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
        "--fy-start",
        default="2024-04-01",
        help="Financial year start date (default: 2024-04-01)",
    )
    parser.add_argument(
        "--fy-end",
        default="2025-03-31",
        help="Financial year end date   (default: 2025-03-31)",
    )
    parser.add_argument(
        "--skip-update", action="store_true", help="Skip git pull for sbi-fx-ratekeeper"
    )
    args = parser.parse_args()

    global FY_START, FY_END
    FY_START = datetime.strptime(args.fy_start, "%Y-%m-%d")
    FY_END = datetime.strptime(args.fy_end, "%Y-%m-%d")

    log.info(f"Financial Year: {FY_START.date()} → {FY_END.date()}")

    setup_ratekeeper(skip_update=args.skip_update)

    input_csv = Path(args.input_csv)
    output_csv = BASE_DIR / args.output

    if not input_csv.exists():
        log.error(f"Input file not found: {input_csv}")
        sys.exit(1)

    generate(input_csv, output_csv)


if __name__ == "__main__":
    main()

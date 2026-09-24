#!/usr/bin/env python3
"""
Cash-only peak & closing balance calculator for a foreign custodial account
(E*TRADE / Morgan Stanley "Client Statement" PDF), for Indian ITR Schedule FA.

Why this exists:
    Statement month/period-end "CLOSING CASH" is only a *snapshot*. The true
    peak balance can occur mid-month when RSU sell-to-cover proceeds land in
    cash for a few days before the tax is swept out. To get the real peak we
    rebuild a daily running balance from the transaction-level
    "CASH FLOW ACTIVITY BY DATE" section and take the maximum.

What it does:
    1. Extracts text from the PDF (stdlib only: zlib inflate of FlateDecode
       streams — no external deps).
    2. Parses every dated cash-affecting transaction:
         + Sold  (sell-to-cover proceeds, booked on SETTLEMENT date)
         - Funds Paid SP Company Due (tax paid out)
         - Funds Transferred WIRE OUT
         - Service Fee
         + Interest Income
    3. Builds a chronological running balance.
    4. Prints a per-transaction ledger and a month-by-month comparison
       (monthly peak vs monthly closing).
    5. Reports the overall PEAK (with date) and CLOSING balance.
    6. Optionally converts to INR at SBI TT-Buy (peak → rate on peak date,
       closing → rate on Dec 31) and compares against expected values.

Usage:
    python3 cash_balance.py /path/to/ClientStatements.pdf
    python3 cash_balance.py /path/to/ClientStatements.pdf --year 2025 --inr
    python3 cash_balance.py stmt.pdf --expect-peak 6166.49 --expect-closing 60.16
"""

import argparse
import re
import sys
import zlib
from datetime import date
from pathlib import Path

BASE_DIR = Path(__file__).parent
REPO_ROOT = BASE_DIR.parent
RATEKEEPER_DIR = REPO_ROOT / "sbi-fx-ratekeeper"

MONTHS = [
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


# ─── PDF text extraction (stdlib only) ────────────────────────────────────────


def extract_pdf_text(pdf_path: Path) -> str:
    data = pdf_path.read_bytes()
    chunks = []
    for m in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", data, re.S):
        try:
            raw = zlib.decompress(m.group(1))
        except zlib.error:
            continue
        parts = [t.group(0)[1:-1] for t in re.finditer(rb"\((?:[^()\\]|\\.)*\)", raw)]
        if parts:
            chunks.append(b" ".join(parts))
    text = b"\n".join(chunks).decode("latin-1")
    return text.replace("\\(", "(").replace("\\)", ")")


# ─── Transaction parsing ──────────────────────────────────────────────────────

NUM2 = r"[\d,]+\.\d{2}"


def _to_date(md: str, year: int) -> date:
    mo, d = md.split("/")
    return date(year, int(mo), int(d))


def _f(s: str) -> float:
    return float(s.replace(",", ""))


def parse_transactions(text: str, year: int) -> list[tuple[date, float, str, str]]:
    """Return sorted list of (date, delta_usd, kind, note) from all
    'CASH FLOW ACTIVITY BY DATE' sections."""
    starts = [m.start() for m in re.finditer(r"CASH FLOW ACTIVITY BY DATE", text)]
    starts.append(len(text))
    txns: list[tuple[date, float, str, str]] = []

    for i in range(len(starts) - 1):
        block = text[starts[i]:starts[i + 1]]
        # Stop before the MMF/BDP internal transfers to avoid double counting
        cut = block.find("MONEY MARKET FUND")
        b = block[:cut] if cut > 0 else block

        # Sold: "<trade> <settle> Sold ... <qty.3dp> $ <price.4dp> $ <amount.2dp>"
        # Cash lands on the SETTLEMENT date (2nd date).
        for m in re.finditer(
            r"(\d{1,2}/\d{1,2})\s+(\d{1,2}/\d{1,2})\s+Sold "
            r".*?\d+\.\d{3}\s+\$?\s*[\d,]+\.\d{4}\s+\$?\s*(" + NUM2 + r")",
            b,
        ):
            txns.append((_to_date(m.group(2), year), +_f(m.group(3)), "Sold", "sell-to-cover proceeds"))

        for m in re.finditer(
            r"(\d{1,2}/\d{1,2})\s+Funds Paid SP Company Due[^()]*\((" + NUM2 + r")\)", b
        ):
            txns.append((_to_date(m.group(1), year), -_f(m.group(2)), "TaxPaid", "Funds Paid SP Company Due"))

        for m in re.finditer(
            r"(\d{1,2}/\d{1,2})\s+Funds Transferred WIRE OUT\s*\((" + NUM2 + r")\)", b
        ):
            txns.append((_to_date(m.group(1), year), -_f(m.group(2)), "WireOut", "Funds Transferred WIRE OUT"))

        for m in re.finditer(
            r"(\d{1,2}/\d{1,2})\s+Service Fee[^()]*\((" + NUM2 + r")\)", b
        ):
            txns.append((_to_date(m.group(1), year), -_f(m.group(2)), "Fee", "Service Fee"))

        for m in re.finditer(
            r"(\d{1,2}/\d{1,2})\s+Interest Income.*?(\d+\.\d{2})(?!\d)", b
        ):
            txns.append((_to_date(m.group(1), year), +_f(m.group(2)), "Interest", "Interest Income"))

    # Deduplicate (a period may be repeated across statement copies) then sort.
    txns = sorted(set(txns), key=lambda t: (t[0], -t[1]))
    return txns


# ─── SBI FX (optional) ────────────────────────────────────────────────────────


def sbi_tt_buy(target: date):
    import pandas as pd

    csv = RATEKEEPER_DIR / "csv_files" / "SBI_REFERENCE_RATES_USD.csv"
    if not csv.exists():
        matches = list(RATEKEEPER_DIR.rglob("SBI_REFERENCE_RATES_USD.csv"))
        if not matches:
            raise FileNotFoundError(f"SBI USD rate CSV not found under {RATEKEEPER_DIR}")
        csv = matches[0]
    r = pd.read_csv(csv)
    r.columns = r.columns.str.strip().str.upper()
    r["DATE"] = pd.to_datetime(r["DATE"]).dt.normalize()
    r["TT BUY"] = pd.to_numeric(r["TT BUY"], errors="coerce")
    r = r.dropna(subset=["DATE", "TT BUY"])
    r = r[r["TT BUY"] > 0].sort_values("DATE").drop_duplicates("DATE", keep="last")
    ts = pd.Timestamp(target)
    win = r[(r["DATE"] >= ts) & (r["DATE"] <= ts + pd.Timedelta(days=10))]
    if win.empty:
        raise ValueError(f"No SBI TT Buy rate near {target}")
    row = win.iloc[0]
    return float(row["TT BUY"]), row["DATE"].date()


# ─── Core ─────────────────────────────────────────────────────────────────────


def run_ledger(txns, year):
    """Return (ledger, monthly, peak_bal, peak_date, closing_bal)."""
    bal = 0.0
    peak = 0.0
    peak_date = None
    ledger = []
    # monthly[m] = {"peak": x, "close": x}
    monthly = {m: {"peak": 0.0, "close": 0.0, "seen": False} for m in range(1, 13)}

    for d, amt, kind, note in txns:
        bal = round(bal + amt, 2)
        if bal > peak:
            peak = bal
            peak_date = d
        ledger.append((d, amt, bal, kind, note))
        mo = d.month
        monthly[mo]["seen"] = True
        if bal > monthly[mo]["peak"]:
            monthly[mo]["peak"] = bal

    # Month-end closing = running balance as of the last txn on/before month end.
    running = 0.0
    idx = 0
    stxns = txns
    for mo in range(1, 13):
        month_end = date(year, mo, 28)  # any day; we compare by <= last day
        # advance running through all txns in this month
        while idx < len(stxns) and stxns[idx][0].month <= mo:
            running = round(running + stxns[idx][1], 2)
            idx += 1
        monthly[mo]["close"] = running

    closing_bal = ledger[-1][2] if ledger else 0.0
    return ledger, monthly, peak, peak_date, closing_bal


def main() -> None:
    ap = argparse.ArgumentParser(description="Cash peak/closing from E*TRADE statement PDF")
    ap.add_argument("pdf", help="Path to Client Statement PDF")
    ap.add_argument("--year", type=int, default=2025)
    ap.add_argument("--inr", action="store_true", help="Convert to INR via SBI TT Buy")
    ap.add_argument("--expect-peak", type=float, default=None, help="Expected peak USD to verify")
    ap.add_argument("--expect-closing", type=float, default=None, help="Expected closing USD to verify")
    args = ap.parse_args()

    pdf = Path(args.pdf)
    if not pdf.exists():
        print(f"PDF not found: {pdf}")
        sys.exit(1)

    text = extract_pdf_text(pdf)
    txns = parse_transactions(text, args.year)
    if not txns:
        print("No cash-flow transactions parsed. Is this the right statement PDF?")
        sys.exit(1)

    ledger, monthly, peak, peak_date, closing = run_ledger(txns, args.year)

    print("=" * 78)
    print(f"CASH LEDGER (running balance, USD) — {args.year}")
    print("=" * 78)
    print(f"{'date':<12}{'delta':>12}{'balance':>12}  {'type':<9} note")
    for d, amt, bal, kind, note in ledger:
        print(f"{d.isoformat():<12}{amt:>12,.2f}{bal:>12,.2f}  {kind:<9} {note}")

    print("\n" + "=" * 78)
    print("MONTHLY COMPARISON (USD)")
    print("=" * 78)
    print(f"{'Month':<12}{'Month Peak':>15}{'Month Close':>15}")
    year_peak_month = None
    for mo in range(1, 13):
        if not monthly[mo]["seen"] and monthly[mo]["close"] == (
            monthly[mo - 1]["close"] if mo > 1 else 0.0
        ):
            # still show carried-forward closing
            pass
        p = monthly[mo]["peak"]
        c = monthly[mo]["close"]
        mark = ""
        if abs(p - peak) < 1e-6 and monthly[mo]["seen"]:
            mark = "  <= YEAR PEAK"
            year_peak_month = mo
        print(f"{MONTHS[mo]:<12}{p:>15,.2f}{c:>15,.2f}{mark}")

    print("\n" + "=" * 78)
    print("RESULT")
    print("=" * 78)
    print(f"  PEAK balance : ${peak:,.2f}  on {peak_date} "
          f"({MONTHS[peak_date.month]})")
    print(f"  CLOSING bal  : ${closing:,.2f}  (as of {ledger[-1][0]})")

    if args.inr:
        pfx, pfx_d = sbi_tt_buy(peak_date)
        cfx, cfx_d = sbi_tt_buy(date(args.year, 12, 31))
        print(f"\n  Peak    : ${peak:,.2f} x {pfx:.4f} (SBI {pfx_d}) = "
              f"₹{round(peak * pfx, 2):,.2f}")
        print(f"  Closing : ${closing:,.2f} x {cfx:.4f} (SBI {cfx_d}) = "
              f"₹{round(closing * cfx, 2):,.2f}")

    if args.expect_peak is not None or args.expect_closing is not None:
        print("\n" + "-" * 78)
        print("VERIFICATION")
        print("-" * 78)
        ok = True
        if args.expect_peak is not None:
            good = abs(peak - args.expect_peak) < 0.01
            ok &= good
            print(f"  peak    expected {args.expect_peak:,.2f} | got {peak:,.2f} "
                  f"| {'OK' if good else 'MISMATCH'}")
        if args.expect_closing is not None:
            good = abs(closing - args.expect_closing) < 0.01
            ok &= good
            print(f"  closing expected {args.expect_closing:,.2f} | got {closing:,.2f} "
                  f"| {'OK' if good else 'MISMATCH'}")
        print("  ALL GOOD ✅" if ok else "  CHECK FAILED ❌")
        sys.exit(0 if ok else 2)


if __name__ == "__main__":
    main()

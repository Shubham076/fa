import importlib.util
import io
import sys
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

V1_DIR = Path(__file__).resolve().parents[1]

INPUT_HEADER = (
    "symbol,benefit_type,units,acquisition_date,acquisition_price,company_name,"
    "address,zip_code,nature,country,country_code,dividends_usd\n"
)
SALES_HEADER = "symbol,benefit_type,sale_date,units,sale_price\n"

# Synthetic SBI TT Buy rates used across scenarios.
RATES = {
    "2025-06-02": 80.0,
    "2025-10-01": 82.0,
    "2026-01-02": 83.0,
    "2026-03-02": 90.0,
    "2026-04-01": 85.0,
    "2026-05-04": 88.0,
    "2026-12-31": 100.0,
}
# Mocked yfinance for 2026: close $14 on Jan 1 (carry-forward initial, fx 83 on Jan 2),
# peak $20 on 2026-03-02 (fx 90), close $15 on Dec 31 (fx 100).
JAN1_CLOSE = 14.0
PEAK = (20.0, datetime(2026, 3, 2))
DEC31_CLOSE = 15.0


def _load_main():
    name = "fa_v1_main"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, V1_DIR / "main.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


@pytest.fixture
def fa(monkeypatch, tmp_path):
    module = _load_main()
    monkeypatch.setattr(module, "CY_START", datetime(2026, 1, 1))
    monkeypatch.setattr(module, "CY_END", datetime(2026, 12, 31))
    monkeypatch.setattr(module, "INPUTS_DIR", tmp_path / "inputs")
    monkeypatch.setattr(module, "OUTPUTS_DIR", tmp_path / "outputs")
    monkeypatch.setattr(module, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(module, "LOG_FILE", tmp_path / "logs" / "schedule_fa.log")
    monkeypatch.setattr(module, "_RATE_CACHE", {})
    return module


@pytest.fixture
def set_rates(fa):
    def _set(rates=None):
        rates = RATES if rates is None else rates
        fa._RATE_CACHE["USD"] = pd.DataFrame(
            {"DATE": pd.to_datetime(list(rates)), "TT BUY": [float(v) for v in rates.values()]}
        ).sort_values("DATE")

    return _set


@pytest.fixture
def market(fa, monkeypatch, set_rates):
    """Mock yfinance lookups and SBI rates for CY 2026."""
    set_rates()

    def closing(symbol, target):
        return JAN1_CLOSE if target.month == 1 else DEC31_CLOSE

    monkeypatch.setattr(fa, "get_closing_price", closing)
    monkeypatch.setattr(fa, "get_peak_in_period", lambda symbol, start, end: PEAK)


@pytest.fixture
def write(tmp_path):
    def _write(name, content):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    return _write


def _input_line(symbol="S", benefit_type="SO", units=10, acq_date="2025-06-02", price=12, dividends=0):
    return (
        f"{symbol},{benefit_type},{units},{acq_date},{price},Co,Addr,94041,Company,"
        f"UNITED STATES OF AMERICA,2,{dividends}\n"
    )


@pytest.fixture
def input_csv():
    def _build(*lots):
        return INPUT_HEADER + "".join(_input_line(**lot) for lot in lots)

    return _build


@pytest.fixture
def lot_row():
    def _row(units=10, acq_date="2025-06-02", price=12.0, symbol="S", benefit_type="SO", dividends=0):
        return pd.Series(
            {
                "symbol": symbol,
                "benefit_type": benefit_type,
                "units": units,
                "acquisition_date": acq_date,
                "acquisition_price": price,
                "company_name": "Co",
                "address": "Addr",
                "zip_code": 94041,
                "nature": "Company",
                "country": "UNITED STATES OF AMERICA",
                "country_code": 2,
                "dividends_usd": dividends,
            }
        )

    return _row


@pytest.fixture
def sale():
    def _sale(date, units, price):
        return {
            "sale_date": datetime.fromisoformat(date),
            "units": float(units),
            "sale_price": float(price),
        }

    return _sale


@pytest.fixture
def make_lots(fa):
    def _make(rows):
        df = pd.DataFrame(rows, columns=["symbol", "benefit_type", "units", "acquisition_date"])
        df["symbol_norm"] = df["symbol"].astype(str).str.strip().str.upper()
        df["benefit_type_norm"] = df["benefit_type"].map(fa._benefit_type)
        df["acq_date"] = pd.to_datetime(df["acquisition_date"])
        return df

    return _make


@pytest.fixture
def run_generate(fa, market, write, tmp_path):
    """Write inputs, run generate() and return (schedule_fa_df, audit_df)."""

    def _run(input_text, sales_text=None):
        sales_path = write("sales.csv", sales_text) if sales_text is not None else None
        output = tmp_path / "out.csv"
        with redirect_stdout(io.StringIO()):
            fa.generate(write("input.csv", input_text), output, sales_path)
        return pd.read_csv(output), pd.read_csv(tmp_path / "out_audit_trail.csv")

    return _run


@pytest.fixture
def cols():
    return {
        "initial": "Initial value of the investment",
        "peak": "Peak value of investment during the Period",
        "closing": "Closing balance",
        "dividends": "Total gross amount paid/credited with respect to the holding during the period",
        "proceeds": "Total gross proceeds from sale or redemption of investment during the period",
        "sales_header": SALES_HEADER,
    }

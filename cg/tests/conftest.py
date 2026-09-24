import importlib.util
import io
import logging
import sys
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

CG_DIR = Path(__file__).resolve().parents[1]

SALES_HEADER = (
    "symbol,benefit_type,acquisition_date,sale_date,units,proceeds_usd,cost_basis_usd,fees_usd\n"
)

# Synthetic SBI TT Buy rates (Rule 115 = last day of the previous month).
# 2024-03-31 is a Sunday: the latest earlier rate (2024-03-28) is used.
RATES = {
    "2024-03-28": 83.0,
    "2025-03-31": 85.0,
    "2026-02-28": 91.0,
    "2026-03-31": 92.5,
    "2026-08-31": 95.0,
    "2026-11-30": 96.0,
    "2027-02-28": 97.0,
}


def _load_main():
    name = "cg_main"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, CG_DIR / "main.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


@pytest.fixture
def cg(monkeypatch, tmp_path):
    module = _load_main()
    monkeypatch.setattr(module, "FY_START", datetime(2026, 4, 1))
    monkeypatch.setattr(module, "FY_END", datetime(2027, 3, 31))
    monkeypatch.setattr(module, "INPUTS_DIR", tmp_path / "inputs")
    monkeypatch.setattr(module, "OUTPUTS_DIR", tmp_path / "outputs")
    monkeypatch.setattr(module, "RATEKEEPER_DIR", tmp_path / "rk")
    monkeypatch.setattr(module, "_RATE_CACHE", {})
    return module


@pytest.fixture
def set_rates(cg):
    def _set(rates=None):
        rates = RATES if rates is None else rates
        cg._RATE_CACHE["USD"] = pd.DataFrame(
            {"DATE": pd.to_datetime(list(rates)), "TT BUY": [float(v) for v in rates.values()]}
        ).sort_values("DATE")

    return _set


@pytest.fixture
def write(tmp_path):
    def _write(name, content):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    return _write


def sale_line(
    acq="2026-04-13", sold="2026-04-13", units=416, price=12.71, cost=12.71, fees=0,
    symbol="S", benefit_type="SO",
):
    """price / cost are per share; the CSV holds row totals like the broker report."""
    return f"{symbol},{benefit_type},{acq},{sold},{units},{units * price:.2f},{units * cost:.2f},{fees}\n"


@pytest.fixture
def header():
    return SALES_HEADER


@pytest.fixture
def line():
    return sale_line


@pytest.fixture
def sales_csv():
    def _build(*sales):
        return SALES_HEADER + "".join(sale_line(**s) for s in sales)

    return _build


@pytest.fixture
def sale_row(cg, write):
    """One validated sale row, as generate() passes it to process_sale()."""

    def _make(**kw):
        path = write("one_sale.csv", SALES_HEADER + sale_line(**kw))
        return cg.load_sales(path).iloc[0]

    return _make


@pytest.fixture
def run_generate(cg, write, set_rates, tmp_path):
    """Write sales, run generate(); return schedule, quarterly, checks, the logged output
    text and the audit trail split into sale rows (audit) and TOTAL rows (totals)."""

    def _run(sales_text, rates=None):
        set_rates(rates)
        output = tmp_path / "cg.txt"
        handler = logging.FileHandler(output, mode="w", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        cg.log.addHandler(handler)
        cg.log.setLevel(logging.INFO)
        try:
            with redirect_stdout(io.StringIO()):
                schedule, quarterly, checks = cg.generate(write("sales.csv", sales_text), output)
        finally:
            cg.log.removeHandler(handler)
            handler.close()
        rows = pd.read_csv(tmp_path / "cg_audit_trail.csv", dtype=str).fillna("")
        is_total = rows["Symbol"] == "TOTAL"
        return SimpleNamespace(
            schedule=schedule,
            quarterly=quarterly,
            checks=checks,
            output=output.read_text(encoding="utf-8"),
            rows=rows,
            audit=rows[~is_total & (rows["Symbol"] != "")].reset_index(drop=True),
            totals=rows[is_total].reset_index(drop=True),
        )

    return _run

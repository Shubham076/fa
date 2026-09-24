import io
import logging
import subprocess
import sys
from contextlib import redirect_stdout
from datetime import datetime

import pandas as pd
import pytest


# ─── _benefit_type ────────────────────────────────────────────────────────────


def test_benefit_type_normalizes(fa):
    assert fa._benefit_type(" so ") == "SO"


def test_benefit_type_blank_values(fa):
    assert fa._benefit_type(float("nan")) == ""
    assert fa._benefit_type(None) == ""


# ─── setup_ratekeeper ─────────────────────────────────────────────────────────


def test_ratekeeper_clones_when_missing(fa, monkeypatch, tmp_path):
    monkeypatch.setattr(fa, "RATEKEEPER_DIR", tmp_path / "rk")
    calls = []
    monkeypatch.setattr(fa.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    fa.setup_ratekeeper()
    assert calls[0][:2] == ["git", "clone"]


def test_ratekeeper_skip_update_does_nothing(fa, monkeypatch, tmp_path):
    monkeypatch.setattr(fa, "RATEKEEPER_DIR", tmp_path)
    calls = []
    monkeypatch.setattr(fa.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    fa.setup_ratekeeper(skip_update=True)
    assert calls == []


def test_ratekeeper_pulls_when_present(fa, monkeypatch, tmp_path):
    monkeypatch.setattr(fa, "RATEKEEPER_DIR", tmp_path)
    calls = []
    monkeypatch.setattr(fa.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    fa.setup_ratekeeper()
    assert "pull" in calls[0]


def _fail(*args, **kwargs):
    raise subprocess.CalledProcessError(1, "git")


def test_ratekeeper_pull_failure_uses_local_data(fa, monkeypatch, tmp_path):
    monkeypatch.setattr(fa, "RATEKEEPER_DIR", tmp_path)
    (tmp_path / "csv_files").mkdir()
    monkeypatch.setattr(fa.subprocess, "run", _fail)
    fa.setup_ratekeeper()


def test_ratekeeper_pull_failure_without_local_data_raises(fa, monkeypatch, tmp_path):
    monkeypatch.setattr(fa, "RATEKEEPER_DIR", tmp_path)
    monkeypatch.setattr(fa.subprocess, "run", _fail)
    with pytest.raises(subprocess.CalledProcessError):
        fa.setup_ratekeeper()


# ─── SBI rates ────────────────────────────────────────────────────────────────


def test_find_rate_csv_standard_location(fa, monkeypatch, write, tmp_path):
    monkeypatch.setattr(fa, "RATEKEEPER_DIR", tmp_path)
    path = write("csv_files/SBI_REFERENCE_RATES_USD.csv", "DATE,TT BUY\n")
    assert fa._find_rate_csv("USD") == path


def test_find_rate_csv_nested_location(fa, monkeypatch, write, tmp_path):
    monkeypatch.setattr(fa, "RATEKEEPER_DIR", tmp_path)
    path = write("other/SBI_REFERENCE_RATES_EUR.csv", "DATE,TT BUY\n")
    assert fa._find_rate_csv("EUR") == path


def test_find_rate_csv_missing_raises(fa, monkeypatch, tmp_path):
    monkeypatch.setattr(fa, "RATEKEEPER_DIR", tmp_path)
    with pytest.raises(FileNotFoundError):
        fa._find_rate_csv("USD")


def test_load_rates_cleans_and_caches(fa, monkeypatch, write, tmp_path):
    monkeypatch.setattr(fa, "RATEKEEPER_DIR", tmp_path)
    path = write(
        "csv_files/SBI_REFERENCE_RATES_USD.csv",
        "date ,tt buy,other\n"
        "2024-01-04,84,x\n"
        "2024-01-02,83.1,x\n"
        "2024-01-01,0,x\n"
        "2024-01-03,abc,x\n"
        "2024-01-02,83.1,x\n",
    )
    rates = fa._load_rates("usd")
    assert list(rates["DATE"].dt.strftime("%Y-%m-%d")) == ["2024-01-02", "2024-01-04"]
    assert list(rates["TT BUY"]) == [83.1, 84.0]
    path.unlink()
    assert fa._load_rates("USD") is rates


def test_load_rates_missing_columns_raises(fa, monkeypatch, write, tmp_path):
    monkeypatch.setattr(fa, "RATEKEEPER_DIR", tmp_path)
    write("csv_files/SBI_REFERENCE_RATES_USD.csv", "DATE,RATE\n2024-01-02,83\n")
    with pytest.raises(ValueError):
        fa._load_rates("USD")


def test_load_rates_no_usable_rates_raises(fa, monkeypatch, write, tmp_path):
    monkeypatch.setattr(fa, "RATEKEEPER_DIR", tmp_path)
    write("csv_files/SBI_REFERENCE_RATES_USD.csv", "DATE,TT BUY\n2024-01-02,0\n")
    with pytest.raises(ValueError):
        fa._load_rates("USD")


@pytest.fixture
def jan_rates(set_rates):
    set_rates({"2024-01-05": 83.0, "2024-01-08": 83.2})


def test_tt_buy_exact_date(fa, jan_rates):
    assert fa.get_sbi_tt_buy(datetime(2024, 1, 5)) == (83.0, datetime(2024, 1, 5))


def test_tt_buy_weekend_uses_next_available(fa, jan_rates):
    assert fa.get_sbi_tt_buy(datetime(2024, 1, 6)) == (83.2, datetime(2024, 1, 8))


def test_tt_buy_time_component_ignored(fa, jan_rates):
    assert fa.get_sbi_tt_buy(datetime(2024, 1, 5, 15, 30))[0] == 83.0


def test_tt_buy_timezone_aware(fa, jan_rates):
    target = pd.Timestamp("2024-01-05", tz="UTC").to_pydatetime()
    assert fa.get_sbi_tt_buy(target)[0] == 83.0


def test_tt_buy_no_rate_within_window_raises(fa, jan_rates):
    with pytest.raises(ValueError):
        fa.get_sbi_tt_buy(datetime(2024, 1, 20))


# ─── Prices ───────────────────────────────────────────────────────────────────


def test_load_prices_keyed_by_symbol_and_year(fa, write, cols):
    path = write(
        "prices.csv",
        cols["prices_header"] + "s ,2025,25.24,2025-02-14,15.00\nS,2026,24.26,2026-09-17,\n",
    )
    fa.load_prices(path)
    assert set(fa._PRICES) == {("S", 2025), ("S", 2026)}
    assert fa._PRICES[("S", 2025)] == {
        "peak_price": 25.24,
        "peak_date": datetime(2025, 2, 14),
        "closing_price": 15.0,
    }
    assert fa._PRICES[("S", 2026)]["closing_price"] is None


def test_load_prices_duplicate_symbol_year_raises(fa, write, cols):
    path = write(
        "prices.csv",
        cols["prices_header"] + "S,2025,25,2025-02-14,15\nS,2025,26,2025-03-14,15\n",
    )
    with pytest.raises(ValueError):
        fa.load_prices(path)


def test_load_prices_missing_columns_raises(fa, write):
    path = write("prices.csv", "symbol,peak_price,peak_date,closing_price\nS,1,2025-01-01,1\n")
    with pytest.raises(ValueError):
        fa.load_prices(path)


def test_get_price_entry_found(fa):
    fa._PRICES[("S", 2026)] = {"peak_price": 1.0}
    assert fa._get_price_entry("S", 2026) == {"peak_price": 1.0}


def test_get_price_entry_other_year_not_used(fa):
    fa._PRICES[("S", 2025)] = {"peak_price": 1.0}
    with pytest.raises(ValueError, match="2026"):
        fa._get_price_entry("S", 2026)


# ─── Sales loading ────────────────────────────────────────────────────────────


def test_load_sales_minimal_columns_sorted(fa, write):
    path = write(
        "sales.csv",
        "symbol,sale_date,units,sale_price\ns,2026-05-01,2,10\nS,2026-04-01,3,11\n",
    )
    df = fa.load_sales(path)
    assert list(df["symbol"]) == ["S", "S"]
    assert list(df["sale_date"].dt.strftime("%Y-%m-%d")) == ["2026-04-01", "2026-05-01"]
    assert list(df["units"]) == [3.0, 2.0]
    assert list(df["benefit_type"]) == ["", ""]
    assert df["acquisition_date"].isna().all()


def test_load_sales_optional_columns(fa, write):
    path = write(
        "sales.csv",
        "symbol,benefit_type,sale_date,units,sale_price,acquisition_date\n"
        "S, so ,2026-04-01,3,11,2024-03-01\n"
        "S,SO,2026-04-02,3,11,\n",
    )
    df = fa.load_sales(path)
    assert list(df["benefit_type"]) == ["SO", "SO"]
    assert df["acquisition_date"].iloc[0] == pd.Timestamp("2024-03-01")
    assert pd.isna(df["acquisition_date"].iloc[1])


def test_load_sales_missing_columns_raises(fa, write):
    path = write("sales.csv", "symbol,sale_date,units\nS,2026-04-01,3\n")
    with pytest.raises(ValueError):
        fa.load_sales(path)


def test_allocate_blank_benefit_type_matches_blank(fa, write, make_lots):
    lots = make_lots([("S", None, 5, "2024-01-01")])
    sales = fa.load_sales(write("sales.csv", "symbol,sale_date,units,sale_price\nS,2024-03-01,5,1\n"))
    alloc = fa.allocate_sales(lots, sales)
    assert [a["units"] for a in alloc[0]] == [5.0]


# ─── process_row (generic) ────────────────────────────────────────────────────


def test_process_row_missing_price_for_year_raises(fa, set_rates, lot_row):
    set_rates()
    fa._PRICES[("S", 2025)] = {
        "peak_price": 1.0,
        "peak_date": datetime(2025, 1, 2),
        "closing_price": 1.0,
    }
    with pytest.raises(ValueError):
        fa.process_row(lot_row(), [])


def test_process_row_audit_has_all_columns(fa, set_rates, write, cols, lot_row):
    set_rates()
    fa.load_prices(write("prices.csv", cols["prices_2026"]))
    _, audit = fa.process_row(lot_row(), [])
    assert [k for k in audit if not k.startswith("_")] == fa.AUDIT_TRAIL_COLS


# ─── audit trail formatting ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value, text",
    [(27.9, "27.90"), (95.5, "95.50"), (12.0, "12.00"), (82.4712, "82.4712"), (1234.5, "1,234.50")],
)
def test_amount_format(fa, value, text):
    assert fa._amount(value) == text


def test_units_format(fa):
    assert fa._units(442.0) == "442"
    assert fa._units(0.5) == "0.5"
    assert fa._units(1500.0) == "1,500"


def test_audit_date_notes_sbi_date_only_when_different(fa):
    assert fa._audit_date(datetime(2026, 7, 15), datetime(2026, 7, 15)) == "2026-07-15"
    assert (
        fa._audit_date(datetime(2026, 7, 15), datetime(2026, 7, 16))
        == "2026-07-15 (SBI date: 2026-07-16)"
    )


def test_audit_calculations(fa):
    assert fa._audit_usd(442, 24.26) == "442 × $24.26 = $10,722.92"
    assert fa._audit_inr(10722.92, 95.5, 1024038.86) == "$10,722.92 × ₹95.50 (TT Buy) = ₹1,024,038.86"


def test_audit_join_adds_total_only_for_multiple_parts(fa):
    assert fa._audit_join(["a"], "$1.00") == "a"
    assert fa._audit_join(["a", "b"], "$2.00") == "a | b → total $2.00"
    assert fa._audit_join([], "$0.00") == ""


# ─── generate (generic) ───────────────────────────────────────────────────────


def test_generate_output_columns(fa, run_generate, input_csv):
    out, audit = run_generate(input_csv({}))
    assert list(out.columns) == fa.SCHEDULE_FA_COLS
    assert list(audit.columns) == fa.AUDIT_TRAIL_COLS


def test_generate_row_error_does_not_stop_other_rows(fa, run_generate, input_csv):
    out, _ = run_generate(input_csv({}, {"symbol": "X"}))
    assert len(out) == 1


def test_generate_missing_input_columns_raises(fa, run_generate):
    with pytest.raises(ValueError):
        run_generate("symbol,units\nS,1\n")


def test_generate_prints_totals(fa, write, set_rates, input_csv, cols, tmp_path):
    set_rates()
    fa.load_prices(write("prices.csv", cols["prices_2026"]))
    buf = io.StringIO()
    with redirect_stdout(buf):
        fa.generate(write("input.csv", input_csv({})), tmp_path / "out.csv")
    printed = buf.getvalue()
    assert "TOTALS across 1 holding(s)" in printed
    assert "UNITS per symbol" in printed


# ─── setup_logging / main ─────────────────────────────────────────────────────


def test_setup_logging_writes_to_logs_dir(fa):
    root = logging.getLogger()
    saved = root.handlers[:]
    try:
        fa.setup_logging()
        fa.log.info("hello")
        assert fa.LOG_FILE.exists()
    finally:
        for handler in root.handlers:
            if handler not in saved:
                handler.close()
        root.handlers[:] = saved
    assert "hello" in fa.LOG_FILE.read_text()


@pytest.fixture
def cli(fa, monkeypatch, set_rates, write, input_csv, cols):
    set_rates()
    monkeypatch.setattr(fa, "setup_logging", lambda: None)
    monkeypatch.setattr(fa, "setup_ratekeeper", lambda skip_update=False: None)
    write("inputs/input.csv", input_csv({}))
    write("inputs/prices.csv", cols["prices_2026"])

    def _run(*args):
        monkeypatch.setattr(sys, "argv", ["main.py", "--year", "2026", *args])
        with redirect_stdout(io.StringIO()):
            fa.main()
        default_output = fa.OUTPUTS_DIR / "output.csv"
        return pd.read_csv(default_output) if default_output.exists() else None

    return _run


def test_main_uses_default_folders(fa, cli):
    out = cli()
    assert (fa.OUTPUTS_DIR / "output_audit_trail.csv").exists()
    assert out[fa.SCHEDULE_FA_COLS[9]].tolist() == [15 * 100 * 10]


def test_main_sets_calendar_year(fa, cli):
    cli()
    assert (fa.CY_START, fa.CY_END) == (datetime(2026, 1, 1), datetime(2026, 12, 31))


def test_main_picks_up_default_sales_file(fa, cli, write, cols):
    write("inputs/sales.csv", cols["sales_header"] + "S,SO,2026-04-01,10,18\n")
    out = cli()
    assert out[fa.SCHEDULE_FA_COLS[9]].tolist() == [0.0]


def test_main_custom_output_name(fa, cli):
    cli("--output", "output_2026.csv")
    assert (fa.OUTPUTS_DIR / "output_2026.csv").exists()


def test_main_missing_sales_file_exits(fa, cli, tmp_path):
    with pytest.raises(SystemExit):
        cli("--sales", str(tmp_path / "nope.csv"))


def test_main_missing_prices_file_exits(fa, cli, tmp_path):
    with pytest.raises(SystemExit):
        cli("--prices", str(tmp_path / "nope.csv"))


def test_main_missing_input_file_exits(fa, cli, tmp_path):
    with pytest.raises(SystemExit):
        cli(str(tmp_path / "nope.csv"))

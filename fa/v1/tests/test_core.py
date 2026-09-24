import io
import logging
import subprocess
import sys
from contextlib import redirect_stdout
from datetime import datetime
from types import SimpleNamespace

import pandas as pd
import pytest


# ─── _benefit_type ────────────────────────────────────────────────────────────


def test_benefit_type_normalizes(fa):
    assert fa._benefit_type(" rsu ") == "RSU"
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


def test_find_rate_csv(fa, monkeypatch, write, tmp_path):
    monkeypatch.setattr(fa, "RATEKEEPER_DIR", tmp_path)
    with pytest.raises(FileNotFoundError):
        fa._find_rate_csv("USD")
    path = write("csv_files/SBI_REFERENCE_RATES_USD.csv", "DATE,TT BUY\n")
    assert fa._find_rate_csv("USD") == path


def test_load_rates_cleans_and_caches(fa, monkeypatch, write, tmp_path):
    monkeypatch.setattr(fa, "RATEKEEPER_DIR", tmp_path)
    path = write(
        "csv_files/SBI_REFERENCE_RATES_USD.csv",
        "date ,tt buy\n2024-01-04,84\n2024-01-02,83.1\n2024-01-01,0\n2024-01-03,abc\n",
    )
    rates = fa._load_rates("usd")
    assert list(rates["TT BUY"]) == [83.1, 84.0]
    path.unlink()
    assert fa._load_rates("USD") is rates


def test_load_rates_missing_columns_raises(fa, monkeypatch, write, tmp_path):
    monkeypatch.setattr(fa, "RATEKEEPER_DIR", tmp_path)
    write("csv_files/SBI_REFERENCE_RATES_USD.csv", "DATE,RATE\n2024-01-02,83\n")
    with pytest.raises(ValueError):
        fa._load_rates("USD")


def test_tt_buy_forward_fills_and_limits_window(fa, set_rates):
    set_rates({"2024-01-05": 83.0, "2024-01-08": 83.2})
    assert fa.get_sbi_tt_buy(datetime(2024, 1, 5)) == (83.0, datetime(2024, 1, 5))
    assert fa.get_sbi_tt_buy(datetime(2024, 1, 6)) == (83.2, datetime(2024, 1, 8))
    with pytest.raises(ValueError):
        fa.get_sbi_tt_buy(datetime(2024, 1, 20))


# ─── yfinance helpers ─────────────────────────────────────────────────────────


def _fake_yf(monkeypatch, fa, frames):
    calls = []

    class Ticker:
        def __init__(self, symbol, session=None):
            self.symbol = symbol

        def history(self, start, end, auto_adjust):
            calls.append((start, end, auto_adjust))
            return frames.pop(0) if frames else pd.DataFrame()

    monkeypatch.setattr(fa, "yf", SimpleNamespace(Ticker=Ticker))
    return calls


def _hist(rows):
    index = pd.DatetimeIndex([pd.Timestamp(d) for d, *_ in rows])
    return pd.DataFrame(
        {"Close": [c for _, c, _ in rows], "High": [h for _, _, h in rows]}, index=index
    )


def test_history_retries_then_raises(fa, monkeypatch):
    calls = _fake_yf(monkeypatch, fa, [])
    with pytest.raises(ValueError, match="No price data"):
        fa._history("S", datetime(2026, 1, 1), datetime(2026, 1, 5))
    assert len(calls) == 3
    assert calls[0] == ("2026-01-01", "2026-01-06", True)


def test_history_returns_first_non_empty(fa, monkeypatch):
    frame = _hist([("2026-01-02", 14.0, 14.5)])
    _fake_yf(monkeypatch, fa, [pd.DataFrame(), frame])
    assert fa._history("S", datetime(2026, 1, 1), datetime(2026, 1, 5)) is frame


def test_closing_price_rolls_forward_to_next_trading_day(fa, monkeypatch):
    _fake_yf(monkeypatch, fa, [_hist([("2025-12-31", 13.0, 13.1), ("2026-01-02", 14.0, 14.5)])])
    assert fa.get_closing_price("S", datetime(2026, 1, 1)) == 14.0


def test_peak_in_period_uses_highest_high(fa, monkeypatch):
    _fake_yf(
        monkeypatch,
        fa,
        [_hist([("2026-02-02", 18.0, 18.5), ("2026-03-02", 19.0, 20.0), ("2026-04-01", 17.0, 17.5)])],
    )
    assert fa.get_peak_in_period("S", datetime(2026, 1, 1), datetime(2026, 12, 31)) == (
        20.0,
        datetime(2026, 3, 2),
    )


# ─── Sales loading ────────────────────────────────────────────────────────────


def test_load_sales_sorted_with_defaults(fa, write):
    path = write(
        "sales.csv",
        "symbol,sale_date,units,sale_price\ns,2026-05-01,2,10\nS,2026-04-01,3,11\n",
    )
    df = fa.load_sales(path)
    assert list(df["sale_date"].dt.strftime("%Y-%m-%d")) == ["2026-04-01", "2026-05-01"]
    assert list(df["benefit_type"]) == ["", ""]
    assert df["acquisition_date"].isna().all()


def test_load_sales_missing_columns_raises(fa, write):
    with pytest.raises(ValueError):
        fa.load_sales(write("sales.csv", "symbol,sale_date,units\nS,2026-04-01,3\n"))


# ─── generate (generic) ───────────────────────────────────────────────────────


def test_generate_output_columns(fa, run_generate, input_csv):
    out, audit = run_generate(input_csv({}))
    assert list(out.columns) == fa.SCHEDULE_FA_COLS
    assert list(audit.columns) == fa.AUDIT_TRAIL_COLS


def test_generate_missing_input_columns_raises(fa, run_generate):
    with pytest.raises(ValueError):
        run_generate("symbol,units\nS,1\n")


def test_generate_row_error_does_not_stop_other_rows(fa, run_generate, input_csv, monkeypatch):
    real = fa.get_peak_in_period

    def peak(symbol, start, end):
        if symbol == "X":
            raise ValueError("No price data")
        return real(symbol, start, end)

    monkeypatch.setattr(fa, "get_peak_in_period", peak)
    out, _ = run_generate(input_csv({}, {"symbol": "X"}))
    assert len(out) == 1


# ─── setup_logging / main ─────────────────────────────────────────────────────


def test_setup_logging_writes_to_logs_dir(fa):
    root = logging.getLogger()
    saved = root.handlers[:]
    try:
        fa.setup_logging()
        fa.log.info("hello")
    finally:
        for handler in root.handlers:
            if handler not in saved:
                handler.close()
        root.handlers[:] = saved
    assert "hello" in fa.LOG_FILE.read_text()


@pytest.fixture
def cli(fa, market, monkeypatch, write, input_csv):
    monkeypatch.setattr(fa, "setup_logging", lambda: None)
    monkeypatch.setattr(fa, "setup_ratekeeper", lambda skip_update=False: None)
    write("inputs/input.csv", input_csv({}))

    def _run(*args):
        monkeypatch.setattr(sys, "argv", ["main.py", "--year", "2026", *args])
        with redirect_stdout(io.StringIO()):
            fa.main()
        default_output = fa.OUTPUTS_DIR / "output.csv"
        return pd.read_csv(default_output) if default_output.exists() else None

    return _run


def test_main_uses_default_folders(fa, cli, cols):
    out = cli()
    assert (fa.OUTPUTS_DIR / "output_audit_trail.csv").exists()
    assert out[cols["closing"]].tolist() == [15 * 100 * 10]
    assert (fa.CY_START, fa.CY_END) == (datetime(2026, 1, 1), datetime(2026, 12, 31))


def test_main_picks_up_default_sales_file(fa, cli, write, cols):
    write("inputs/sales.csv", cols["sales_header"] + "S,SO,2026-04-01,10,18\n")
    assert cli()[cols["closing"]].tolist() == [0.0]


def test_main_custom_output_name(fa, cli):
    cli("--output", "output_2026.csv")
    assert (fa.OUTPUTS_DIR / "output_2026.csv").exists()


def test_main_missing_files_exit(fa, cli, tmp_path):
    with pytest.raises(SystemExit):
        cli("--sales", str(tmp_path / "nope.csv"))
    with pytest.raises(SystemExit):
        cli(str(tmp_path / "nope.csv"))

"""Building blocks: financial year, Rule 115 rates, holding period, quarters, sales CSV, CLI."""

import subprocess
import sys
from datetime import date, datetime

import pytest

# ─── Financial year ───────────────────────────────────────────────────────────


def test_parse_fy(cg):
    assert cg.parse_fy("2026-27") == (datetime(2026, 4, 1), datetime(2027, 3, 31))
    assert cg.parse_fy("1999-00") == (datetime(1999, 4, 1), datetime(2000, 3, 31))


@pytest.mark.parametrize("label", ["2026", "2026-28", "26-27", "2026/27"])
def test_parse_fy_rejects_bad_labels(cg, label):
    with pytest.raises(ValueError, match="2026-27"):
        cg.parse_fy(label)


def test_default_fy_is_last_completed_year(cg):
    assert cg.default_fy(date(2026, 9, 24)) == "2025-26"
    assert cg.default_fy(date(2026, 4, 1)) == "2025-26"
    assert cg.default_fy(date(2026, 3, 31)) == "2024-25"


def test_fy_label(cg):
    assert cg.fy_label() == "2026-27"


# ─── Rule 115 rates ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "event, expected",
    [("2026-04-13", "2026-03-31"), ("2026-03-01", "2026-02-28"), ("2024-03-15", "2024-02-29"), ("2027-01-31", "2026-12-31")],
)
def test_rule115_date_is_last_day_of_previous_month(cg, event, expected):
    assert cg.rule115_date(datetime.fromisoformat(event)) == datetime.fromisoformat(expected)


def test_rule115_rate_on_specified_date(cg, set_rates):
    set_rates()
    rate, specified, used = cg.get_rule115_rate(datetime(2026, 4, 13))
    assert (rate, specified, used) == (92.5, datetime(2026, 3, 31), datetime(2026, 3, 31))


def test_rule115_rate_falls_back_to_latest_earlier_rate(cg, set_rates):
    set_rates()
    rate, specified, used = cg.get_rule115_rate(datetime(2024, 4, 10))
    assert (rate, specified, used) == (83.0, datetime(2024, 3, 31), datetime(2024, 3, 28))


def test_rule115_rate_never_uses_a_later_rate(cg, set_rates):
    set_rates({"2026-04-01": 99.0})
    with pytest.raises(ValueError, match="2026-03-31"):
        cg.get_rule115_rate(datetime(2026, 4, 13))


def test_rule115_rate_too_old_raises(cg, set_rates):
    set_rates({"2026-03-10": 90.0})
    with pytest.raises(ValueError, match="Is the ratekeeper CSV up to date"):
        cg.get_rule115_rate(datetime(2026, 4, 13))


# ─── Ratekeeper ───────────────────────────────────────────────────────────────


def test_ratekeeper_clones_when_missing(cg, monkeypatch):
    calls = []
    monkeypatch.setattr(cg.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    cg.setup_ratekeeper()
    assert calls[0][:2] == ["git", "clone"]


def test_ratekeeper_skip_update_does_nothing(cg, monkeypatch, tmp_path):
    monkeypatch.setattr(cg, "RATEKEEPER_DIR", tmp_path)
    calls = []
    monkeypatch.setattr(cg.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    cg.setup_ratekeeper(skip_update=True)
    assert calls == []


def test_ratekeeper_pull_failure_uses_local_data(cg, monkeypatch, tmp_path):
    monkeypatch.setattr(cg, "RATEKEEPER_DIR", tmp_path)
    (tmp_path / "csv_files").mkdir()

    def _fail(cmd, **kw):
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(cg.subprocess, "run", _fail)
    cg.setup_ratekeeper()


def test_load_rates_cleans_and_caches(cg, monkeypatch, write, tmp_path):
    monkeypatch.setattr(cg, "RATEKEEPER_DIR", tmp_path)
    write(
        "csv_files/SBI_REFERENCE_RATES_USD.csv",
        "date ,tt buy\n2026-03-31,92.5\n2026-03-30,0\n2026-03-27,abc\n",
    )
    rates = cg._load_rates("USD")
    assert rates["TT BUY"].tolist() == [92.5]
    assert cg._load_rates("usd") is rates


def test_repo_root_holds_shared_ratekeeper(cg):
    assert cg.REPO_ROOT == cg.BASE_DIR.parent


# ─── Holding period and quarters ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "acquired, sold, long_term",
    [
        ("2024-04-10", "2026-04-10", False),
        ("2024-04-10", "2026-04-11", True),
        ("2026-04-13", "2026-04-13", False),
        ("2024-02-29", "2026-03-01", True),
    ],
)
def test_is_long_term_after_24_months(cg, acquired, sold, long_term):
    assert cg.is_long_term(datetime.fromisoformat(acquired), datetime.fromisoformat(sold)) is long_term


@pytest.mark.parametrize(
    "day, quarter",
    [
        ("2026-04-01", 0),
        ("2026-06-15", 0),
        ("2026-06-16", 1),
        ("2026-09-15", 1),
        ("2026-09-16", 2),
        ("2026-12-15", 2),
        ("2026-12-16", 3),
        ("2027-01-10", 3),
        ("2027-03-15", 3),
        ("2027-03-16", 4),
        ("2027-03-31", 4),
    ],
)
def test_quarter_index(cg, day, quarter):
    assert cg.quarter_index(datetime.fromisoformat(day)) == quarter


# ─── Sales CSV ────────────────────────────────────────────────────────────────


def test_load_sales_parses_and_sorts(cg, write, header, line):
    text = header + line(sold="2026-05-01") + line(sold="2026-04-13")
    sales = cg.load_sales(write("sales.csv", text))
    assert sales["sale_date"].dt.strftime("%Y-%m-%d").tolist() == ["2026-04-13", "2026-05-01"]
    assert sales["benefit_type"].tolist() == ["SO", "SO"]


def test_load_sales_fees_and_benefit_type_optional(cg, write):
    text = (
        "symbol,acquisition_date,sale_date,units,proceeds_usd,cost_basis_usd\n"
        "s,2026-04-13,2026-04-13,1,10,10\n"
    )
    sales = cg.load_sales(write("sales.csv", text))
    assert sales.loc[0, "symbol"] == "S"
    assert sales.loc[0, "fees_usd"] == 0.0
    assert sales.loc[0, "benefit_type"] == ""


def test_load_sales_blank_fees_are_zero(cg, write, header):
    text = header + "S,SO,2026-04-13,2026-04-13,1,10,10,\n"
    assert cg.load_sales(write("sales.csv", text)).loc[0, "fees_usd"] == 0.0


def test_load_sales_missing_columns_raises(cg, write):
    text = "symbol,sale_date,units,proceeds_usd\nS,2026-04-13,1,10\n"
    with pytest.raises(ValueError, match="missing required columns"):
        cg.load_sales(write("sales.csv", text))


@pytest.mark.parametrize(
    "row, message",
    [
        ("S,SO,2026-05-01,2026-04-13,1,10,10,0\n", "acquisition_date is after sale_date"),
        ("S,SO,2026-04-13,2026-04-13,1,10,,0\n", "cost_basis_usd is blank"),
        ("S,SO,2026-04-13,2026-04-13,1,,10,0\n", "proceeds_usd is blank"),
        ("S,SO,,2026-04-13,1,10,10,0\n", "acquisition_date and sale_date are required"),
        ("S,SO,2026-04-13,2026-04-13,0,10,10,0\n", "units must be positive"),
    ],
)
def test_load_sales_invalid_rows_raise(cg, write, header, row, message):
    with pytest.raises(ValueError, match=message):
        cg.load_sales(write("sales.csv", header + row))


# ─── Formatting ───────────────────────────────────────────────────────────────


def test_inr_formats_negative_with_minus_sign(cg):
    assert cg._inr(1234.5) == "₹1,234.50"
    assert cg._inr(-462.5) == "−₹462.50"


def test_audit_date_shows_rate_date_and_missing_specified_date(cg):
    d = datetime
    assert cg._audit_date(d(2026, 4, 13), d(2026, 3, 31), d(2026, 3, 31)) == (
        "2026-04-13 (SBI date: 2026-03-31)"
    )
    assert cg._audit_date(d(2024, 4, 10), d(2024, 3, 31), d(2024, 3, 28)) == (
        "2024-04-10 (SBI date: 2024-03-28, no rate on 2024-03-31)"
    )


# ─── CLI ──────────────────────────────────────────────────────────────────────


def _run_cli(cg, monkeypatch, *argv):
    monkeypatch.setattr(cg, "setup_ratekeeper", lambda skip_update=False: None)
    monkeypatch.setattr(sys, "argv", ["main.py", *argv])
    cg.main()


def test_cli_writes_outputs_for_fy(cg, monkeypatch, set_rates, write, header, line):
    set_rates()
    path = write("sales.csv", header + line())
    _run_cli(cg, monkeypatch, str(path), "--fy", "2026-27")
    out = cg.OUTPUTS_DIR
    assert sorted(p.name for p in out.iterdir()) == ["cg_2026-27.txt", "cg_2026-27_audit_trail.csv"]
    assert "TOTAL,Schedule CG A5" in (out / "cg_2026-27_audit_trail.csv").read_text(encoding="utf-8-sig")
    output = (out / "cg_2026-27.txt").read_text(encoding="utf-8")
    assert output.startswith("INFO Financial Year: 2026-04-01 → 2027-03-31\n")
    assert "INFO A5. Short-term" in output


def test_cli_output_option_names_both_files(cg, monkeypatch, set_rates, write, header, line):
    set_rates()
    path = write("sales.csv", header + line())
    _run_cli(cg, monkeypatch, str(path), "--fy", "2026-27", "--output", "cg_test.txt")
    assert sorted(p.name for p in cg.OUTPUTS_DIR.iterdir()) == ["cg_test.txt", "cg_test_audit_trail.csv"]


def test_cli_validation_failure_exits(cg, monkeypatch, set_rates, write, header, line):
    set_rates()
    monkeypatch.setattr(cg, "validate", lambda schedule, quarterly: [("broken rule", False)])
    path = write("sales.csv", header + line())
    with pytest.raises(SystemExit):
        _run_cli(cg, monkeypatch, str(path), "--fy", "2026-27")
    assert (cg.OUTPUTS_DIR / "cg_2026-27_audit_trail.csv").exists()
    assert "ERROR ✗ broken rule" in (cg.OUTPUTS_DIR / "cg_2026-27.txt").read_text(encoding="utf-8")


def test_cli_bad_fy_exits(cg, monkeypatch, write, header, line):
    path = write("sales.csv", header + line())
    with pytest.raises(SystemExit):
        _run_cli(cg, monkeypatch, str(path), "--fy", "2026")


def test_cli_missing_sales_file_exits(cg, monkeypatch, tmp_path):
    with pytest.raises(SystemExit):
        _run_cli(cg, monkeypatch, str(tmp_path / "nope.csv"), "--fy", "2026-27")


def test_cli_invalid_sales_exits(cg, monkeypatch, set_rates, write, header):
    set_rates()
    path = write("sales.csv", header + "S,SO,2026-04-13,2026-04-13,1,10,,0\n")
    with pytest.raises(SystemExit):
        _run_cli(cg, monkeypatch, str(path), "--fy", "2026-27")

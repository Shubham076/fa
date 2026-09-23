"""Dividend scenarios: converted at the SBI TT Buy rate on Dec 31 of the reporting year."""

import pytest


@pytest.fixture
def priced(fa, set_rates, write, cols):
    set_rates()
    fa.load_prices(write("prices.csv", cols["prices_2026"]))


def test_dividends_converted_at_year_end_rate(fa, priced, lot_row, cols):
    fa_row, audit = fa.process_row(lot_row(dividends=5), [])
    assert fa_row[cols["dividends"]] == 5 * 100
    assert audit["dividends_usd"] == 5.0
    assert audit["dividends_inr"] == 500.0


def test_blank_dividends_treated_as_zero(fa, priced, lot_row, cols):
    fa_row, audit = fa.process_row(lot_row(dividends=float("nan")), [])
    assert fa_row[cols["dividends"]] == 0.0
    assert audit["dividends_usd"] == 0.0


def test_zero_dividends_need_no_year_end_rate_when_sold(fa, priced, lot_row, sale, cols):
    fa._RATE_CACHE["USD"] = fa._RATE_CACHE["USD"].iloc[:-1]
    fa_row, _ = fa.process_row(lot_row(dividends=0), [sale("2026-04-01", 10, 18)])
    assert fa_row[cols["dividends"]] == 0.0


def test_dividends_on_sold_lot_need_year_end_rate(fa, priced, lot_row, sale):
    fa._RATE_CACHE["USD"] = fa._RATE_CACHE["USD"].iloc[:-1]
    with pytest.raises(ValueError, match="No SBI TT Buy rate"):
        fa.process_row(lot_row(dividends=5), [sale("2026-04-01", 10, 18)])


def test_generate_dividends_per_lot(fa, run_generate, input_csv, cols):
    out, _ = run_generate(input_csv({"dividends": 5}, {"dividends": 2.5, "units": 4}))
    assert out[cols["dividends"]].tolist() == [500.0, 250.0]


def test_generate_without_dividends_column(fa, run_generate, cols):
    text = (
        "symbol,units,acquisition_date,acquisition_price,company_name,address,zip_code\n"
        "S,10,2025-06-02,12,Co,Addr,94041\n"
    )
    out, audit = run_generate(text)
    assert out[cols["dividends"]].tolist() == [0.0]
    assert audit["benefit_type"].isna().all()

"""RSU (benefit_type=RSU) scenarios: held lots, closing balance, year-specific prices."""

import pytest


@pytest.fixture
def priced(fa, set_rates, write, cols):
    set_rates()
    fa.load_prices(write("prices.csv", cols["prices_2026"]))


def rsu(lot_row, **kw):
    return lot_row(benefit_type="RSU", **kw)


def test_held_all_year_carry_forward(fa, priced, lot_row, cols):
    fa_row, audit = fa.process_row(rsu(lot_row), [])
    assert fa_row[cols["initial"]] == 12 * 80 * 10
    assert fa_row[cols["peak"]] == 20 * 90 * 10
    assert fa_row[cols["closing"]] == 15 * 100 * 10
    assert fa_row[cols["proceeds"]] == 0.0
    assert fa_row["Date of acquiring the interest"] == "2025-06-02"
    assert audit["initial_source"] == "Acquisition price (carry-forward)"
    assert audit["benefit_type"] == "RSU"
    assert (audit["units_at_start"], audit["units_sold"], audit["units_at_end"]) == (10, 0, 10)


def test_vested_during_year(fa, priced, lot_row):
    _, audit = fa.process_row(rsu(lot_row, acq_date="2026-04-01"), [])
    assert audit["initial_source"] == "Acquisition price"
    assert audit["initial_inr"] == 12 * 85 * 10


def test_vested_after_year_is_skipped(fa, priced, lot_row):
    assert fa.process_row(rsu(lot_row, acq_date="2027-01-05"), []) is None


def test_blank_closing_price_with_held_units_raises(fa, priced, lot_row):
    fa._PRICES[("S", 2026)]["closing_price"] = None
    with pytest.raises(ValueError, match="closing_price"):
        fa.process_row(rsu(lot_row), [])


def test_option_sales_never_consume_rsu_lots(fa, write, make_lots, cols):
    lots = make_lots([("S", "RSU", 10, "2024-01-01"), ("S", "SO", 10, "2024-02-01")])
    sales = fa.load_sales(write("sales.csv", cols["sales_header"] + "S,SO,2024-03-01,10,5\n"))
    alloc = fa.allocate_sales(lots, sales)
    assert alloc[0] == []
    assert [a["units"] for a in alloc[1]] == [10.0]


def test_generate_rsu_held_while_options_sold(fa, run_generate, input_csv, cols):
    out, audit = run_generate(
        input_csv({"benefit_type": "SO"}, {"benefit_type": "RSU", "units": 4}),
        sales_text=cols["sales_header"] + "S,SO,2026-04-01,10,18\n",
    )
    assert audit["benefit_type"].tolist() == ["SO", "RSU"]
    assert out[cols["closing"]].tolist() == [0.0, 15 * 100 * 4]
    assert out[cols["proceeds"]].tolist() == [10 * 18 * 85, 0.0]


def test_generate_partial_rsu_sale(fa, run_generate, input_csv, cols):
    out, _ = run_generate(
        input_csv({"benefit_type": "RSU", "units": 4}),
        sales_text=cols["sales_header"] + "S,RSU,2026-05-04,1,19\n",
    )
    assert out[cols["closing"]].tolist() == [15 * 100 * 3]
    assert out[cols["proceeds"]].tolist() == [19 * 88]


def test_generate_without_sales_everything_held(fa, run_generate, input_csv, cols):
    out, _ = run_generate(input_csv({"benefit_type": "RSU"}, {"benefit_type": "RSU", "units": 4}))
    assert out[cols["closing"]].tolist() == [15 * 100 * 10, 15 * 100 * 4]


def test_prices_for_reporting_year_are_used(fa, set_cy, set_rates, write, lot_row, cols):
    set_cy(2025)
    set_rates({"2025-06-02": 80.0, "2025-10-01": 82.0, "2025-12-31": 90.0, "2026-03-02": 95.0})
    fa.load_prices(
        write(
            "prices.csv",
            cols["prices_header"] + "S,2025,25,2025-10-01,16\nS,2026,20,2026-03-02,15\n",
        )
    )
    fa_row, audit = fa.process_row(rsu(lot_row), [])
    assert audit["peak_date"] == "2025-10-01"
    assert fa_row[cols["peak"]] == 25 * 82 * 10
    assert fa_row[cols["closing"]] == 16 * 90 * 10

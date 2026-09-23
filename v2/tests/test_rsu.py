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
    assert audit["Type"] == "RSU"
    assert audit["Units"] == "10 (carry forward)"
    assert audit["Initial Value (USD)"] == "10 × $12.00 = $120.00"
    assert audit["Initial Value (INR)"] == "$120.00 × ₹80.00 (TT Buy) = ₹9,600.00"
    assert audit["Closing Date"] == "2026-12-31"
    assert audit["Closing Value (USD)"] == "10 × $15.00 = $150.00"
    assert audit["Closing Value (INR)"] == "$150.00 × ₹100.00 (TT Buy) = ₹15,000.00"
    assert audit["Sale Dates"] == ""


def test_vested_during_year(fa, priced, lot_row):
    _, audit = fa.process_row(rsu(lot_row, acq_date="2026-04-01"), [])
    assert audit["Units"] == "10"
    assert audit["Initial Date"] == "2026-04-01"
    assert audit["Initial Value (INR)"] == "$120.00 × ₹85.00 (TT Buy) = ₹10,200.00"


def test_vested_on_day_without_sbi_rate_notes_rate_date(fa, priced, lot_row):
    _, audit = fa.process_row(rsu(lot_row, acq_date="2026-03-01"), [])
    assert audit["Initial Date"] == "2026-03-01 (SBI date: 2026-03-02)"
    assert audit["Initial Value (INR)"] == "$120.00 × ₹90.00 (TT Buy) = ₹10,800.00"


def test_peak_on_day_without_sbi_rate_notes_rate_date(fa, set_rates, write, lot_row, cols):
    set_rates()
    fa.load_prices(write("prices.csv", cols["prices_header"] + "S,2026,20,2026-03-01,15\n"))
    _, audit = fa.process_row(rsu(lot_row), [])
    assert audit["Peak Date"] == "2026-03-01 (SBI date: 2026-03-02)"
    assert audit["Peak Value (INR)"] == "$200.00 × ₹90.00 (TT Buy) = ₹18,000.00"


def test_closing_without_dec31_sbi_rate_notes_rate_date(fa, set_rates, write, lot_row, cols):
    set_rates({"2025-06-02": 80.0, "2026-03-02": 90.0, "2027-01-04": 101.0})
    fa.load_prices(write("prices.csv", cols["prices_2026"]))
    _, audit = fa.process_row(rsu(lot_row), [])
    assert audit["Closing Date"] == "2026-12-31 (SBI date: 2027-01-04)"
    assert audit["Closing Value (INR)"] == "$150.00 × ₹101.00 (TT Buy) = ₹15,150.00"


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
    assert audit["Type"].tolist() == ["SO", "RSU"]
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
    assert audit["Peak Date"] == "2025-10-01"
    assert audit["Peak Value (INR)"] == "$250.00 × ₹82.00 (TT Buy) = ₹20,500.00"
    assert fa_row[cols["peak"]] == 25 * 82 * 10
    assert fa_row[cols["closing"]] == 16 * 90 * 10

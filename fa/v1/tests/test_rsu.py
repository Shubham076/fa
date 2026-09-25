"""RSU (benefit_type=RSU) scenarios: held lots and closing balance from yfinance."""

import pytest

SALES_HEADER_WITH_LOT = "symbol,benefit_type,sale_date,units,sale_price,acquisition_date\n"


def rsu(lot_row, **kw):
    return lot_row(benefit_type="RSU", **kw)


def test_held_all_year_carry_forward(fa, market, lot_row, cols):
    fa_row, audit = fa.process_row(rsu(lot_row), [])
    assert fa_row[cols["initial"]] == 14 * 83 * 10
    assert fa_row[cols["peak"]] == 20 * 90 * 10
    assert fa_row[cols["closing"]] == 15 * 100 * 10
    assert fa_row[cols["proceeds"]] == 0.0
    assert audit["Type"] == "RSU"
    assert audit["Units"] == "10 (carry forward)"
    assert audit["Initial Date"] == "2026-01-01 (SBI date: 2026-01-02)"
    assert audit["Initial Value (USD)"] == "10 × $14.00 = $140.00"
    assert audit["Initial Value (INR)"] == "$140.00 × ₹83.00 (TT Buy) = ₹11,620.00"
    assert audit["Peak Value (INR)"] == "$200.00 × ₹90.00 (TT Buy) = ₹18,000.00"
    assert audit["Closing Date"] == "2026-12-31"
    assert audit["Closing Value (INR)"] == "$150.00 × ₹100.00 (TT Buy) = ₹15,000.00"


def test_vested_during_year_uses_acquisition_price(fa, market, lot_row):
    _, audit = fa.process_row(rsu(lot_row, acq_date="2026-04-01"), [])
    assert audit["Units"] == "10"
    assert audit["Initial Date"] == "2026-04-01"
    assert audit["Initial Value (INR)"] == "$120.00 × ₹85.00 (TT Buy) = ₹10,200.00"


def test_vested_after_year_is_skipped(fa, market, lot_row):
    assert fa.process_row(rsu(lot_row, acq_date="2027-01-05"), []) is None


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
        sales_text=SALES_HEADER_WITH_LOT + "S,RSU,2026-05-04,1,19,2025-06-02\n",
    )
    assert out[cols["closing"]].tolist() == [15 * 100 * 3]
    assert out[cols["proceeds"]].tolist() == [19 * 88]


def test_rsu_sale_without_acquisition_date_raises(fa, write, cols):
    path = write("sales.csv", cols["sales_header"] + "S,RSU,2026-05-04,1,19\n")
    with pytest.raises(ValueError, match="RSU sales need acquisition_date.*S 1 on 2026-05-04"):
        fa.load_sales(path)


def test_rsu_sale_with_blank_acquisition_date_raises(fa, write):
    path = write("sales.csv", SALES_HEADER_WITH_LOT + "S,RSU,2026-05-04,1,19,\n")
    with pytest.raises(ValueError, match="RSU sales need acquisition_date"):
        fa.load_sales(path)


def test_option_sale_without_acquisition_date_is_fine(fa, write):
    sales = fa.load_sales(write("sales.csv", SALES_HEADER_WITH_LOT + "S,SO,2026-05-04,1,19,\n"))
    assert sales["acquisition_date"].isna().all()


def test_rsu_sale_takes_only_the_named_lot(fa, write, make_lots):
    lots = make_lots([("S", "RSU", 10, "2024-01-01"), ("S", "RSU", 10, "2024-06-01")])
    sales = fa.load_sales(
        write("sales.csv", SALES_HEADER_WITH_LOT + "S,RSU,2024-09-01,4,5,2024-06-01\n")
    )
    alloc = fa.allocate_sales(lots, sales)
    assert alloc[0] == []
    assert [a["units"] for a in alloc[1]] == [4.0]


def test_generate_rsu_sale_without_acquisition_date_exits(fa, run_generate, input_csv, cols):
    with pytest.raises(SystemExit):
        run_generate(
            input_csv({"benefit_type": "RSU", "units": 4}),
            sales_text=cols["sales_header"] + "S,RSU,2026-05-04,1,19\n",
        )

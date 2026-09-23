"""Stock option (benefit_type=SO) scenarios: FIFO sale matching, sold/partial/held lots."""

import pytest


# ─── Sale → lot matching ──────────────────────────────────────────────────────


def test_fifo_by_acquisition_date_within_benefit_type(fa, write, make_lots, cols):
    lots = make_lots(
        [
            ("S", "SO", 10, "2024-02-01"),
            ("S", "SO", 10, "2024-01-01"),
            ("S", "RSU", 5, "2023-12-01"),
        ]
    )
    sales = fa.load_sales(write("sales.csv", cols["sales_header"] + "S,SO,2024-03-01,12,5\n"))
    alloc = fa.allocate_sales(lots, sales)
    assert [a["units"] for a in alloc[1]] == [10.0]
    assert [a["units"] for a in alloc[0]] == [2.0]
    assert alloc[2] == []
    assert alloc[0][0]["sale_price"] == 5.0


def test_acquisition_date_targets_specific_lot(fa, write, make_lots):
    lots = make_lots([("S", "SO", 10, "2024-01-01"), ("S", "SO", 10, "2024-02-01")])
    sales = fa.load_sales(
        write(
            "sales.csv",
            "symbol,benefit_type,sale_date,units,sale_price,acquisition_date\n"
            "S,SO,2024-03-01,3,5,2024-02-01\n",
        )
    )
    alloc = fa.allocate_sales(lots, sales)
    assert alloc[0] == []
    assert [a["units"] for a in alloc[1]] == [3.0]


def test_lot_vested_after_sale_date_not_used(fa, write, make_lots, cols):
    lots = make_lots([("S", "SO", 10, "2024-05-01")])
    sales = fa.load_sales(write("sales.csv", cols["sales_header"] + "S,SO,2024-03-01,1,5\n"))
    with pytest.raises(ValueError):
        fa.allocate_sales(lots, sales)


def test_selling_more_than_vested_raises(fa, write, make_lots, cols):
    lots = make_lots([("S", "SO", 10, "2024-01-01")])
    sales = fa.load_sales(write("sales.csv", cols["sales_header"] + "S,SO,2024-03-01,11,5\n"))
    with pytest.raises(ValueError):
        fa.allocate_sales(lots, sales)


def test_same_day_sales_keep_file_order(fa, write, make_lots, cols):
    lots = make_lots([("S", "SO", 5, "2024-01-01"), ("S", "SO", 5, "2024-02-01")])
    sales = fa.load_sales(
        write("sales.csv", cols["sales_header"] + "S,SO,2024-03-01,5,1\nS,SO,2024-03-01,5,2\n")
    )
    alloc = fa.allocate_sales(lots, sales)
    assert alloc[0][0]["sale_price"] == 1.0
    assert alloc[1][0]["sale_price"] == 2.0


def test_sales_spread_across_lots_in_date_order(fa, write, make_lots, cols):
    lots = make_lots([("S", "SO", 4, "2024-01-01"), ("S", "SO", 4, "2024-02-01")])
    sales = fa.load_sales(
        write("sales.csv", cols["sales_header"] + "S,SO,2024-04-01,3,2\nS,SO,2024-03-01,3,1\n")
    )
    alloc = fa.allocate_sales(lots, sales)
    assert [(a["units"], a["sale_price"]) for a in alloc[0]] == [(3.0, 1.0), (1.0, 2.0)]
    assert [(a["units"], a["sale_price"]) for a in alloc[1]] == [(2.0, 2.0)]


# ─── Per-lot Schedule FA values ───────────────────────────────────────────────


@pytest.fixture
def priced(fa, set_rates, write, cols):
    set_rates()
    fa.load_prices(write("prices.csv", cols["prices_2026"]))


def test_fully_sold_during_year(fa, priced, lot_row, sale, cols):
    sales = [sale("2026-04-01", 4, 18), sale("2026-05-04", 6, 19)]
    fa_row, audit = fa.process_row(lot_row(), sales)
    assert fa_row[cols["closing"]] == 0.0
    assert fa_row[cols["proceeds"]] == 4 * 18 * 85 + 6 * 19 * 88
    assert fa_row[cols["peak"]] == 20 * 90 * 10
    assert fa_row[cols["initial"]] == 12 * 80 * 10
    assert audit["proceeds_usd"] == 186.0
    assert (audit["units_at_start"], audit["units_sold"], audit["units_at_end"]) == (10, 10, 0)
    assert "2026-04-01" in audit["sales"] and "2026-05-04" in audit["sales"]


def test_partially_sold_during_year(fa, priced, lot_row, sale, cols):
    fa_row, _ = fa.process_row(lot_row(), [sale("2026-04-01", 8, 18)])
    assert fa_row[cols["closing"]] == 15 * 100 * 2
    assert fa_row[cols["proceeds"]] == 8 * 18 * 85


def test_partially_sold_before_year_uses_remaining_units(fa, priced, lot_row, sale, cols):
    fa_row, audit = fa.process_row(lot_row(), [sale("2025-10-01", 3, 18)])
    assert audit["units_at_start"] == 7
    assert fa_row[cols["initial"]] == 12 * 80 * 7
    assert fa_row[cols["peak"]] == 20 * 90 * 7
    assert fa_row[cols["closing"]] == 15 * 100 * 7
    assert fa_row[cols["proceeds"]] == 0.0


def test_fully_sold_before_year_is_skipped(fa, priced, lot_row, sale):
    assert fa.process_row(lot_row(), [sale("2025-10-01", 10, 18)]) is None


def test_vested_after_year_is_skipped(fa, priced, lot_row):
    assert fa.process_row(lot_row(acq_date="2027-01-05"), []) is None


def test_sales_after_year_are_ignored(fa, priced, lot_row, sale, cols):
    fa_row, audit = fa.process_row(lot_row(), [sale("2027-02-01", 10, 18)])
    assert audit["units_at_end"] == 10
    assert fa_row[cols["closing"]] == 15 * 100 * 10


def test_blank_closing_price_ok_when_fully_sold(fa, priced, lot_row, sale, cols):
    fa._PRICES[("S", 2026)]["closing_price"] = None
    fa_row, _ = fa.process_row(lot_row(), [sale("2026-04-01", 10, 18)])
    assert fa_row[cols["closing"]] == 0.0


# ─── End to end ───────────────────────────────────────────────────────────────


def test_generate_all_options_exercised_and_sold(fa, run_generate, input_csv, cols):
    out, audit = run_generate(
        input_csv(
            {"units": 10, "acq_date": "2025-06-02"},
            {"units": 5, "acq_date": "2026-03-02"},
        ),
        sales_text=cols["sales_header"] + "S,SO,2026-04-01,12,18\nS,SO,2026-05-04,3,19\n",
    )
    assert out[cols["closing"]].tolist() == [0.0, 0.0]
    assert out[cols["proceeds"]].tolist() == [10 * 18 * 85, 2 * 18 * 85 + 3 * 19 * 88]
    assert out[cols["initial"]].tolist() == [12 * 80 * 10, 12 * 90 * 5]
    assert audit["initial_source"].tolist() == [
        "Acquisition price (carry-forward)",
        "Acquisition price",
    ]
    assert audit["units_sold"].sum() == 15
    assert audit["units_at_end"].sum() == 0


def test_generate_oversell_exits(fa, run_generate, input_csv, cols):
    with pytest.raises(SystemExit):
        run_generate(input_csv({}), sales_text=cols["sales_header"] + "S,SO,2026-04-01,11,18\n")

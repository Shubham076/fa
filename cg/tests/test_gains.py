"""Capital gain scenarios for FY 2026-27 (rates in conftest.RATES)."""

import pytest

# Options exercised and sold the same day: cost = sale value, only the fee is a loss.
SAME_DAY = dict(acq="2026-04-13", sold="2026-04-13", units=416, price=12.71, cost=12.71, fees=5)
# RSU vested 2025-04-15 (Rule 115 rate 85.0), sold 2026-04-20 (rate 92.5): short-term.
RSU = dict(acq="2025-04-15", sold="2026-04-20", units=10, price=20, cost=15, benefit_type="RSU")
# Acquired 2024-04-10 (no rate on Sun 2024-03-31 → 2024-03-28 at 83.0), sold 2026-09-20 (95.0).
LONG = dict(acq="2024-04-10", sold="2026-09-20", units=10, price=30, cost=10, benefit_type="RSU")


@pytest.fixture
def rated(set_rates):
    set_rates()


# ─── Single sale ──────────────────────────────────────────────────────────────


def test_exercise_and_sell_same_day_gain_is_minus_fees(cg, rated, sale_row):
    r = cg.process_sale(sale_row(**SAME_DAY))
    assert r["term"] == "Short-term"
    assert r["full_inr"] == 489_080.80
    assert r["cost_inr"] == 489_080.80
    assert r["fees_inr"] == 462.50
    assert r["gain_inr"] == -462.50
    assert r["quarter"] == 0


def test_exercise_and_sell_audit_row(cg, rated, sale_row):
    audit = cg.process_sale(sale_row(**SAME_DAY))["audit"]
    assert list(audit) == cg.AUDIT_TRAIL_COLS
    assert audit["Units"] == "416"
    assert audit["Term"] == "Short-term (0 days)"
    assert audit["Acquisition Date"] == "2026-04-13 (SBI date: 2026-03-31)"
    assert audit["Sale Date"] == "2026-04-13 (SBI date: 2026-03-31)"
    assert audit["Sale Value (USD)"] == "$5,287.36 (416 @ $12.7100)"
    assert audit["Sale Value (INR)"] == "$5,287.36 × ₹92.50 (TT Buy) = ₹489,080.80"
    assert audit["Cost (USD)"] == "$5,287.36 (416 @ $12.7100)"
    assert audit["Cost (INR)"] == "$5,287.36 × ₹92.50 (TT Buy) = ₹489,080.80"
    assert audit["Fees (USD)"] == "$5.00"
    assert audit["Fees (INR)"] == "$5.00 × ₹92.50 (TT Buy) = ₹462.50"
    assert audit["Gain (INR)"] == "₹489,080.80 − ₹489,080.80 − ₹462.50 = −₹462.50"
    assert audit["Quarter"] == "Upto 15/6"


def test_rsu_cost_uses_rate_for_month_before_vest(cg, rated, sale_row):
    r = cg.process_sale(sale_row(**RSU))
    assert r["term"] == "Short-term"
    assert r["full_inr"] == 10 * 20 * 92.5
    assert r["cost_inr"] == 10 * 15 * 85.0
    assert r["gain_inr"] == 5_750.00
    assert r["audit"]["Acquisition Date"] == "2025-04-15 (SBI date: 2025-03-31)"
    assert r["audit"]["Fees (USD)"] == "" and r["audit"]["Fees (INR)"] == ""


def test_long_term_sale_with_rate_fallback(cg, rated, sale_row):
    r = cg.process_sale(sale_row(**LONG))
    assert r["term"] == "Long-term"
    assert r["full_inr"] == 28_500.00
    assert r["cost_inr"] == 8_300.00
    assert r["gain_inr"] == 20_200.00
    assert r["quarter"] == 2
    assert r["audit"]["Term"] == "Long-term (893 days)"
    assert r["audit"]["Acquisition Date"] == (
        "2024-04-10 (SBI date: 2024-03-28, no rate on 2024-03-31)"
    )
    assert r["audit"]["Sale Date"] == "2026-09-20 (SBI date: 2026-08-31)"


def test_broker_totals_used_as_is(cg, rated, write):
    text = (
        "symbol,benefit_type,acquisition_date,sale_date,units,proceeds_usd,cost_basis_usd\n"
        "S,SO,2026-04-15,2026-04-15,250,3280.16,3280.23\n"
    )
    r = cg.process_sale(cg.load_sales(write("broker.csv", text)).iloc[0])
    assert r["full_inr"] == round(3280.16 * 92.5, 2)
    assert r["cost_inr"] == round(3280.23 * 92.5, 2)
    assert r["gain_inr"] == round(r["full_inr"] - r["cost_inr"], 2)
    assert r["audit"]["Sale Value (USD)"] == "$3,280.16 (250 @ $13.1206)"


def test_missing_rate_raises(cg, set_rates, sale_row):
    set_rates({"2026-03-31": 92.5})
    with pytest.raises(ValueError, match="2025-03-31"):
        cg.process_sale(sale_row(**RSU))


# ─── generate ─────────────────────────────────────────────────────────────────


def test_generate_items_in_whole_rupees(cg, run_generate, sales_csv):
    run = run_generate(sales_csv(SAME_DAY, RSU, LONG))
    # 507,580.80 / 501,830.80 / 462.50 rounded half-up to rupees.
    assert run.schedule[cg.SHORT_TERM] == {
        "full_value": 507_581,
        "cost_of_acquisition": 501_831,
        "cost_of_improvement": 0,
        "transfer_expenses": 463,
        "total_deductions": 502_294,
        "capital_gain": 5_287,
    }
    assert run.schedule[cg.LONG_TERM] == {
        "full_value": 28_500,
        "cost_of_acquisition": 8_300,
        "cost_of_improvement": 0,
        "transfer_expenses": 0,
        "total_deductions": 8_300,
        "capital_gain": 20_200,
    }


def test_generate_total_rows_at_end_of_audit_trail(cg, run_generate, sales_csv):
    run = run_generate(sales_csv(SAME_DAY, RSU, LONG))
    assert run.rows["Symbol"].tolist() == ["S", "S", "S", "", "TOTAL", "TOTAL"]
    st, lt = run.totals.to_dict("records")
    assert st == {
        "Symbol": "TOTAL",
        "Type": "Schedule CG A5",
        "Units": "426",
        "Term": "Short-term",
        "Acquisition Date": "",
        "Sale Date": "",
        "Sale Value (USD)": "$5,487.36",
        "Sale Value (INR)": "₹5,07,581",
        "Cost (USD)": "$5,437.36",
        "Cost (INR)": "₹5,01,831",
        "Fees (USD)": "$5.00",
        "Fees (INR)": "₹463",
        "Gain (INR)": "₹5,07,581 − ₹5,01,831 − ₹463 = ₹5,287",
        "Quarter": "Table F row 3: Upto 15/6 ₹5,287 | 16/6 to 15/9 ₹0 | 16/9 to 15/12 ₹0 | "
        "16/12 to 15/3 ₹0 | 16/3 to 31/3 ₹0",
    }
    assert lt["Type"] == "Schedule CG B8"
    assert lt["Gain (INR)"] == "₹28,500 − ₹8,300 − ₹0 = ₹20,200"
    assert lt["Quarter"].startswith("Table F row 5: Upto 15/6 ₹0 | 16/6 to 15/9 ₹0 | 16/9 to 15/12 ₹20,200")


def test_generate_logs_form_rows(cg, run_generate, sales_csv):
    lines = run_generate(sales_csv(SAME_DAY, RSU, LONG)).output.splitlines()
    start = lines.index("INFO A5. Short-term: from sale of assets other than at A1 or A2 or A3 or A4 above")
    rows = [line.split() for line in lines[start + 1 : start + 11]]
    assert [(r[1], r[-1]) for r in rows] == [
        ("a(i)", "₹0"),
        ("a(ii)", "₹5,07,581"),
        ("a(iii)", "₹5,07,581"),
        ("b(i)", "₹5,01,831"),
        ("b(ii)", "₹0"),
        ("b(iii)", "₹463"),
        ("b(iv)", "₹5,02,294"),
        ("c", "₹5,287"),
        ("d", "₹0"),
        ("e", "₹5,287"),
    ]
    assert "INFO B8. Long-term: from sale of assets where B1 to B7 above are not applicable" in lines
    assert "INFO   Row 5 (long-term)" in lines
    assert lines[-1] == "INFO ✓ B8 and Table F row 5: only the capital gain may be negative"


def test_generate_net_loss_keeps_negative_gain(cg, run_generate, sales_csv):
    run = run_generate(sales_csv(SAME_DAY))
    st = run.schedule[cg.SHORT_TERM]
    assert st["full_value"] == 489_081
    assert st["total_deductions"] == 489_081 + 463
    assert st["capital_gain"] == -463
    assert run.totals.loc[0, "Gain (INR)"].endswith("= -₹463")
    assert run.quarterly[cg.SHORT_TERM] == [0, 0, 0, 0, 0]


def test_generate_quarterly_accrual(cg, run_generate, sales_csv):
    dec = dict(acq="2026-12-20", sold="2026-12-20", units=1, price=50, cost=40)
    mar = dict(acq="2027-03-20", sold="2027-03-20", units=1, price=50, cost=45)
    run = run_generate(sales_csv(SAME_DAY, RSU, LONG, dec, mar))
    # Q1 5,287.50 → 5,288, trimmed by ₹1 so the split totals the rounded gain of 6,732.
    assert run.schedule[cg.SHORT_TERM]["capital_gain"] == 6_732
    assert run.quarterly[cg.SHORT_TERM] == [5_287, 0, 0, 960, 485]
    assert run.quarterly[cg.LONG_TERM] == [0, 0, 20_200, 0, 0]


def test_generate_quarterly_matches_schedule_gain(cg, run_generate, sales_csv):
    run = run_generate(sales_csv(SAME_DAY, RSU, LONG))
    for term in (cg.SHORT_TERM, cg.LONG_TERM):
        expected = max(run.schedule[term]["capital_gain"], 0)
        assert sum(run.quarterly[term]) == expected


@pytest.mark.parametrize(
    "net, split",
    [
        ([-100, 300, 0, 0, 0], [0, 200, 0, 0, 0]),
        ([300, -100, 0, 0, 0], [200, 0, 0, 0, 0]),
        ([100, -30, 50, 0, 0], [100, 0, 20, 0, 0]),
        ([-50, 0, 0, 0, 0], [0, 0, 0, 0, 0]),
        ([0, 0, 0, 10, 20], [0, 0, 0, 10, 20]),
    ],
)
def test_allocate_quarters_never_negative_and_totals_net(cg, net, split):
    assert cg.allocate_quarters(net) == split


def test_generate_ignores_sales_outside_fy(cg, run_generate, sales_csv):
    before = dict(acq="2026-03-20", sold="2026-03-20", units=1, price=10, cost=10)
    after = dict(acq="2027-04-02", sold="2027-04-02", units=1, price=10, cost=10)
    run = run_generate(sales_csv(before, SAME_DAY, after))
    assert len(run.audit) == 1
    assert run.audit.loc[0, "Sale Date"].startswith("2026-04-13")
    assert run.schedule[cg.SHORT_TERM]["full_value"] == 489_081


def test_generate_no_sales_in_fy_writes_zero_rows(cg, run_generate, sales_csv):
    run = run_generate(
        sales_csv(dict(acq="2026-03-20", sold="2026-03-20", units=1, price=10, cost=10))
    )
    assert run.audit.empty
    assert run.totals["Gain (INR)"].tolist() == ["₹0 − ₹0 − ₹0 = ₹0"] * 2
    assert [item["capital_gain"] for item in run.schedule.values()] == [0, 0]
    assert sum(sum(q) for q in run.quarterly.values()) == 0
    assert all(ok for _, ok in run.checks)


def test_generate_audit_trail_columns_and_order(cg, run_generate, sales_csv):
    audit = run_generate(sales_csv(LONG, SAME_DAY, RSU)).audit
    assert list(audit.columns) == cg.AUDIT_TRAIL_COLS
    assert audit["Type"].tolist() == ["SO", "RSU", "RSU"]
    assert audit["Term"].tolist() == [
        "Short-term (0 days)",
        "Short-term (370 days)",
        "Long-term (893 days)",
    ]

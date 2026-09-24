"""validate(): every check passes for generated data and fails when its value is broken."""

import copy

import pytest

SAME_DAY = dict(acq="2026-04-13", sold="2026-04-13", units=416, price=12.71, cost=12.71, fees=5)
RSU = dict(acq="2025-04-15", sold="2026-04-20", units=10, price=20, cost=15, benefit_type="RSU")
LONG = dict(acq="2024-04-10", sold="2026-09-20", units=10, price=30, cost=10, benefit_type="RSU")

TABLE_F_TOTAL = "Table F row 5: periods add up to the B8 gain, or 0 for a loss"
WHOLE_RUPEES = "B8 and Table F row 5: amounts are whole rupees, at most 14 digits"
NOT_NEGATIVE = "B8 and Table F row 5: only the capital gain may be negative"


@pytest.fixture
def built(run_generate, sales_csv):
    run = run_generate(sales_csv(SAME_DAY, RSU, LONG))
    return copy.deepcopy(run.schedule), copy.deepcopy(run.quarterly)


def _failed(cg, schedule, quarterly):
    return [text for text, ok in cg.validate(schedule, quarterly) if not ok]


def test_all_checks_pass_for_generated_data(cg, built):
    assert _failed(cg, *built) == []


def test_all_checks_pass_with_a_loss(run_generate, sales_csv):
    assert all(ok for _, ok in run_generate(sales_csv(SAME_DAY)).checks)


def test_check_descriptions(cg, built):
    texts = [text for text, _ in cg.validate(*built)]
    assert len(texts) == 12
    assert texts[:6] == [
        "A5: no expenses claimed when the full value is zero",
        "A5: total deductions = cost of acquisition + improvement + transfer expenses",
        "A5: capital gain = full value – total deductions",
        "Table F row 3: periods add up to the A5 gain, or 0 for a loss",
        "A5 and Table F row 3: amounts are whole rupees, at most 14 digits",
        "A5 and Table F row 3: only the capital gain may be negative",
    ]


def _set(item: dict, table_f: list, **changes):
    """Change item fields; keep total deductions and gain consistent unless given."""
    item.update({k: v for k, v in changes.items() if k != "quarters"})
    if "total_deductions" not in changes:
        item["total_deductions"] = (
            item["cost_of_acquisition"] + item["cost_of_improvement"] + item["transfer_expenses"]
        )
    if "capital_gain" not in changes:
        item["capital_gain"] = item["full_value"] - item["total_deductions"]
    if "quarters" in changes:
        table_f[:] = changes["quarters"]


@pytest.mark.parametrize(
    "changes, failed",
    [
        (dict(full_value=0, quarters=[0] * 5), ["B8: no expenses claimed when the full value is zero"]),
        (
            dict(total_deductions=8_301),
            ["B8: total deductions = cost of acquisition + improvement + transfer expenses", TABLE_F_TOTAL],
        ),
        (dict(capital_gain=20_201), ["B8: capital gain = full value – total deductions", TABLE_F_TOTAL]),
        (dict(quarters=[1, 0, 20_200, 0, 0]), [TABLE_F_TOTAL]),
        (dict(cost_of_improvement=0.5), [TABLE_F_TOTAL, WHOLE_RUPEES]),
        (dict(full_value=10**14), [TABLE_F_TOTAL, WHOLE_RUPEES]),
        (dict(cost_of_improvement=-100), [TABLE_F_TOTAL, NOT_NEGATIVE]),
        (dict(quarters=[-5, 5, 20_200, 0, 0]), [NOT_NEGATIVE]),
    ],
)
def test_each_check_catches_a_broken_value(cg, built, changes, failed):
    schedule, quarterly = built
    _set(schedule[cg.LONG_TERM], quarterly[cg.LONG_TERM], **changes)
    assert _failed(cg, schedule, quarterly) == failed


def test_negative_gain_is_allowed(cg, built):
    schedule, quarterly = built
    _set(schedule[cg.SHORT_TERM], quarterly[cg.SHORT_TERM], full_value=1_000, quarters=[0] * 5)
    assert schedule[cg.SHORT_TERM]["capital_gain"] < 0
    assert _failed(cg, schedule, quarterly) == []

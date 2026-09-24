"""Regression against your real data (never committed).

For every inputs/expected_<year>.csv, re-runs that year from inputs/input.csv,
inputs/prices.csv (and inputs/sales.csv if present) with the real SBI rates and
asserts the Schedule FA output is unchanged. Skipped when the files are absent.
"""

import io
import re
from contextlib import redirect_stdout
from pathlib import Path

import pandas as pd
import pytest

V2_DIR = Path(__file__).resolve().parents[1]
INPUTS = V2_DIR / "inputs"
EXPECTED = sorted(INPUTS.glob("expected_*.csv"))


@pytest.mark.skipif(not EXPECTED, reason="no inputs/expected_<year>.csv files")
@pytest.mark.parametrize("expected", EXPECTED, ids=lambda p: p.stem)
def test_real_output_unchanged(fa, set_cy, monkeypatch, tmp_path, expected):
    rates = fa.REPO_ROOT / "sbi-fx-ratekeeper" / "csv_files" / "SBI_REFERENCE_RATES_USD.csv"
    if not rates.exists() or not (INPUTS / "input.csv").exists():
        pytest.skip("real SBI rates or inputs/input.csv not available")

    set_cy(int(re.search(r"\d{4}", expected.stem).group()))
    fa.load_prices(INPUTS / "prices.csv")
    sales = INPUTS / "sales.csv"
    output = tmp_path / "out.csv"
    with redirect_stdout(io.StringIO()):
        fa.generate(INPUTS / "input.csv", output, sales if sales.exists() else None)

    pd.testing.assert_frame_equal(pd.read_csv(output), pd.read_csv(expected))

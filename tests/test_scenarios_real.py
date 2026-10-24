"""The Phase 5 gate on the real gold data, with greedy shifting only (LP is exercised by
`ets simulate`; it takes about 30 s)."""

import duckdb
import pytest

from ets.config import load_config
from ets.settings import GOLD_DB
from ets.simulate.context import OPSD_SILVER, load_gold_context
from ets.simulate.run import run_scenarios

pytestmark = pytest.mark.skipif(
    not (GOLD_DB.exists() and OPSD_SILVER.exists()),
    reason="run `ets ingest` and `ets transform` first",
)

CFG = load_config()


@pytest.fixture(scope="module")
def summary():
    gold = duckdb.connect(str(GOLD_DB), read_only=True)
    try:
        ctx = load_gold_context(gold)
    finally:
        gold.close()
    results = duckdb.connect()  # results go to memory, the real database is not touched
    run_scenarios(CFG, results, methods=("none", "greedy"), ctx=ctx)
    rows = results.execute(
        "select household_id, variant_id, method, annual_eur, saving_vs_fixed_eur, "
        "saving_timing_eur, shifted_kwh from sim_summary"
    ).fetchall()
    return {
        (h, v, m): (annual, saving, timing, shifted)
        for h, v, m, annual, saving, timing, shifted in rows
    }


def pct(summary, hid, vid, method) -> float:
    return 100 * summary[(hid, vid, method)][1] / summary[(hid, "fixed", "none")][0]


def test_type_a_is_unchanged_from_phase_4(summary):
    assert pct(summary, "A_standard", "dynamic", "none") == pytest.approx(1.42, abs=0.01)


def test_types_b_and_c_save_more_than_type_a_after_shifting(summary):
    a = pct(summary, "A_standard", "dynamic", "none")
    assert pct(summary, "B_ev", "dynamic", "greedy") > a + 3
    assert pct(summary, "C_heat_pump", "dynamic", "greedy") > a + 3


def test_shifting_never_costs_the_ev_or_heat_pump_household_money(summary):
    for hid in ("B_ev", "C_heat_pump"):
        for vid in ("dynamic", "dynamic_m1", "dynamic_m2", "dynamic_m3", "dynamic_m1_m3"):
            assert summary[(hid, vid, "greedy")][0] <= summary[(hid, vid, "none")][0] + 1e-9
            assert summary[(hid, vid, "greedy")][3] > 0  # something was actually shifted


def test_timing_saving_is_zero_without_shifting(summary):
    for (_, _, method), (_, _, timing, shifted) in summary.items():
        if method == "none":
            assert timing == pytest.approx(0) and shifted == 0

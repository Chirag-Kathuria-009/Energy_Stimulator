"""Unshifted billing on the real gold data (skipped when the pipeline has not been run)."""

import duckdb
import pytest
from typer.testing import CliRunner

from ets.cli import app
from ets.config import load_config
from ets.settings import GOLD_DB
from ets.simulate.context import OPSD_SILVER, load_gold_context
from ets.simulate.run import build_households, run_baseline, saving_vs_fixed_pct

pytestmark = pytest.mark.skipif(
    not (GOLD_DB.exists() and OPSD_SILVER.exists()),
    reason="run `ets ingest` and `ets transform` first",
)

CFG = load_config()


@pytest.fixture(scope="module")
def real():
    con = duckdb.connect(str(GOLD_DB), read_only=True)
    try:
        ctx = load_gold_context(con)
    finally:
        con.close()
    households = build_households(CFG, ctx)
    results = run_baseline(CFG, ctx, households)
    return ctx, {h.id: h for h in households}, results


def cost(results, household, variant) -> float:
    return next(
        r.annual_eur for r in results if (r.household_id, r.variant_id) == (household, variant)
    )


def test_type_a_gate_dynamic_saving_is_small_and_positive(real):
    _, _, results = real
    saving = saving_vs_fixed_pct(results, "A_standard", "dynamic")
    assert -2.0 <= saving <= 5.0  # the plan's gate
    # Independent estimate from the raw prices before any pipeline code existed (DECISIONS.md #13):
    assert saving == pytest.approx(1.42, abs=0.15)


def test_result_set_has_the_expected_rows(real):
    _, _, results = real
    per_household = {}
    for r in results:
        per_household.setdefault(r.household_id, []).append(r.variant_id)
    assert per_household["A_standard"] == ["fixed", "dynamic"]  # no device, no §14a module
    for hid in ("B_ev", "C_heat_pump", "D_full"):
        assert len(per_household[hid]) == 6
    assert all(r.method == "none" and r.shifted_kwh == 0 for r in results)


def test_fixed_bill_is_kwh_times_the_fixed_price(real):
    ctx, households, results = real
    price = CFG.price_components.fixed_tariff_gross_ct_kwh
    for hid in ("A_standard", "B_ev", "C_heat_pump"):  # no PV, so import = consumption
        hh = households[hid]
        kwh = hh.base_kwh.sum() + hh.ev_baseline_kwh.sum() + hh.hp_baseline_kwh.sum()
        assert cost(results, hid, "fixed") == pytest.approx(kwh * price / 100)


def test_module1_is_exactly_the_credit_below_plain_dynamic(real):
    _, _, results = real
    for hid in ("B_ev", "C_heat_pump", "D_full"):
        diff = cost(results, hid, "dynamic") - cost(results, hid, "dynamic_m1")
        assert diff == pytest.approx(137.95 * 1.19)
        diff3 = cost(results, hid, "dynamic_m3") - cost(results, hid, "dynamic_m1_m3")
        assert diff3 == pytest.approx(137.95 * 1.19)


def test_module2_saves_60_percent_of_the_grid_fee_on_device_energy(real):
    ctx, households, results = real
    hh = households["C_heat_pump"]  # no PV: all device energy is imported
    expected = hh.hp_baseline_kwh.sum() * 9.43 * (1 - 0.4) * 1.19 / 100
    assert cost(results, "C_heat_pump", "dynamic") - cost(
        results, "C_heat_pump", "dynamic_m2"
    ) == pytest.approx(expected)


def test_modules_never_change_the_base_load_cost(real):
    _, _, results = real
    # Module 2 and 3 only touch device energy: a household whose device energy is zero would be
    # unchanged. Household A has no modules; B's EV charges in the standard window only, so
    # Module 3 equals plain dynamic there.
    assert cost(results, "B_ev", "dynamic_m3") == pytest.approx(cost(results, "B_ev", "dynamic"))


def test_ev_dumb_charging_misses_module3_low_window_but_hp_gains_a_little(real):
    _, _, results = real
    # Evening charging lands in the 17:00-19:00 standard window, so Module 3 gives the EV nothing.
    assert cost(results, "B_ev", "dynamic_m3") >= cost(results, "B_ev", "dynamic") - 1e-9
    # The heat pump runs through the day incl. the low window 10:00-17:00, so it gains a little.
    assert cost(results, "C_heat_pump", "dynamic_m3") < cost(results, "C_heat_pump", "dynamic")


def test_cli_baseline_prints_the_table_and_passes_the_gate():
    out = CliRunner().invoke(app, ["baseline"])
    assert out.exit_code == 0, out.output
    assert "A_standard" in out.output and "dynamic_m1_m3" in out.output
    assert "PASS" in out.output

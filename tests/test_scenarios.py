"""apply_shifting, scenarios and the result tables, on the synthetic gold fixture with a daily
price pattern (cheap night, expensive evening) so shifting has something to gain."""

import duckdb
import numpy as np
import pytest
from tests.gold_fixture import make_gold_con, write_pv_parquet

from ets.config import load_config
from ets.optimize import lp as lp_module
from ets.simulate import run as run_module
from ets.simulate.context import load_gold_context
from ets.simulate.run import (
    METHODS,
    apply_shifting,
    build_households,
    run_scenarios,
    scenarios,
    simulate_one,
    spike_window_start,
)
from ets.tariffs.billing import build_variants, variant_prices

CFG = load_config()
V = {v.id: v for v in build_variants(CFG)}


@pytest.fixture(scope="module")
def ctx(tmp_path_factory):
    con = make_gold_con()
    con.execute(
        "update fact_price_qh set price_ct_kwh = case when t.qh_of_day < 24 then 2.0 "
        "when t.qh_of_day between 68 and 83 then 25.0 else 8.0 end "
        "from dim_time t where fact_price_qh.idx = t.idx"
    )
    pv = write_pv_parquet(tmp_path_factory.mktemp("pv") / "pv.parquet")
    return load_gold_context(con, pv)


@pytest.fixture(scope="module")
def hh(ctx):
    return {h.id: h for h in build_households(CFG, ctx)}


def fresh_con() -> duckdb.DuckDBPyConnection:
    return duckdb.connect()


# ------------------------------------------------------------------ apply_shifting


def test_none_is_the_unshifted_baseline(ctx, hh):
    for hid in ("B_ev", "C_heat_pump", "D_full"):
        h = hh[hid]
        dev, short = apply_shifting(h, variant_prices(V["dynamic"], ctx, CFG), "none", ctx)
        assert (dev == h.ev_baseline_kwh + h.hp_baseline_kwh).all() and short == 0


def test_unknown_method_is_rejected(ctx, hh):
    with pytest.raises(ValueError, match="unknown method"):
        apply_shifting(hh["B_ev"], variant_prices(V["dynamic"], ctx, CFG), "magic", ctx)


@pytest.mark.parametrize("method", ["greedy", "lp"])
def test_ev_energy_is_conserved_and_stays_in_the_plug_in_windows(ctx, hh, method):
    h = hh["B_ev"]
    dev, short = apply_shifting(h, variant_prices(V["dynamic"], ctx, CFG), method, ctx)
    assert short == 0 and dev.sum() == pytest.approx(h.ev_baseline_kwh.sum())
    for s in h.ev_sessions:
        assert dev[s.start_idx : s.end_idx].sum() == pytest.approx(s.energy_kwh)
    inside = np.zeros(ctx.n, dtype=bool)
    for s in h.ev_sessions:
        inside[s.start_idx : s.end_idx] = True
    assert dev[~inside].sum() == 0 and dev.max() <= 11 * 0.25 + 1e-9


def test_heat_pump_delivers_the_daily_heat_after_shifting(ctx, hh):
    h = hh["C_heat_pump"]
    dev, _ = apply_shifting(h, variant_prices(V["dynamic"], ctx, CFG), "greedy", ctx)
    for a, b in ctx.day_slices():
        assert (dev[a:b] * h.hp_cop[a:b]).sum() == pytest.approx(h.hp_heat_kwh[a:b].sum())
    assert dev.max() <= h.hp_p_max_kw * 0.25 + 1e-9


def test_day_slices_follow_the_local_calendar(ctx):
    days = ctx.day_slices()
    assert len(days) == 365 and days[0][0] == 0 and days[-1][1] == ctx.n
    assert sorted({b - a for a, b in days}) == [92, 96, 100]
    assert all(days[i][1] == days[i + 1][0] for i in range(364))


def test_variants_with_the_same_device_price_share_one_solve(ctx, hh, monkeypatch):
    calls = []
    real = run_module.shift_ev_lp
    monkeypatch.setattr(run_module, "shift_ev_lp", lambda p, s: calls.append(1) or real(p, s))
    cache: dict = {}
    for vid in ("dynamic", "dynamic_m1"):  # identical device price: standard grid fee
        apply_shifting(hh["B_ev"], variant_prices(V[vid], ctx, CFG), "lp", ctx, cache)
    assert len(calls) == 1
    apply_shifting(hh["B_ev"], variant_prices(V["dynamic_m3"], ctx, CFG), "lp", ctx, cache)
    assert len(calls) == 2  # a different device price needs its own solve


# ------------------------------------------------------------------ scenarios / simulate_one


def test_scenario_plan_skips_pointless_combinations(ctx, hh):
    plan = scenarios(list(hh.values()), build_variants(CFG), METHODS)
    keys = {(h.id, v.id, m) for h, v, m in plan}
    assert ("A_standard", "dynamic", "none") in keys and ("A_standard", "fixed", "none") in keys
    assert not any(h == "A_standard" and m != "none" for h, _, m in keys)  # no device
    assert not any(v == "fixed" and m != "none" for _, v, m in keys)  # flat price
    assert ("B_ev", "dynamic_m3", "lp") in keys and ("A_standard", "dynamic_m1", "none") not in keys
    assert len(plan) == 2 + 3 * 16  # A: 2; B, C, D: 6 none + 5 greedy + 5 lp


def test_shifting_lowers_the_bill_with_a_cheap_night_price(ctx, hh):
    none = simulate_one(hh["B_ev"], V["dynamic"], "none", ctx, CFG)
    greedy = simulate_one(hh["B_ev"], V["dynamic"], "greedy", ctx, CFG)
    lp = simulate_one(hh["B_ev"], V["dynamic"], "lp", ctx, CFG)
    assert lp.annual_eur <= greedy.annual_eur + 1e-6 < none.annual_eur
    assert none.shifted_kwh == 0 and greedy.shifted_kwh > 1000
    assert greedy.device_kwh.sum() == pytest.approx(none.device_kwh.sum())


def test_heat_pump_lp_beats_greedy_beats_unshifted(ctx, hh):
    cost = {
        m: simulate_one(hh["C_heat_pump"], V["dynamic"], m, ctx, CFG).annual_eur for m in METHODS
    }
    assert cost["lp"] <= cost["greedy"] + 1e-6 and cost["greedy"] < cost["none"]


def test_module3_steers_the_ev_into_the_low_windows(ctx, hh):
    plain = simulate_one(hh["B_ev"], V["dynamic"], "greedy", ctx, CFG)
    m3 = simulate_one(hh["B_ev"], V["dynamic_m3"], "greedy", ctx, CFG)
    assert m3.annual_eur < plain.annual_eur  # the low window 01:30-04:30 is cheaper still


# ------------------------------------------------------------------ result tables


@pytest.fixture(scope="module")
def run(ctx, hh):
    con = fresh_con()
    run_id = run_scenarios(CFG, con, methods=("none", "greedy"), ctx=ctx)  # LP: see below
    return con, run_id


def test_run_is_recorded_and_tables_have_the_planned_shape(run, ctx):
    con, run_id = run
    status, sha, chash = con.execute(
        "select status, git_sha, config_hash from sim_runs where run_id = ?", [run_id]
    ).fetchone()
    assert status == "ok" and sha and len(chash) == 64
    n_sets = con.execute("select count(*) from sim_summary").fetchone()[0]
    assert n_sets == 2 + 3 * 11  # A: 2; B, C, D: 6 none + 5 greedy
    assert con.execute("select count(*) from sim_results_qh").fetchone()[0] == n_sets * ctx.n
    assert con.execute("select count(*) from v_latest_summary").fetchone()[0] == n_sets


def test_savings_split_adds_up_for_every_row(run):
    con, _ = run
    rows = con.execute(
        "select household_id, variant_id, method, saving_vs_fixed_eur, saving_tariff_switch_eur, "
        "saving_timing_eur, saving_gridfee_eur from sim_summary"
    ).fetchall()
    assert rows
    for hid, vid, m, total, switch, timing, gridfee in rows:
        assert total == pytest.approx(switch + timing + gridfee, abs=1e-9), (hid, vid, m)
        if vid == "dynamic":
            assert gridfee == pytest.approx(0, abs=1e-9)  # no module, nothing to attribute
        if m == "none":
            assert timing == pytest.approx(0, abs=1e-9)  # nothing shifted
        if vid == "fixed":
            assert total == 0


def test_module_grid_fee_saving_is_the_module_credit_without_shifting(run):
    con, _ = run
    saving = con.execute(
        "select saving_gridfee_eur from sim_summary where household_id = 'B_ev' "
        "and variant_id = 'dynamic_m1' and method = 'none'"
    ).fetchone()[0]
    assert saving == pytest.approx(137.95 * 1.19)


def test_fixed_rows_equal_the_sum_of_their_quarter_hour_costs(run):
    con, _ = run
    for hid, annual, energy in con.execute(
        "select s.household_id, s.annual_eur, sum(r.cost_ct) / 100 from sim_summary s "
        "join sim_results_qh r using (run_id, household_id, variant_id, method) "
        "where s.variant_id = 'fixed' group by all"
    ).fetchall():
        assert annual == pytest.approx(energy), hid


def test_worst_month_and_spike_week_match_an_independent_calculation(run, ctx):
    con, run_id = run
    key = ("C_heat_pump", "dynamic", "greedy")
    annual, worst, spike = con.execute(
        "select annual_eur, worst_month_eur, spike_week_eur from sim_summary where run_id = ? "
        "and household_id = ? and variant_id = ? and method = ?",
        [run_id, *key],
    ).fetchone()
    cost = np.array(
        con.execute(
            "select cost_ct from sim_results_qh where household_id = ? and variant_id = ? "
            "and method = ? order by idx",
            list(key),
        ).fetchnumpy()["cost_ct"]
    )
    fixed_part = annual - cost.sum() / 100
    months = [cost[ctx.month == m].sum() / 100 + fixed_part / 12 for m in range(1, 13)]
    assert worst == pytest.approx(max(months))
    start = spike_window_start(ctx.price_ct)
    window_mean = ctx.price_ct[start : start + 672].mean()
    assert all(
        ctx.price_ct[s : s + 672].mean() <= window_mean + 1e-12 for s in range(0, ctx.n - 672, 97)
    )
    assert spike == pytest.approx(cost[start : start + 672].sum() / 100 + fixed_part * 7 / 365)


def test_lp_rows_are_stored_and_split_correctly(ctx, hh):
    con = fresh_con()
    run_scenarios(CFG, con, methods=("none", "greedy", "lp"), ctx=ctx, households=[hh["B_ev"]])
    cost = dict(
        con.execute(
            "select method, annual_eur from sim_summary where variant_id = 'dynamic'"
        ).fetchall()
    )
    assert cost["lp"] <= cost["greedy"] + 1e-6 < cost["none"]
    total, switch, timing, gridfee = con.execute(
        "select saving_vs_fixed_eur, saving_tariff_switch_eur, saving_timing_eur, "
        "saving_gridfee_eur from sim_summary where variant_id = 'dynamic_m3' and method = 'lp'"
    ).fetchone()
    assert total == pytest.approx(switch + timing + gridfee)
    assert timing > 0 and gridfee > 0  # shifting gain, plus the Module 3 window fees


def test_results_are_reproducible(ctx, hh):
    first, second = fresh_con(), fresh_con()
    pair = [hh["B_ev"], hh["C_heat_pump"]]
    run_scenarios(CFG, first, methods=("none", "greedy"), ctx=ctx, households=pair)
    run_scenarios(CFG, second, methods=("none", "greedy"), ctx=ctx, households=pair)
    q = "select household_id, variant_id, method, annual_eur from sim_summary order by 1, 2, 3"
    assert first.execute(q).fetchall() == second.execute(q).fetchall()


def test_only_the_newest_two_runs_keep_their_quarter_hour_rows(ctx, hh):
    con = fresh_con()
    one_household = [hh["B_ev"]]  # keep the three runs cheap
    ids = [
        run_scenarios(CFG, con, methods=("none",), ctx=ctx, households=one_household)
        for _ in range(3)
    ]
    with_rows = {r[0] for r in con.execute("select distinct run_id from sim_results_qh").fetchall()}
    assert with_rows == set(ids[1:])
    assert con.execute("select count(distinct run_id) from sim_summary").fetchone()[0] == 3
    assert con.execute("select count(*) from sim_runs where status = 'ok'").fetchone()[0] == 3
    latest = con.execute("select distinct run_id from v_latest_summary").fetchall()
    assert latest == [(ids[-1],)]


def test_a_failing_run_is_marked_failed_and_leaves_earlier_results_intact(ctx, hh, monkeypatch):
    con = fresh_con()
    good = run_scenarios(CFG, con, methods=("none",), ctx=ctx, households=[hh["B_ev"]])
    before = con.execute("select count(*) from sim_results_qh").fetchone()[0]

    def boom(*args, **kwargs):
        raise RuntimeError("solver exploded")

    monkeypatch.setattr(run_module, "simulate_one", boom)
    with pytest.raises(RuntimeError, match="solver exploded"):
        run_scenarios(CFG, con, methods=("none",), ctx=ctx, households=[hh["B_ev"]])
    status, error = con.execute(
        "select status, error from sim_runs where run_id != ?", [good]
    ).fetchone()
    assert status == "failed" and "solver exploded" in error
    assert con.execute("select count(*) from sim_results_qh").fetchone()[0] == before
    assert con.execute("select distinct run_id from v_latest_summary").fetchall() == [(good,)]


def test_a_failure_while_writing_rolls_the_whole_transaction_back(ctx, hh, monkeypatch):
    con = fresh_con()

    def broken_summary(*args, **kwargs):
        raise ValueError("bad summary")

    monkeypatch.setattr(run_module, "summary_rows", broken_summary)
    with pytest.raises(ValueError, match="bad summary"):
        run_scenarios(CFG, con, methods=("none",), ctx=ctx, households=[hh["B_ev"]])
    assert con.execute("select count(*) from sim_results_qh").fetchone()[0] == 0  # no partial rows
    assert con.execute("select status from sim_runs").fetchone() == ("failed",)


def test_lp_module_is_used_for_the_lp_method(ctx, hh, monkeypatch):
    seen = []
    monkeypatch.setattr(
        run_module, "shift_ev_lp", lambda p, s: seen.append("lp") or lp_module.shift_ev_lp(p, s)
    )
    simulate_one(hh["B_ev"], V["dynamic"], "lp", ctx, CFG)
    simulate_one(hh["B_ev"], V["dynamic"], "greedy", ctx, CFG)
    assert seen == ["lp"]

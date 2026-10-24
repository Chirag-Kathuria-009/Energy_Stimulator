import duckdb
import numpy as np
import polars as pl
import pytest
from pydantic import ValidationError
from tests.priced_ctx import make_priced_ctx

from ets.config import AppConfig, load_config, load_raw
from ets.domain import Session
from ets.optimize.greedy import shift_ev_greedy, shift_ev_spread
from ets.settings import GOLD_DB
from ets.simulate.context import OPSD_SILVER, load_gold_context
from ets.simulate.herding import (
    aggregate,
    aggregate_scenarios,
    peak_report,
    run_herding,
    sample_household_cfgs,
    write_herding,
)
from ets.tariffs.billing import build_variants

CFG = load_config()
DYNAMIC = next(v for v in build_variants(CFG) if v.id == "dynamic")


def cfg_with(**herding) -> AppConfig:
    return CFG.model_copy(update={"herding": CFG.herding.model_copy(update=herding)})


@pytest.fixture(scope="module")
def ctx(tmp_path_factory):
    return make_priced_ctx(tmp_path_factory.mktemp("herd"))


# ------------------------------------------------------------------ configuration


def test_herding_config_is_validated():
    raw = load_raw()
    raw["herding"]["p_ev"] = 1.5
    with pytest.raises(ValidationError, match="p_ev"):
        AppConfig.model_validate(raw)
    raw = load_raw()
    raw["herding"]["annual_kwh"]["min"] = 4000  # above the mean
    with pytest.raises(ValidationError, match="min <= mean <= max"):
        AppConfig.model_validate(raw)


# ------------------------------------------------------------------ population


def test_same_seed_gives_the_same_population_and_another_seed_a_different_one():
    one = [c.model_dump() for c in sample_household_cfgs(60, CFG, 42)]
    assert one == [c.model_dump() for c in sample_household_cfgs(60, CFG, 42)]
    assert one != [c.model_dump() for c in sample_household_cfgs(60, CFG, 43)]


def test_population_respects_the_configured_bounds():
    cfgs = sample_household_cfgs(500, CFG, 1)
    h = CFG.herding
    assert len({c.id for c in cfgs}) == 500 and cfgs[0].id == "H0001"
    assert all(h.annual_kwh.min <= c.annual_kwh <= h.annual_kwh.max for c in cfgs)
    evs = [c.ev for c in cfgs if c.ev]
    assert all(h.ev_km.min <= e.km_per_year <= h.ev_km.max for e in evs)
    times = {e.plug_in_weekday for e in evs}
    assert times <= {f"{m // 60:02d}:{m % 60:02d}" for m in range(17 * 60, 19 * 60 + 1, 15)}
    assert len(times) > 5  # the jitter is actually used


def test_population_shares_and_means_are_close_to_the_configuration():
    cfgs = sample_household_cfgs(4000, CFG, 7)
    assert sum(c.ev is not None for c in cfgs) / 4000 == pytest.approx(0.30, abs=0.03)
    assert sum(c.heat_pump is not None for c in cfgs) / 4000 == pytest.approx(0.20, abs=0.03)
    assert np.mean([c.annual_kwh for c in cfgs]) == pytest.approx(3500, rel=0.03)


def test_devices_are_copies_of_the_templates_and_meter_fee_follows_the_device():
    for c in sample_household_cfgs(200, CFG, 3):
        if c.ev:
            assert c.ev.wallbox_kw == 11 and c.ev.plug_out_weekday == "07:00"
            assert c.ev.plug_in_weekend is None
        if c.heat_pump:
            assert c.heat_pump.p_max_kw == 6
        assert c.smart_meter_fee_eur_year == (50.0 if (c.ev or c.heat_pump) else 30.0)
        assert c.pv is None


def test_changing_a_probability_does_not_reshuffle_the_other_draws():
    low = sample_household_cfgs(200, cfg_with(p_ev=0.1), 5)
    high = sample_household_cfgs(200, cfg_with(p_ev=0.6), 5)
    assert [c.annual_kwh for c in low] == [c.annual_kwh for c in high]
    assert {c.id for c in low if c.ev} <= {c.id for c in high if c.ev}


def test_a_population_with_devices_needs_templates():
    no_devices = CFG.model_copy(update={"households": [CFG.households[0]]})
    with pytest.raises(ValueError, match="copy from"):
        sample_household_cfgs(10, no_devices, 1)


# ------------------------------------------------------------------ spread optimiser


def test_spread_delivers_the_energy_inside_the_window_among_the_k_cheapest_slots():
    rng = np.random.default_rng(0)
    price = rng.normal(10, 5, 200)
    sessions = [Session(10, 62, 7.3, 11.0), Session(100, 152, 12.0, 11.0)]
    out, short = shift_ev_spread(price, sessions, 8, np.random.default_rng(1))
    assert short == 0
    for s in sessions:
        seg = out[s.start_idx : s.end_idx]
        assert seg.sum() == pytest.approx(s.energy_kwh) and seg.max() <= 11 * 0.25 + 1e-12
        kth_price = np.sort(price[s.start_idx : s.end_idx])[7]
        used = np.flatnonzero(seg) + s.start_idx
        assert (price[used] <= kth_price).all()
    assert out[62:100].sum() == 0


def test_spread_uses_more_slots_when_the_energy_needs_them():
    price = np.arange(20, dtype=float)
    out, short = shift_ev_spread(price, [Session(0, 20, 11.0, 4.0)], 3, np.random.default_rng(0))
    assert short == 0 and (out > 0).sum() == 11  # 1 kWh per slot, k=3 is too small for 11 kWh


def test_spread_costs_a_little_more_than_greedy_and_is_reproducible():
    rng = np.random.default_rng(2)
    price = rng.normal(10, 5, 120)
    sessions = [Session(0, 60, 9.0, 11.0), Session(60, 120, 9.0, 11.0)]
    greedy, _ = shift_ev_greedy(price, sessions)
    a, _ = shift_ev_spread(price, sessions, 12, np.random.default_rng(9))
    b, _ = shift_ev_spread(price, sessions, 12, np.random.default_rng(9))
    assert (a == b).all()
    assert (a * price).sum() >= (greedy * price).sum() - 1e-9


# ------------------------------------------------------------------ aggregation


@pytest.fixture(scope="module")
def small(ctx):
    cfg = cfg_with(n_households=60, p_ev=0.5, p_hp=0.0)
    cfgs = sample_household_cfgs(60, cfg, 11)
    return cfg, cfgs, aggregate_scenarios(cfgs, ctx, cfg, DYNAMIC, seed=11)


def test_aggregation_is_reproducible(ctx, small):
    cfg, cfgs, curves = small
    again = aggregate_scenarios(cfgs, ctx, cfg, DYNAMIC, seed=11)
    for method in curves:
        assert (curves[method] == again[method]).all()
    assert (aggregate(cfgs, ctx, cfg, "greedy", DYNAMIC, seed=11) == curves["greedy"]).all()


def test_shifting_moves_energy_and_never_creates_it(small):
    _, _, curves = small
    totals = {m: kw.sum() * 0.25 for m, kw in curves.items()}
    assert totals["greedy"] == pytest.approx(totals["none"], rel=1e-9)
    assert totals["spread"] == pytest.approx(totals["none"], rel=1e-9)


def test_with_heat_pumps_only_the_cop_gain_changes_the_total(ctx):
    cfg = cfg_with(n_households=40, p_ev=0.0, p_hp=1.0)
    cfgs = sample_household_cfgs(40, cfg, 4)
    curves = aggregate_scenarios(cfgs, ctx, cfg, DYNAMIC, ("none", "greedy"), seed=4)
    none, greedy = curves["none"].sum(), curves["greedy"].sum()
    # Heat is conserved; electricity can only fall (better-COP slots). In this fixture the COP is
    # constant within a day, so the totals are equal; real data shows -1.8% (DECISIONS.md #29).
    assert 0.95 * none < greedy <= none * (1 + 1e-9)


def test_everybody_following_the_same_price_creates_a_higher_peak(small):
    _, _, curves = small
    assert curves["greedy"].max() > 1.5 * curves["none"].max()


def test_spreading_removes_most_of_the_herding_peak(small):
    _, _, curves = small
    assert curves["spread"].max() < 0.7 * curves["greedy"].max()


# ------------------------------------------------------------------ peak report and tables


def test_peak_report_columns_and_formulas(ctx):
    kw = np.full(ctx.n, 100.0)
    kw[1234] = 900.0
    row = peak_report({"x": kw}, ctx).row(0, named=True)
    assert row["peak_kw"] == 900.0 and row["mean_kw"] == pytest.approx(kw.mean())
    assert row["peak_to_mean"] == pytest.approx(900.0 / kw.mean())
    assert row["total_mwh"] == pytest.approx(kw.sum() * 0.25 / 1000)
    assert row["peak_ts_local"][:4] in ("2025", "2026")


def test_share_of_nights_with_the_peak_in_the_cheapest_quarter_hour(ctx):
    # Cheapest quarter-hour of each night is the first one of the 00:00-06:00 block (ties: earlier).
    nights = []
    dates = ctx.dates()
    from datetime import datetime
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Europe/Berlin")
    for d in dates[1:]:
        nights.append(ctx.idx_of_local(datetime.combine(d, datetime.min.time(), tzinfo=tz)))
    herd = np.zeros(ctx.n)
    herd[nights] = 500.0  # a spike at the cheapest slot of every night
    flat = np.ones(ctx.n)  # no spike: the earliest slot of the night (18:00) wins the argmax
    report = peak_report({"herd": herd, "flat": flat}, ctx).to_dict(as_series=False)
    share = dict(zip(report["scenario"], report["share_nights_peak_in_cheapest_qh"], strict=True))
    assert share["herd"] == 1.0 and share["flat"] == 0.0


def test_write_herding_replaces_the_tables(ctx):
    con = duckdb.connect()
    cfg = cfg_with(n_households=20)
    for _ in range(2):  # second run must replace, not append
        summary = run_herding(cfg, con, ctx=ctx)
    assert summary["scenario"].to_list() == ["unshifted", "greedy", "greedy_spread"]
    assert con.execute("select count(*) from herding_curve").fetchone()[0] == 3 * ctx.n
    rows = con.execute("select scenario, peak_kw from herding_summary order by 1").fetchall()
    assert [r[0] for r in rows] == ["greedy", "greedy_spread", "unshifted"]
    assert all(r[1] > 0 for r in rows)


def test_write_failure_rolls_back_and_keeps_the_old_tables(ctx):
    con = duckdb.connect()
    cfg = cfg_with(n_households=10)
    run_herding(cfg, con, ctx=ctx)
    bad = pl.DataFrame({"scenario": ["x"], "wrong": [1.0]})
    with pytest.raises(duckdb.Error):
        write_herding(con, {"x": np.zeros(ctx.n)}, bad)
    assert con.execute("select count(*) from herding_curve").fetchone()[0] == 3 * ctx.n
    assert con.execute("select count(*) from herding_summary").fetchone()[0] == 3


# ------------------------------------------------------------------ real data (skipped if absent)


@pytest.mark.skipif(
    not (GOLD_DB.exists() and OPSD_SILVER.exists()), reason="run the pipeline first"
)
def test_real_prices_produce_a_herding_peak_that_the_spread_removes():
    gold = duckdb.connect(str(GOLD_DB), read_only=True)
    try:
        real = load_gold_context(gold)
    finally:
        gold.close()
    summary = run_herding(cfg_with(n_households=120), duckdb.connect(), ctx=real)
    peak = dict(zip(summary["scenario"], summary["peak_kw"], strict=True))
    assert peak["greedy"] > 1.3 * peak["unshifted"]
    assert peak["greedy_spread"] < peak["greedy"]

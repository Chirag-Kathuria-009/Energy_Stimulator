import logging
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import duckdb
import numpy as np
import pytest
from tests.gold_fixture import make_ctx, make_gold_con, write_pv_parquet

from ets.config import EVCfg, HeatPumpCfg, HouseholdCfg, PVCfg, load_config
from ets.profiles.archetypes import (
    build_base_load,
    build_ev,
    build_heat_pump,
    build_household,
    build_pv,
    household_totals,
)
from ets.settings import GOLD_DB
from ets.simulate.context import OPSD_SILVER, load_gold_context
from ets.simulate.run import archetype_label, build_households, write_dim_household

BERLIN = ZoneInfo("Europe/Berlin")


@pytest.fixture(scope="module")
def ctx(tmp_path_factory):
    return make_ctx(tmp_path_factory.mktemp("pv"))


def local_str(ctx, idx: int) -> str:
    t = ctx.ts_utc[idx].astype(datetime).replace(tzinfo=UTC).astimezone(BERLIN)
    return t.strftime("%Y-%m-%d %H:%M")


def ev_cfg(**over) -> EVCfg:
    base = dict(
        km_per_year=15000, kwh_per_100km=17, battery_kwh=60, wallbox_kw=11,
        plug_in_weekday="18:00", plug_out_weekday="07:00",
    )  # fmt: skip
    return EVCfg(**{**base, **over})


def hp_cfg(**over) -> HeatPumpCfg:
    base = dict(annual_heat_kwh=15000, dhw_share=0.165, p_max_kw=6, thermal_buffer_kwh=10)
    return HeatPumpCfg(**{**base, **over})


# ------------------------------------------------------------------ context


def test_context_arrays_line_up(ctx):
    assert ctx.n == 35040
    for arr in (
        ctx.ts_utc,
        ctx.date_local,
        ctx.qh_of_day,
        ctx.weekday,
        ctx.price_ct,
        ctx.cop_space,
    ):
        assert len(arr) == ctx.n
    assert set(ctx.profiles) == {"H25", "P25"}
    assert ctx.heat_space_norm.sum() == pytest.approx(1.0)
    assert ctx.pv_shape.sum() == pytest.approx(1.0)


def test_idx_of_local_handles_dst(ctx):
    assert ctx.idx_of_local(datetime(2025, 10, 1, 0, 0, tzinfo=BERLIN)) == 0
    for local in (
        datetime(2025, 10, 26, 18, 0, tzinfo=BERLIN),  # after the October change
        datetime(2026, 3, 29, 18, 0, tzinfo=BERLIN),  # after the March change
    ):
        assert local_str(ctx, ctx.idx_of_local(local)) == local.strftime("%Y-%m-%d %H:%M")
    assert ctx.idx_of_local(datetime(2026, 10, 1, 0, 0, tzinfo=BERLIN)) == ctx.n  # one past the end


def test_context_rejects_misaligned_tables():
    con = make_gold_con()
    con.execute("delete from fact_price_qh where idx = 100")
    with pytest.raises(ValueError, match="does not line up"):
        load_gold_context(con, opsd_parquet=None)


# ------------------------------------------------------------------ base load


def test_base_load_hits_annual_kwh(ctx):
    base = build_base_load(3500, ctx)
    assert base.sum() == pytest.approx(3500.0) and len(base) == ctx.n
    assert build_base_load(5000, ctx).sum() == pytest.approx(5000.0)


def test_base_load_rejects_a_profile_that_misses_the_annual_total(ctx):
    broken = {**ctx.profiles, "H25": ctx.profiles["H25"] * 1.05}
    with pytest.raises(ValueError, match="expected 3500"):
        build_base_load(3500, type(ctx)(**{**ctx.__dict__, "profiles": broken}))


# ------------------------------------------------------------------ EV


def test_ev_one_session_per_day_with_the_configured_window(ctx):
    sessions, baseline = build_ev(ev_cfg(), ctx)
    assert len(sessions) == len(ctx.dates()) == 365
    for s in sessions[:-1]:
        assert 0 <= s.start_idx < s.end_idx <= ctx.n
        assert (
            local_str(ctx, s.start_idx)[11:] == "18:00"
            and local_str(ctx, s.end_idx)[11:] == "07:00"
        )
    last = sessions[-1]
    assert last.end_idx == ctx.n and last.end_idx - last.start_idx == 24  # cut off at the year end


def test_ev_energy_is_daily_energy_times_days(ctx):
    sessions, baseline = build_ev(ev_cfg(), ctx)
    assert baseline.sum() == pytest.approx(15000 * 17 / 100)  # 2,550 kWh
    assert sum(s.energy_kwh for s in sessions) == pytest.approx(baseline.sum())
    assert baseline.max() <= 11 * 0.25 + 1e-12


def test_ev_baseline_charges_from_plug_in_and_only_inside_sessions(ctx):
    sessions, baseline = build_ev(ev_cfg(), ctx)
    inside = np.zeros(ctx.n, dtype=bool)
    for s in sessions:
        inside[s.start_idx : s.end_idx] = True
        assert baseline[s.start_idx] > 0  # dumb charging starts at plug-in
        assert baseline[s.start_idx : s.end_idx].sum() == pytest.approx(s.energy_kwh)
    assert baseline[~inside].sum() == 0


def test_ev_window_lengths_on_dst_nights(ctx):
    sessions, _ = build_ev(ev_cfg(), ctx)
    by_start = {local_str(ctx, s.start_idx)[:10]: s.end_idx - s.start_idx for s in sessions}
    assert by_start["2025-10-25"] == 56  # night with the extra hour: 14 h
    assert by_start["2026-03-28"] == 48  # night with the missing hour: 12 h
    assert by_start["2025-11-10"] == 52  # normal night: 13 h


def test_ev_weekend_window_is_configurable(ctx):
    sessions, _ = build_ev(ev_cfg(plug_in_weekend="20:00", plug_out_weekend="09:00"), ctx)
    by_start = {local_str(ctx, s.start_idx): s for s in sessions}
    saturday = by_start["2025-10-04 20:00"]  # Saturday evening uses the weekend window
    assert local_str(ctx, saturday.end_idx) == "2025-10-05 09:00"
    assert "2025-10-06 18:00" in by_start  # Monday evening uses the weekday window


def test_ev_window_too_short_is_capped_and_logged(ctx, caplog):
    cfg = ev_cfg(km_per_year=100000, wallbox_kw=3)  # 46.6 kWh/day, but 3 kW x 13 h = 39 kWh
    with caplog.at_level(logging.WARNING, logger="ets.profiles.archetypes"):
        sessions, baseline = build_ev(cfg, ctx)
    assert "capped" in caplog.text
    assert sessions[10].energy_kwh == pytest.approx(
        3 * 0.25 * (sessions[10].end_idx - sessions[10].start_idx)
    )
    assert baseline.sum() == pytest.approx(sum(s.energy_kwh for s in sessions))


# ------------------------------------------------------------------ heat pump


def test_heat_pump_energy_balance(ctx):
    heat, cop, el = build_heat_pump(hp_cfg(), ctx)
    assert heat.sum() == pytest.approx(15000.0)
    assert el.sum() == pytest.approx((heat / cop).sum())
    assert 2.5 < heat.sum() / el.sum() < 3.5  # between the synthetic COPs (2.5 winter, 3.5 summer)
    assert cop.min() >= 2.5 - 1e-9 and cop.max() <= 3.5 + 1e-9
    assert el.max() <= 6 * 0.25


def test_heat_pump_space_only_follows_the_space_shape(ctx):
    heat, _, _ = build_heat_pump(hp_cfg(dhw_share=0.0), ctx)
    assert np.allclose(heat, 15000 * ctx.heat_space_norm)


def test_heat_pump_winter_dominates(ctx):
    _, _, el = build_heat_pump(hp_cfg(), ctx)
    winter = el[np.isin(ctx.month, [12, 1, 2])].sum() / el.sum()
    summer = el[np.isin(ctx.month, [6, 7, 8])].sum() / el.sum()
    assert winter > 0.4 > 0.15 > summer


def test_heat_pump_power_cap_carries_unserved_heat(ctx, caplog):
    with caplog.at_level(logging.WARNING, logger="ets.profiles.archetypes"):
        _, _, el = build_heat_pump(hp_cfg(p_max_kw=0.5), ctx)  # far below the winter demand
    assert el.max() == pytest.approx(0.5 * 0.25)
    assert "carried" in caplog.text


# ------------------------------------------------------------------ PV


def test_pv_total_is_kwp_times_yield_and_zero_at_night(ctx):
    pv = build_pv(PVCfg(kwp=8, specific_yield_kwh_per_kwp=950, feed_in_ct_kwh=7.78), ctx)
    assert pv.sum() == pytest.approx(7600.0)
    night = (ctx.qh_of_day >= 88) | (ctx.qh_of_day < 16)  # 22:00-04:00
    assert pv[night].sum() == 0
    assert pv.max() * 4 < 8  # peak power below the installed kWp


def test_pv_summer_beats_winter(ctx):
    pv = build_pv(PVCfg(kwp=8, specific_yield_kwh_per_kwp=950, feed_in_ct_kwh=7.78), ctx)
    assert pv[ctx.month == 7].sum() > 2 * pv[ctx.month == 12].sum()


def test_pv_sites_are_normalised_before_averaging(ctx, tmp_path):
    """Synthetic r4 is 3x larger than r1 and peaks 2 h later; each must count 50%."""
    from ets.profiles.calendar import align_to_year
    from ets.simulate.context import _opsd_pv_days

    days = _opsd_pv_days(write_pv_parquet(tmp_path / "pv.parquet"))
    a = align_to_year(days["r1"], ctx, match_daytype=False)
    b = align_to_year(days["r4"], ctx, match_daytype=False)
    expected = (a / a.sum() + b / b.sum()) / 2
    assert ctx.pv_shape == pytest.approx(expected / expected.sum())
    for single in (a, b):  # neither site alone: the 2 h offset must show in the average
        assert not np.allclose(ctx.pv_shape, single, rtol=1e-3, atol=0)


def test_pv_needs_a_shape():
    con = make_gold_con()
    bare = load_gold_context(con, opsd_parquet=None)
    with pytest.raises(ValueError, match="no PV shape"):
        build_pv(PVCfg(kwp=8, specific_yield_kwh_per_kwp=950, feed_in_ct_kwh=7.78), bare)


def test_dst_days_in_the_source_are_not_used(tmp_path):
    """Source days with 92 or 100 quarter-hours are excluded, so every copied day has 96 values."""
    con = make_gold_con()
    path = write_pv_parquet(tmp_path / "pv.parquet")
    from ets.simulate.context import _opsd_pv_days

    days = _opsd_pv_days(path)
    assert date(2016, 3, 27) not in days["r1"] and date(2016, 10, 30) not in days["r1"]
    assert date(2016, 3, 26) in days["r1"]
    assert all(len(v) == 96 for v in days["r1"].values())
    assert load_gold_context(con, path).pv_shape.sum() == pytest.approx(1.0)


# ------------------------------------------------------------------ household


def test_household_without_devices_has_zero_device_arrays(ctx):
    hh = build_household(HouseholdCfg(id="A", annual_kwh=3500, smart_meter_fee_eur_year=30), ctx)
    assert hh.ev_sessions == [] and hh.ev_baseline_kwh.sum() == 0
    assert hh.hp_heat_kwh.sum() == 0 and (hh.hp_cop == 1).all() and hh.hp_p_max_kw == 0
    assert hh.pv_kwh.sum() == 0
    assert all(
        len(a) == ctx.n for a in (hh.base_kwh, hh.ev_baseline_kwh, hh.hp_baseline_kwh, hh.pv_kwh)
    )


def test_config_households_have_totals_that_match_the_yaml(ctx):
    cfg = load_config()
    totals = {hh.id: household_totals(hh) for hh in build_households(cfg, ctx)}
    for t in totals.values():
        assert t["base_kwh"] == pytest.approx(3500.0)
    assert totals["A_standard"]["ev_kwh"] == totals["A_standard"]["pv_kwh"] == 0
    assert totals["B_ev"]["ev_kwh"] == pytest.approx(2550.0) and totals["B_ev"]["hp_el_kwh"] == 0
    assert totals["C_heat_pump"]["hp_heat_kwh"] == pytest.approx(15000.0)
    assert totals["D_full"]["pv_kwh"] == pytest.approx(8 * 950)
    assert totals["D_full"]["ev_sessions"] == 365


def test_archetype_labels():
    assert archetype_label(False, False, False) == "base"
    assert archetype_label(True, True, True) == "ev+hp+pv"
    assert archetype_label(False, True, False) == "hp"


def test_write_dim_household_replaces_rows(ctx):
    cfg = load_config()
    built = build_households(cfg, ctx)
    con = duckdb.connect()
    write_dim_household(con, cfg, built)
    write_dim_household(con, cfg, built)  # second run must not duplicate or fail on the key
    rows = con.execute(
        "select household_id, archetype, annual_base_kwh, ev_kwh, pv_kwh from dim_household order by 1"
    ).fetchall()
    assert [r[0] for r in rows] == ["A_standard", "B_ev", "C_heat_pump", "D_full"]
    assert [r[1] for r in rows] == ["base", "ev", "hp", "ev+hp+pv"]
    assert rows[3][3:] == (pytest.approx(2550.0), pytest.approx(7600.0))


# ------------------------------------------------------------------ real data (skipped if absent)


@pytest.mark.skipif(
    not (GOLD_DB.exists() and OPSD_SILVER.exists()),
    reason="run `ets ingest` and `ets transform` first",
)
def test_real_gold_data_builds_the_four_households():
    con = duckdb.connect(str(GOLD_DB), read_only=True)
    try:
        real = load_gold_context(con)
    finally:
        con.close()
    hh = {h.id: household_totals(h) for h in build_households(load_config(), real)}
    assert hh["B_ev"]["ev_kwh"] == pytest.approx(2550.0)
    assert hh["D_full"]["pv_kwh"] == pytest.approx(7600.0)
    assert 2.5 < hh["C_heat_pump"]["hp_seasonal_cop"] < 3.5
    assert 4000 < hh["C_heat_pump"]["hp_el_kwh"] < 6500
    night = (real.qh_of_day >= 88) | (real.qh_of_day < 16)
    assert real.pv_shape[night].sum() == 0

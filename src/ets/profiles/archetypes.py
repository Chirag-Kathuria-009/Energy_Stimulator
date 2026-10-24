"""Turn config assumptions and gold profiles into per-quarter-hour arrays for each archetype.

Every array has the length of the simulation year (ctx.n). Amounts are kWh per quarter-hour.
"""

import logging
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import numpy as np

from ets.config import EVCfg, HeatPumpCfg, HouseholdCfg, PVCfg
from ets.domain import Household, Session
from ets.settings import QH_HOURS, TZ_LOCAL
from ets.simulate.context import GoldContext

log = logging.getLogger(__name__)
_TZ = ZoneInfo(TZ_LOCAL)
_BASE_TOLERANCE = 0.01  # base load must hit the annual kWh within 1%
_HP_ACCUMULATION_WARN_QH = 4  # warn if unserved heat is carried for more than one hour


def _hhmm(text: str) -> time:
    hour, minute = text.split(":")
    return time(int(hour), int(minute))


def build_base_load(annual_kwh: float, ctx: GoldContext, profile_id: str = "H25") -> np.ndarray:
    """Inflexible load: the BDEW profile (kWh per 1,000 kWh/year) scaled to the household."""
    base = ctx.profiles[profile_id] * annual_kwh / 1000.0
    if abs(base.sum() / annual_kwh - 1) > _BASE_TOLERANCE:
        raise ValueError(f"base load sums to {base.sum():.1f} kWh, expected {annual_kwh}")
    return base


def build_ev(cfg: EVCfg, ctx: GoldContext) -> tuple[list[Session], np.ndarray]:
    """One plug-in session per local date and the unoptimised ("dumb") charging array.

    ASSUMPTIONS (DECISIONS.md): the same energy every day (km_per_year x kWh/100 km / 365); the
    car is plugged in at `plug_in` and unplugged at `plug_out` of the next morning; weekends use
    the same window unless plug_in_weekend / plug_out_weekend are set. The last evening of the
    year is cut off at the end of the simulation year. A window too short for the daily energy
    is capped at what the wallbox can deliver and logged.
    """
    daily_kwh = cfg.km_per_year * cfg.kwh_per_100km / 100.0 / 365.0
    step_kwh = cfg.wallbox_kw * QH_HOURS
    sessions: list[Session] = []
    baseline = np.zeros(ctx.n)
    capped_days = 0
    for d in ctx.dates():
        weekend = d.isoweekday() >= 6
        t_in = _hhmm(
            cfg.plug_in_weekend if weekend and cfg.plug_in_weekend else cfg.plug_in_weekday
        )
        t_out = _hhmm(
            cfg.plug_out_weekend if weekend and cfg.plug_out_weekend else cfg.plug_out_weekday
        )
        start = ctx.idx_of_local(datetime.combine(d, t_in, tzinfo=_TZ))
        out_day = d + timedelta(days=1) if t_out <= t_in else d
        end = min(ctx.idx_of_local(datetime.combine(out_day, t_out, tzinfo=_TZ)), ctx.n)
        if start >= ctx.n or end <= start:
            continue
        energy = min(daily_kwh, step_kwh * (end - start))
        if energy < daily_kwh:
            capped_days += 1
        sessions.append(Session(start, end, energy, cfg.wallbox_kw))
        remaining = energy
        for slot in range(start, end):
            if remaining <= 0:
                break
            charged = min(step_kwh, remaining)
            baseline[slot] += charged
            remaining -= charged
    if capped_days:
        log.warning(
            "EV: window too short for the daily energy on %d day(s); energy capped", capped_days
        )
    return sessions, baseline


def build_heat_pump(
    cfg: HeatPumpCfg, ctx: GoldContext
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Heat demand, effective COP and unoptimised electricity per quarter-hour.

    Heat is a mix of the space-heating and hot-water shapes (`dhw_share`). Electricity per
    quarter-hour is space/COP_space + water/COP_water; the effective COP returned is
    heat / electricity, so later phases can use a single COP array. The electricity is capped
    at p_max; heat that cannot be served is carried to the next quarter-hour.
    """
    space = cfg.annual_heat_kwh * (1 - cfg.dhw_share) * ctx.heat_space_norm
    water = cfg.annual_heat_kwh * cfg.dhw_share * ctx.heat_water_norm
    heat = space + water
    wanted_el = space / ctx.cop_space + water / ctx.cop_water
    cop = heat / wanted_el
    cap_el = cfg.p_max_kw * QH_HOURS
    baseline = np.zeros(ctx.n)
    carry_el = 0.0  # electricity not yet delivered
    run = longest = 0
    for i in range(ctx.n):
        need = wanted_el[i] + carry_el
        baseline[i] = min(need, cap_el)
        carry_el = need - baseline[i]
        run = run + 1 if carry_el > 1e-12 else 0
        longest = max(longest, run)
    if longest > _HP_ACCUMULATION_WARN_QH:
        log.warning("heat pump: unserved heat carried for %d consecutive quarter-hours", longest)
    if carry_el > 1e-9:
        log.warning(
            "heat pump: %.2f kWh electricity still undelivered at the end of the year", carry_el
        )
    return heat, cop, baseline


def build_pv(cfg: PVCfg, ctx: GoldContext) -> np.ndarray:
    """PV generation: the OPSD-derived shape (sums to 1) x kWp x specific yield."""
    if ctx.pv_shape is None:
        raise ValueError("context has no PV shape; load it with an OPSD silver parquet")
    return ctx.pv_shape * cfg.kwp * cfg.specific_yield_kwh_per_kwp


def build_household(hh_cfg: HouseholdCfg, ctx: GoldContext) -> Household:
    """Combine the builders for the devices present; absent devices are all-zero arrays."""
    zeros = np.zeros(ctx.n)
    sessions: list[Session] = []
    ev_baseline = zeros
    if hh_cfg.ev is not None:
        sessions, ev_baseline = build_ev(hh_cfg.ev, ctx)
    heat, cop, hp_baseline = zeros, np.ones(ctx.n), zeros
    hp_p_max = hp_buffer = 0.0
    if hh_cfg.heat_pump is not None:
        heat, cop, hp_baseline = build_heat_pump(hh_cfg.heat_pump, ctx)
        hp_p_max, hp_buffer = hh_cfg.heat_pump.p_max_kw, hh_cfg.heat_pump.thermal_buffer_kwh
    pv = build_pv(hh_cfg.pv, ctx) if hh_cfg.pv is not None else zeros
    return Household(
        id=hh_cfg.id,
        base_kwh=build_base_load(hh_cfg.annual_kwh, ctx),
        ev_sessions=sessions,
        ev_baseline_kwh=ev_baseline,
        hp_heat_kwh=heat,
        hp_cop=cop,
        hp_baseline_kwh=hp_baseline,
        hp_p_max_kw=hp_p_max,
        hp_buffer_kwh=hp_buffer,
        pv_kwh=pv,
    )


def household_totals(hh: Household) -> dict[str, float]:
    """Annual kWh per component (written to dim_household)."""
    return {
        "base_kwh": float(hh.base_kwh.sum()),
        "ev_kwh": float(hh.ev_baseline_kwh.sum()),
        "ev_sessions": len(hh.ev_sessions),
        "hp_heat_kwh": float(hh.hp_heat_kwh.sum()),
        "hp_el_kwh": float(hh.hp_baseline_kwh.sum()),
        "hp_seasonal_cop": (
            float(hh.hp_heat_kwh.sum() / hh.hp_baseline_kwh.sum())
            if hh.hp_baseline_kwh.sum() > 0
            else 0.0
        ),
        "pv_kwh": float(hh.pv_kwh.sum()),
    }

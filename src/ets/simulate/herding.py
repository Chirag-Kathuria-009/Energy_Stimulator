"""Phase 6: what happens to the grid when many households react to the same price.

A synthetic population (seeded, all parameters in config/herding.yaml) is aggregated into one
load curve per scenario: unshifted, greedy (everybody shifts to the cheapest slots) and
greedy_spread (mitigation: EVs spread randomly over their k cheapest slots). Only the running
total is kept in memory, never 1,000 x 35,040 values.
"""

from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

import duckdb
import numpy as np
import polars as pl

from ets.config import AppConfig, EVCfg, HeatPumpCfg, HouseholdCfg
from ets.domain import Household, TariffVariant
from ets.optimize.greedy import shift_ev_greedy, shift_ev_spread, shift_hp_greedy
from ets.profiles.archetypes import build_household
from ets.settings import QH_HOURS, TZ_LOCAL
from ets.simulate.context import GoldContext, load_gold_context
from ets.tariffs.billing import VariantPrices, build_variants, variant_prices

SCENARIOS = {"unshifted": "none", "greedy": "greedy", "greedy_spread": "spread"}
NIGHT_START, NIGHT_END = time(18, 0), time(7, 0)  # the window in which the EVs are plugged in
_TZ = ZoneInfo(TZ_LOCAL)


def _shift_clock(hhmm: str, minutes: int) -> str:
    total = (int(hhmm[:2]) * 60 + int(hhmm[3:]) + minutes) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


def _templates(base_cfg: AppConfig) -> tuple[EVCfg | None, HeatPumpCfg | None]:
    ev = next((h.ev for h in base_cfg.households if h.ev is not None), None)
    hp = next((h.heat_pump for h in base_cfg.households if h.heat_pump is not None), None)
    return ev, hp


def sample_household_cfgs(n: int, base_cfg: AppConfig, seed: int) -> list[HouseholdCfg]:
    """A reproducible population. ASSUMPTIONS (config/herding.yaml): base load ~ normal and
    clipped, EV and heat pump present with fixed probabilities (copies of the first EV / heat
    pump in households.yaml), EV km ~ normal and clipped, plug-in time jittered by up to
    +/- plug_in_jitter_min minutes (rounded to quarter-hours). All random numbers are drawn up
    front, so a given seed gives the same population whatever the probabilities are.
    """
    h = base_cfg.herding
    ev_t, hp_t = _templates(base_cfg)
    if (h.p_ev > 0 and ev_t is None) or (h.p_hp > 0 and hp_t is None):
        raise ValueError("herding needs an EV and a heat pump in households.yaml to copy from")
    rng = np.random.default_rng(seed)
    kwh = np.clip(
        rng.normal(h.annual_kwh.mean, h.annual_kwh.sd, n), h.annual_kwh.min, h.annual_kwh.max
    )
    has_ev, has_hp = rng.random(n) < h.p_ev, rng.random(n) < h.p_hp
    km = np.clip(rng.normal(h.ev_km.mean, h.ev_km.sd, n), h.ev_km.min, h.ev_km.max)
    jitter = np.round(rng.integers(-h.plug_in_jitter_min, h.plug_in_jitter_min + 1, n) / 15) * 15
    cfgs = []
    for i in range(n):
        ev = None
        if has_ev[i]:
            ev = ev_t.model_copy(
                update={
                    "km_per_year": float(km[i]),
                    "plug_in_weekday": _shift_clock(ev_t.plug_in_weekday, int(jitter[i])),
                    "plug_in_weekend": None,
                    "plug_out_weekend": None,
                }
            )
        cfgs.append(
            HouseholdCfg(
                id=f"H{i + 1:04d}",
                annual_kwh=float(kwh[i]),
                smart_meter_fee_eur_year=50.0 if (has_ev[i] or has_hp[i]) else 30.0,
                ev=ev,
                heat_pump=hp_t.model_copy() if has_hp[i] else None,
            )
        )
    return cfgs


def _device(
    hh: Household,
    prices: VariantPrices,
    ctx: GoldContext,
    k: int,
    rng: np.random.Generator,
) -> dict[str, np.ndarray]:
    """Device electricity of one household under each method."""
    out = {"none": hh.ev_baseline_kwh + hh.hp_baseline_kwh}
    hp_shifted = np.zeros(ctx.n)
    if hh.hp_p_max_kw > 0:  # one heat-pump solve serves both shifted scenarios
        hp_shifted = shift_hp_greedy(
            prices.device_ct,
            hh.hp_heat_kwh,
            hh.hp_cop,
            hh.hp_p_max_kw,
            hh.hp_buffer_kwh,
            ctx.day_slices(),
        )
    ev_greedy, _ = shift_ev_greedy(prices.device_ct, hh.ev_sessions)
    ev_spread, _ = shift_ev_spread(prices.device_ct, hh.ev_sessions, k, rng)
    out["greedy"] = ev_greedy + hp_shifted
    out["spread"] = ev_spread + hp_shifted
    return out


def aggregate_scenarios(
    cfgs: list[HouseholdCfg],
    ctx: GoldContext,
    cfg: AppConfig,
    variant: TariffVariant,
    methods: tuple[str, ...] = ("none", "greedy", "spread"),
    seed: int = 0,
) -> dict[str, np.ndarray]:
    """Aggregate grid load in kW per quarter-hour for each method, in one pass over the
    households. Every household faces the same price, which is exactly what produces herding.
    Net load = base + device - PV (no household in the sample has PV)."""
    prices = variant_prices(variant, ctx, cfg)
    totals = {m: np.zeros(ctx.n) for m in methods}
    for i, h_cfg in enumerate(cfgs):
        hh = build_household(h_cfg, ctx)
        rng = np.random.default_rng([seed, i])  # independent of the order the methods are run in
        device = _device(hh, prices, ctx, cfg.herding.spread_k, rng)
        for m in methods:
            totals[m] += hh.base_kwh + device[m] - hh.pv_kwh
    return {m: t / QH_HOURS for m, t in totals.items()}


def aggregate(
    cfgs: list[HouseholdCfg],
    ctx: GoldContext,
    cfg: AppConfig,
    method: str,
    variant: TariffVariant,
    seed: int = 0,
) -> np.ndarray:
    """Aggregate load (kW) for one method: 'none', 'greedy' or 'spread'."""
    return aggregate_scenarios(cfgs, ctx, cfg, variant, (method,), seed)[method]


def _night_windows(ctx: GoldContext) -> list[tuple[int, int]]:
    """(start, end) positions of every evening-to-morning window that lies in the year."""
    windows = []
    dates = ctx.dates()
    for d, nxt in zip(dates[:-1], dates[1:], strict=True):
        a = ctx.idx_of_local(datetime.combine(d, NIGHT_START, tzinfo=_TZ))
        b = ctx.idx_of_local(datetime.combine(nxt, NIGHT_END, tzinfo=_TZ))
        windows.append((a, b))
    return windows


def _local_text(ctx: GoldContext, idx: int) -> str:
    t = ctx.ts_utc[idx].astype(datetime).replace(tzinfo=UTC).astimezone(_TZ)
    return t.strftime("%Y-%m-%d %H:%M")


def peak_report(curves: dict[str, np.ndarray], ctx: GoldContext) -> pl.DataFrame:
    """Per scenario: peak kW, local time of the peak, mean kW, peak/mean, total MWh and the share
    of nights in which the highest load of the night falls in the cheapest quarter-hour of that
    night (the signature of herding). Ties in price or load go to the earlier quarter-hour."""
    nights = _night_windows(ctx)
    cheapest = np.array([a + int(np.argmin(ctx.price_ct[a:b])) for a, b in nights])
    rows = []
    for name, kw in curves.items():
        peak = int(np.argmax(kw))
        at_cheapest = sum(
            a + int(np.argmax(kw[a:b])) == c for (a, b), c in zip(nights, cheapest, strict=True)
        )
        rows.append(
            {
                "scenario": name,
                "peak_kw": float(kw[peak]),
                "peak_ts_local": _local_text(ctx, peak),
                "mean_kw": float(kw.mean()),
                "peak_to_mean": float(kw[peak] / kw.mean()),
                "share_nights_peak_in_cheapest_qh": at_cheapest / len(nights),
                "total_mwh": float(kw.sum() * QH_HOURS / 1000),
            }
        )
    return pl.DataFrame(rows)


def write_herding(
    con: duckdb.DuckDBPyConnection, curves: dict[str, np.ndarray], summary: pl.DataFrame
) -> None:
    """Replace herding_curve and herding_summary in one transaction."""
    con.execute(
        "create table if not exists herding_curve (scenario varchar, idx integer, kw double)"
    )
    con.execute(
        "create table if not exists herding_summary (scenario varchar primary key, peak_kw double, "
        "peak_ts_local varchar, mean_kw double, peak_to_mean double, "
        "share_nights_peak_in_cheapest_qh double, total_mwh double)"
    )
    frames = [
        pl.DataFrame(
            {"scenario": [name] * len(kw), "idx": np.arange(len(kw), dtype=np.int32), "kw": kw}
        )
        for name, kw in curves.items()
    ]
    con.execute("begin")
    try:
        con.execute("delete from herding_curve")
        con.execute("delete from herding_summary")
        con.register("curve_df", pl.concat(frames).to_arrow())
        con.execute("insert into herding_curve select * from curve_df")
        con.unregister("curve_df")
        con.register("summary_df", summary.to_arrow())
        con.execute("insert into herding_summary select * from summary_df")
        con.unregister("summary_df")
        con.execute("commit")
    except BaseException:
        con.execute("rollback")
        raise


def run_herding(
    cfg: AppConfig,
    con: duckdb.DuckDBPyConnection,
    ctx: GoldContext | None = None,
    n: int | None = None,
) -> pl.DataFrame:
    """Sample the population, aggregate the three scenarios, write the tables, return the summary.

    The dynamic tariff without a §14a module is used: every household sees the same spot-based
    price, which is the herding case. Greedy shifting only (the LP adds nothing to this question).
    """
    ctx = ctx or load_gold_context(con)
    h = cfg.herding
    cfgs = sample_household_cfgs(n or h.n_households, cfg, h.seed)
    variant = next(v for v in build_variants(cfg) if v.id == "dynamic")
    curves = aggregate_scenarios(cfgs, ctx, cfg, variant, tuple(SCENARIOS.values()), seed=h.seed)
    curves = {name: curves[method] for name, method in SCENARIOS.items()}
    summary = peak_report(curves, ctx)
    write_herding(con, curves, summary)
    return summary

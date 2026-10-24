"""The stages of `ets simulate` that run inside one process.

Phase 3: build the households. Phase 4: bill them. Phase 5: shift the flexible load (greedy or
LP), run all scenarios and write the result tables.
"""

import hashlib
import json
import logging
import subprocess
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import duckdb
import numpy as np
import polars as pl

from ets.config import AppConfig, config_hash
from ets.domain import Household, SimResult, TariffVariant
from ets.optimize.greedy import shift_ev_greedy, shift_hp_greedy
from ets.optimize.lp import shift_ev_lp, shift_hp_lp
from ets.profiles.archetypes import build_household, household_totals
from ets.settings import ROOT
from ets.simulate.context import GoldContext, load_gold_context
from ets.tariffs.billing import (
    VariantPrices,
    bill,
    build_variants,
    grid_flows,
    has_device,
    variant_prices,
    variants_for,
)

log = logging.getLogger(__name__)

METHODS = ("none", "greedy", "lp")
SPIKE_WINDOW_QH = 7 * 96  # the "price spike week" is the 7-day window with the highest mean price


# ------------------------------------------------------------------ Phase 3


def build_households(cfg: AppConfig, ctx: GoldContext) -> list[Household]:
    return [build_household(h, ctx) for h in cfg.households]


def archetype_label(has_ev: bool, has_hp: bool, has_pv: bool) -> str:
    parts = [name for name, present in (("ev", has_ev), ("hp", has_hp), ("pv", has_pv)) if present]
    return "+".join(parts) or "base"


def write_dim_household(
    con: duckdb.DuckDBPyConnection, cfg: AppConfig, households: list[Household]
) -> None:
    """Replace `dim_household`: one row per household with annual kWh per component."""
    rows = []
    for h_cfg, hh in zip(cfg.households, households, strict=True):
        totals = household_totals(hh)
        rows.append(
            (
                hh.id,
                archetype_label(
                    h_cfg.ev is not None, h_cfg.heat_pump is not None, h_cfg.pv is not None
                ),
                totals["base_kwh"],
                totals["ev_kwh"],
                totals["hp_el_kwh"],
                totals["pv_kwh"],
                json.dumps({"config": h_cfg.model_dump(mode="json"), "totals": totals}),
            )
        )
    con.execute("drop table if exists dim_household")
    con.execute(
        "create table dim_household (household_id varchar primary key, archetype varchar, "
        "annual_base_kwh double, ev_kwh double, hp_kwh double, pv_kwh double, params_json varchar)"
    )
    con.executemany("insert into dim_household values (?, ?, ?, ?, ?, ?, ?)", rows)


# ------------------------------------------------------------------ Phase 5: shifting


def _digest(array: np.ndarray) -> str:
    return hashlib.blake2b(np.ascontiguousarray(array).tobytes(), digest_size=16).hexdigest()


def apply_shifting(
    hh: Household,
    prices: VariantPrices,
    method: str,
    ctx: GoldContext,
    cache: dict | None = None,
) -> tuple[np.ndarray, float]:
    """Device electricity per quarter-hour after shifting, and the EV energy that did not fit.

    `none` is the unshifted baseline. `greedy` and `lp` optimise the EV and the heat pump on the
    device price, so a Module 2/3 grid fee steers the shifting. Optimiser results are cached by
    (household, device, method, price array): variants with the same device price (e.g. dynamic
    and dynamic_m1) share one solve.
    """
    if method == "none":
        return hh.ev_baseline_kwh + hh.hp_baseline_kwh, 0.0
    if method not in ("greedy", "lp"):
        raise ValueError(f"unknown method {method!r}")
    cache = {} if cache is None else cache
    ev_shift, hp_shift = (
        (shift_ev_greedy, shift_hp_greedy) if method == "greedy" else (shift_ev_lp, shift_hp_lp)
    )
    price = prices.device_ct
    tag = (hh.id, method, _digest(price))
    ev, shortfall = np.zeros(ctx.n), 0.0
    if hh.ev_sessions:
        if (*tag, "ev") not in cache:
            cache[(*tag, "ev")] = ev_shift(price, hh.ev_sessions)
        ev, shortfall = cache[(*tag, "ev")]
    hp = np.zeros(ctx.n)
    if hh.hp_p_max_kw > 0:
        if (*tag, "hp") not in cache:
            cache[(*tag, "hp")] = hp_shift(
                price, hh.hp_heat_kwh, hh.hp_cop, hh.hp_p_max_kw, hh.hp_buffer_kwh, ctx.day_slices()
            )
        hp = cache[(*tag, "hp")]
    return ev + hp, shortfall


def simulate_one(
    hh: Household,
    variant: TariffVariant,
    method: str,
    ctx: GoldContext,
    cfg: AppConfig,
    cache: dict | None = None,
) -> SimResult:
    """Price the variant, shift the load with `method`, bill it."""
    prices = variant_prices(variant, ctx, cfg)
    device, shortfall = apply_shifting(hh, prices, method, ctx, cache)
    cost_ct, annual_eur = bill(hh, device, prices, variant, cfg)
    import_base, import_device, _ = grid_flows(hh.base_kwh, device, hh.pv_kwh)
    baseline = hh.ev_baseline_kwh + hh.hp_baseline_kwh
    return SimResult(
        household_id=hh.id,
        variant_id=variant.id,
        method=method,
        grid_import_kwh=import_base + import_device,
        cost_ct=cost_ct,
        annual_eur=annual_eur,
        shifted_kwh=float(np.abs(device - baseline).sum() / 2),
        shortfall_kwh=shortfall,
        device_kwh=device,
    )


def scenarios(
    households: list[Household], variants: list[TariffVariant], methods: Sequence[str]
) -> list[tuple[Household, TariffVariant, str]]:
    """Every (household, variant, method) worth running: shifting makes no sense for the fixed
    tariff (flat price) or for a household without a flexible device."""
    plan = []
    for hh in households:
        for v in variants_for(hh, variants):
            for m in methods:
                if m != "none" and (v.kind == "fixed" or not has_device(hh)):
                    continue
                plan.append((hh, v, m))
    return plan


def run_baseline(cfg: AppConfig, ctx: GoldContext, households: list[Household]) -> list[SimResult]:
    """Unshifted results for every household x eligible tariff variant (Phase 4)."""
    return [
        simulate_one(hh, v, "none", ctx, cfg)
        for hh, v, _ in scenarios(households, build_variants(cfg), ("none",))
    ]


def saving_vs_fixed_pct(
    results: list[SimResult], household_id: str, variant_id: str, method: str = "none"
) -> float:
    """(cost_fixed - cost_variant) / cost_fixed in percent for one household and method."""
    cost = {
        (r.variant_id, r.method): r.annual_eur for r in results if r.household_id == household_id
    }
    fixed = cost[("fixed", "none")]
    return (fixed - cost[(variant_id, method)]) / fixed * 100.0


# ------------------------------------------------------------------ Phase 5: result tables

_DDL = (
    "create table if not exists sim_runs (run_id varchar primary key, started_at timestamp, "
    "finished_at timestamp, config_hash varchar, git_sha varchar, status varchar, error varchar)",
    "create table if not exists sim_results_qh (run_id varchar, household_id varchar, "
    "variant_id varchar, method varchar, idx integer, grid_import_kwh double, cost_ct double, "
    "device_kwh double)",
    "create table if not exists sim_summary (run_id varchar, household_id varchar, "
    "variant_id varchar, method varchar, annual_eur double, saving_vs_fixed_eur double, "
    "saving_tariff_switch_eur double, saving_timing_eur double, saving_gridfee_eur double, "
    "shifted_kwh double, shortfall_kwh double, worst_month_eur double, spike_week_eur double, "
    "primary key (run_id, household_id, variant_id, method))",
    "create or replace view v_latest_summary as select * from sim_summary where run_id = ("
    "select run_id from sim_runs where status = 'ok' order by finished_at desc limit 1)",
)


def ensure_tables(con: duckdb.DuckDBPyConnection) -> None:
    for statement in _DDL:
        con.execute(statement)


def git_sha() -> str:
    """Current commit, with '+dirty' if the working tree has changes; 'unknown' without git."""
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT, capture_output=True, text=True, check=True,
        ).stdout.strip()  # fmt: skip
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return f"{sha}+dirty" if dirty else sha


def start_run(con: duckdb.DuckDBPyConnection, cfg: AppConfig) -> str:
    ensure_tables(con)
    run_id = str(uuid.uuid4())
    con.execute(
        "insert into sim_runs values (?, ?, null, ?, ?, 'running', null)",
        [run_id, datetime.now(UTC).replace(tzinfo=None), config_hash(cfg), git_sha()],
    )
    return run_id


def finish_run(
    con: duckdb.DuckDBPyConnection, run_id: str, status: str, error: str | None = None
) -> None:
    con.execute(
        "update sim_runs set finished_at = ?, status = ?, error = ? where run_id = ?",
        [datetime.now(UTC).replace(tzinfo=None), status, error, run_id],
    )


def write_results(
    con: duckdb.DuckDBPyConnection, run_id: str, results: list[SimResult], n: int
) -> None:
    """Replace this run's rows in sim_results_qh (idempotent per run_id)."""
    con.execute("delete from sim_results_qh where run_id = ?", [run_id])
    frames = [
        pl.DataFrame(
            {
                "run_id": [run_id] * n,
                "household_id": [r.household_id] * n,
                "variant_id": [r.variant_id] * n,
                "method": [r.method] * n,
                "idx": np.arange(n, dtype=np.int32),
                "grid_import_kwh": r.grid_import_kwh,
                "cost_ct": r.cost_ct,
                "device_kwh": r.device_kwh if r.device_kwh is not None else np.zeros(n),
            }
        )
        for r in results
    ]
    con.register("results_df", pl.concat(frames).to_arrow())
    try:
        con.execute("insert into sim_results_qh select * from results_df")
    finally:
        con.unregister("results_df")


def spike_window_start(price_ct: np.ndarray) -> int:
    """Start of the 7-day window with the highest mean spot price."""
    sums = np.convolve(price_ct, np.ones(SPIKE_WINDOW_QH), mode="valid")
    return int(np.argmax(sums))


def summary_rows(run_id: str, results: list[SimResult], ctx: GoldContext) -> list[tuple]:
    """One row per result with the savings split (the three parts add up to the saving vs fixed):

    tariff switch = fixed - dynamic (unshifted); timing = dynamic (unshifted) - dynamic (this
    method); grid fee = dynamic (this method) - this variant (this method). The grid-fee part
    also holds the extra timing gain from module-specific prices, which cannot be separated.
    """
    by_key = {(r.household_id, r.variant_id, r.method): r for r in results}
    start = spike_window_start(ctx.price_ct)
    rows = []
    for r in results:
        fixed = by_key[(r.household_id, "fixed", "none")].annual_eur
        dyn_none = by_key.get((r.household_id, "dynamic", "none"))
        dyn_method = by_key.get((r.household_id, "dynamic", r.method))
        if r.variant_id == "fixed":
            switch = timing = gridfee = 0.0
        else:
            switch = fixed - dyn_none.annual_eur
            timing = dyn_none.annual_eur - dyn_method.annual_eur
            gridfee = dyn_method.annual_eur - r.annual_eur
        energy_eur = r.cost_ct.sum() / 100.0
        fixed_part = r.annual_eur - energy_eur  # base fee, meter share, module credit
        month_eur = [
            r.cost_ct[ctx.month == m].sum() / 100.0 + fixed_part / 12 for m in range(1, 13)
        ]
        spike_eur = r.cost_ct[start : start + SPIKE_WINDOW_QH].sum() / 100.0 + fixed_part * 7 / 365
        rows.append(
            (
                run_id, r.household_id, r.variant_id, r.method, r.annual_eur, fixed - r.annual_eur,
                switch, timing, gridfee, r.shifted_kwh, r.shortfall_kwh, max(month_eur), spike_eur,
            )
        )  # fmt: skip
    return rows


def write_summary(con: duckdb.DuckDBPyConnection, run_id: str, rows: list[tuple]) -> None:
    con.execute("delete from sim_summary where run_id = ?", [run_id])
    con.executemany("insert into sim_summary values (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)


def prune_old_runs(con: duckdb.DuckDBPyConnection, keep: int = 2) -> None:
    """Drop the per-quarter-hour rows of all but the newest `keep` runs (about 30 MB each).
    Summaries and run records are kept."""
    con.execute(
        "delete from sim_results_qh where run_id not in ("
        "select run_id from sim_runs order by started_at desc limit ?)",
        [keep],
    )


def run_scenarios(
    cfg: AppConfig,
    con: duckdb.DuckDBPyConnection,
    methods: Sequence[str] = METHODS,
    ctx: GoldContext | None = None,
    households: list[Household] | None = None,
) -> str:
    """Run every scenario and store the results under a new run_id. Returns the run_id.

    Results and summary are written in one transaction; a failure marks the run `failed`,
    leaves the previous results untouched and re-raises.
    """
    run_id = start_run(con, cfg)
    try:
        ctx = ctx or load_gold_context(con)
        households = households or build_households(cfg, ctx)
        cache: dict = {}
        results = [
            simulate_one(hh, v, m, ctx, cfg, cache)
            for hh, v, m in scenarios(households, build_variants(cfg), methods)
        ]
        con.execute("begin")
        try:
            write_results(con, run_id, results, ctx.n)
            write_summary(con, run_id, summary_rows(run_id, results, ctx))
            con.execute("commit")
        except BaseException:
            con.execute("rollback")
            raise
        finish_run(con, run_id, "ok")
        prune_old_runs(con)
    except BaseException as e:
        finish_run(con, run_id, "failed", str(e))
        raise
    return run_id

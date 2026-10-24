"""The household / billing stages of `ets simulate`.

Phase 3: build the households. Phase 4: bill them unshifted. Phase 5 adds shifting and the
scenario runner here.
"""

import json

import duckdb

from ets.config import AppConfig
from ets.domain import Household, SimResult, TariffVariant
from ets.profiles.archetypes import build_household, household_totals
from ets.simulate.context import GoldContext
from ets.tariffs.billing import (
    bill,
    build_variants,
    grid_flows,
    variant_prices,
    variants_for,
)


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


def simulate_unshifted(
    hh: Household, variant: TariffVariant, ctx: GoldContext, cfg: AppConfig
) -> SimResult:
    """Bill the household as it is (EV charges on arrival, heat pump follows demand)."""
    device = hh.ev_baseline_kwh + hh.hp_baseline_kwh
    prices = variant_prices(variant, ctx, cfg)
    cost_ct, annual_eur = bill(hh, device, prices, variant, cfg)
    import_base, import_device, _ = grid_flows(hh.base_kwh, device, hh.pv_kwh)
    return SimResult(
        household_id=hh.id,
        variant_id=variant.id,
        method="none",
        grid_import_kwh=import_base + import_device,
        cost_ct=cost_ct,
        annual_eur=annual_eur,
        shifted_kwh=0.0,
        shortfall_kwh=0.0,
    )


def run_baseline(cfg: AppConfig, ctx: GoldContext, households: list[Household]) -> list[SimResult]:
    """Unshifted results for every household x eligible tariff variant."""
    variants = build_variants(cfg)
    return [
        simulate_unshifted(hh, v, ctx, cfg) for hh in households for v in variants_for(hh, variants)
    ]


def saving_vs_fixed_pct(results: list[SimResult], household_id: str, variant_id: str) -> float:
    """(cost_fixed - cost_variant) / cost_fixed in percent, for one household."""
    cost = {r.variant_id: r.annual_eur for r in results if r.household_id == household_id}
    return (cost["fixed"] - cost[variant_id]) / cost["fixed"] * 100.0

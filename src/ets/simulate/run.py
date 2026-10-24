"""Phase 3 stage of `ets simulate`: build the households and record their totals.

Phases 4 and 5 (billing, shifting, scenario runner) extend this module.
"""

import json

import duckdb

from ets.config import AppConfig
from ets.domain import Household
from ets.profiles.archetypes import build_household, household_totals
from ets.simulate.context import GoldContext


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

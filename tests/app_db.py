"""A complete small result database (every table the app reads) built from the synthetic gold
fixture, written to a file the way `ets simulate` and `ets herding` write the real one."""

from pathlib import Path

from tests.gold_fixture import make_gold_con, write_pv_parquet

from ets.config import load_config
from ets.simulate.context import load_gold_context
from ets.simulate.herding import run_herding
from ets.simulate.run import _DDL, run_scenarios

TABLES = (
    "dim_time",
    "fact_price_qh",
    "fact_heat_qh",
    "fact_load_profile_qh",
    "sim_runs",
    "sim_results_qh",
    "sim_summary",
    "herding_curve",
    "herding_summary",
)


def make_app_db(tmp_path: Path) -> Path:
    cfg = load_config()
    con = make_gold_con()
    con.execute(
        "update fact_price_qh set price_ct_kwh = case when t.qh_of_day < 24 then 2.0 "
        "when t.qh_of_day between 68 and 83 then 25.0 else 8.0 end "
        "from dim_time t where fact_price_qh.idx = t.idx"
    )
    ctx = load_gold_context(con, write_pv_parquet(tmp_path / "pv.parquet"))
    run_scenarios(cfg, con, methods=("none", "greedy"), ctx=ctx)
    small = cfg.model_copy(update={"herding": cfg.herding.model_copy(update={"n_households": 25})})
    run_herding(small, con, ctx=ctx)

    db = tmp_path / "energy.duckdb"
    con.execute(f"attach '{db.as_posix()}' as out")
    for table in TABLES:
        con.execute(f"create table out.{table} as select * from main.{table}")
    con.execute("use out")
    con.execute(_DDL[-1])  # the v_latest_summary view, created inside the target database
    con.execute("use memory")
    con.execute("detach out")
    return db

"""A synthetic year with a daily price pattern, so shifting and herding have something to react to:
2 ct from 00:00 to 06:00 (a flat block, so ties are broken by the earlier slot), 25 ct in the
evening 17:00-21:00, 8 ct otherwise."""

from pathlib import Path

from tests.gold_fixture import make_gold_con, write_pv_parquet

from ets.simulate.context import GoldContext, load_gold_context


def make_priced_ctx(tmp_path: Path) -> GoldContext:
    con = make_gold_con()
    con.execute(
        "update fact_price_qh set price_ct_kwh = case when t.qh_of_day < 24 then 2.0 "
        "when t.qh_of_day between 68 and 83 then 25.0 else 8.0 end "
        "from dim_time t where fact_price_qh.idx = t.idx"
    )
    return load_gold_context(con, write_pv_parquet(tmp_path / "pv.parquet"))

"""A synthetic but structurally real gold database and OPSD PV parquet for Phase 3 tests."""

from datetime import date
from pathlib import Path

import duckdb
from tests.sql_helpers import render_model
from tests.test_dst import build_dim_time

from ets.simulate.context import GoldContext, load_gold_context

SIM = ("2025-09-30 22:00:00", "2026-09-30 22:00:00")
HOLIDAYS = [date(2025, 10, 3), date(2025, 12, 25), date(2025, 12, 26), date(2026, 1, 1)]


def make_gold_con() -> duckdb.DuckDBPyConnection:
    """dim_time and fact_load_profile_qh come from the real models; price and heat are synthetic."""
    con = build_dim_time(*SIM, HOLIDAYS)
    con.execute(
        """
        create table silver_bdew_profiles_qh as
        select p as profile_id, m as month, d as daytype, q as qh_of_day,
               case d when 'WT' then 1000.0 when 'SA' then 2000.0 else 3000.0 end as kwh_per_million
        from (values ('H25'), ('P25')) a(p), range(1, 13) b(m),
             (values ('WT'), ('SA'), ('FT')) c(d), range(0, 96) e(q)
        """
    )
    con.execute(
        f"create table fact_load_profile_qh as {render_model('gold/fact_load_profile_qh.sql')}"
    )
    con.execute(
        "create table fact_price_qh as select idx, ts_utc, 10.0 as price_ct_kwh, 100.0 as price_eur_mwh, "
        "'qh' as source_resolution, false as is_estimated from dim_time"
    )
    con.execute(
        """
        create table fact_heat_qh as
        with w as (
            select idx, ts_utc, month,
                   case when month in (12, 1, 2) then 3.0 when month in (11, 3) then 2.0
                        when month in (10, 4) then 1.0 else 0.2 end as weight
            from dim_time)
        select idx, ts_utc,
               weight / sum(weight) over () as heat_space_norm,
               1.0 / count(*) over () as heat_water_norm,
               case when month in (12, 1, 2) then 2.5 else 3.5 end as cop_space,
               3.0 as cop_water
        from w
        """
    )
    return con


def write_pv_parquet(path: Path) -> Path:
    """Two sites, calendar year 2016 in UTC (so it contains real 92- and 100-quarter-hour DST days).

    Site r4 is three times the size of r1 and peaks two hours later: after per-site
    normalisation the two must get equal weight in the averaged shape.
    """
    con = duckdb.connect()
    con.execute(
        """
        create table pv as
        select ts_utc, site, 'pv' as channel,
               scale * greatest(0.0, sin(pi() * (qh - 24 - shift) / 48.0))
                     * (0.3 + 0.7 * sin(pi() * doy / 365.0)) as kwh,
               false as is_estimated, date_local
        from (
            select ts_utc, l::date as date_local, hour(l) * 4 + minute(l) // 15 as qh,
                   dayofyear(l::date) as doy
            from (select ts_utc, (ts_utc at time zone 'UTC') at time zone 'Europe/Berlin' as l
                  from (select unnest(generate_series(timestamp '2015-12-31 23:00:00',
                                       timestamp '2016-12-31 22:45:00', interval 15 minute)) as ts_utc))
        ) t
        cross join (values ('r1', 1.0, 0), ('r4', 3.0, 8)) s(site, scale, shift)
        """
    )
    con.execute(f"copy pv to '{path.as_posix()}' (format parquet)")
    return path


def make_ctx(tmp_path: Path) -> GoldContext:
    con = make_gold_con()
    return load_gold_context(con, write_pv_parquet(tmp_path / "opsd_pv.parquet"))

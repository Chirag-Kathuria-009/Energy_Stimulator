from datetime import date

import duckdb
import pytest
from tests.sql_helpers import render_model


def build_dim_time(
    sim_start_utc: str, sim_end_utc: str, holiday_dates=()
) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("create table holidays_de(date date, name varchar)")
    for d in holiday_dates:
        con.execute("insert into holidays_de values (?, 'x')", [d])
    sql = render_model(
        "gold/dim_time.sql", {"sim_start_utc": sim_start_utc, "sim_end_utc": sim_end_utc}
    )
    con.execute(f"create table dim_time as {sql}")
    return con


def n_rows(con, d: date) -> int:
    return con.execute("select count(*) from dim_time where date_local = ?", [d]).fetchone()[0]


@pytest.fixture(scope="module")
def sim_year():
    # 2025-10-01 .. 2026-09-30 local = [2025-09-30 22:00Z, 2026-09-30 22:00Z)
    return build_dim_time(
        "2025-09-30 22:00:00", "2026-09-30 22:00:00", [date(2025, 10, 3), date(2025, 12, 25)]
    )


def test_simulation_year_has_35040_rows_and_contiguous_idx(sim_year):
    n, lo, hi = sim_year.execute("select count(*), min(idx), max(idx) from dim_time").fetchone()
    assert (n, lo, hi) == (35040, 0, 35039)
    assert sim_year.execute("select count(distinct ts_utc) from dim_time").fetchone()[0] == 35040


def test_dst_days_in_the_simulation_year(sim_year):
    assert n_rows(sim_year, date(2025, 10, 26)) == 100  # last Sunday of October 2025
    assert n_rows(sim_year, date(2026, 3, 29)) == 92  # last Sunday of March 2026
    assert n_rows(sim_year, date(2025, 11, 2)) == 96
    flagged = sim_year.execute(
        "select distinct date_local from dim_time where is_dst_day order by 1"
    ).fetchall()
    assert [r[0] for r in flagged] == [date(2025, 10, 26), date(2026, 3, 29)]


def test_repeated_october_hour_maps_to_qh_8_to_11_twice(sim_year):
    rows = sim_year.execute(
        "select qh_of_day, count(*) from dim_time where date_local = date '2025-10-26' "
        "and qh_of_day between 8 and 11 group by 1 order by 1"
    ).fetchall()
    assert rows == [(8, 2), (9, 2), (10, 2), (11, 2)]


def test_march_day_skips_the_02_00_hour(sim_year):
    skipped = sim_year.execute(
        "select count(*) from dim_time where date_local = date '2026-03-29' and qh_of_day between 8 and 11"
    ).fetchone()[0]
    assert skipped == 0


def test_every_local_day_has_92_96_or_100_rows(sim_year):
    bad = sim_year.execute(
        "select date_local from dim_time group by 1 having count(*) not in (92, 96, 100)"
    ).fetchall()
    assert bad == []


def test_calendar_attributes(sim_year):
    row = sim_year.execute(
        "select weekday, is_holiday, season, month from dim_time "
        "where date_local = date '2025-10-03' and qh_of_day = 0"
    ).fetchone()
    assert row == (5, True, "autumn", 10)  # Friday, holiday seed row
    assert sim_year.execute(
        "select is_holiday from dim_time where date_local = date '2025-10-04' limit 1"
    ).fetchone() == (False,)
    assert sim_year.execute(
        "select weekday from dim_time where date_local = date '2025-10-05' limit 1"
    ).fetchone() == (7,)  # Sunday


def test_leap_year_last_sundays_of_march_and_october():
    # 2024 local year: 2023-12-31 23:00Z .. 2024-12-31 23:00Z, leap year -> 35,136 rows
    con = build_dim_time("2023-12-31 23:00:00", "2024-12-31 23:00:00")
    assert con.execute("select count(*) from dim_time").fetchone()[0] == 366 * 96
    assert n_rows(con, date(2024, 3, 31)) == 92
    assert n_rows(con, date(2024, 10, 27)) == 100

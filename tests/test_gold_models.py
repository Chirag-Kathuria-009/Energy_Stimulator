"""Gold fact models and the heat silver model, on small hand-made inputs."""

from datetime import date

import duckdb
import pytest
from tests.sql_helpers import render_model
from tests.test_dst import build_dim_time

SIM = ("2025-09-30 22:00:00", "2026-09-30 22:00:00")
HOLIDAYS = [date(2025, 10, 3), date(2025, 12, 25), date(2025, 12, 26)]


def dynamisation(doy: int) -> float:
    t = float(doy)
    return round(-3.92e-10 * t**4 + 3.2e-7 * t**3 - 7.02e-5 * t**2 + 2.1e-3 * t + 1.24, 4)


# ---------------------------------------------------------------- fact_load_profile_qh


@pytest.fixture(scope="module")
def load():
    con = build_dim_time(*SIM, HOLIDAYS)
    # Constant per day type so the test can see which day type a date was given: WT=1, SA=2, FT=3
    con.execute(
        """
        create table silver_bdew_profiles_qh as
        select p as profile_id, m as month, d as daytype, q as qh_of_day,
               case d when 'WT' then 1000.0 when 'SA' then 2000.0 else 3000.0 end as kwh_per_million
        from (values ('H25'), ('P25')) a(p), range(1, 13) b(m), (values ('WT'), ('SA'), ('FT')) c(d),
             range(0, 96) e(q)
        """
    )
    con.execute(f"create table fact as {render_model('gold/fact_load_profile_qh.sql')}")
    return con


def kwh(con, d: str, qh: int, profile="H25", nth=0) -> float:
    rows = con.execute(
        "select l.kwh from fact l join dim_time t using (idx) where t.date_local = ? and "
        "t.qh_of_day = ? and l.profile_id = ? order by t.idx",
        [d, qh, profile],
    ).fetchall()
    return rows[nth][0]


def test_each_profile_has_n_rows_and_sums_to_1000(load):
    for profile, n, total in load.execute(
        "select profile_id, count(*), sum(kwh) from fact group by 1 order by 1"
    ).fetchall():
        assert n == 35040 and total == pytest.approx(1000.0, abs=1e-6), profile


def test_day_types_saturday_holiday_and_christmas_eve(load):
    # Same day type on two dates -> ratio of the values is the ratio of the dynamisation factors.
    sat_ratio = kwh(load, "2025-12-24", 0) / kwh(load, "2025-12-20", 0)  # Wed 24 Dec vs Sat 20 Dec
    assert sat_ratio == pytest.approx(dynamisation(358) / dynamisation(354), rel=2e-3)  # both SA
    hol_ratio = kwh(load, "2025-10-03", 0) / kwh(load, "2025-10-05", 0)  # Fri holiday vs Sunday
    assert hol_ratio == pytest.approx(dynamisation(276) / dynamisation(278), rel=2e-3)  # both FT
    # A plain weekday is WT = half a Saturday: Wed 17 Dec vs Sat 20 Dec.
    wd_ratio = kwh(load, "2025-12-20", 0) / kwh(load, "2025-12-17", 0)
    assert wd_ratio == pytest.approx(2 * dynamisation(354) / dynamisation(351), rel=2e-3)


def test_repeated_october_hour_is_split_in_half(load):
    single = kwh(load, "2025-10-26", 4)  # 01:00 local, occurs once
    first, second = kwh(load, "2025-10-26", 8, nth=0), kwh(load, "2025-10-26", 8, nth=1)
    assert first == pytest.approx(single / 2) and second == pytest.approx(single / 2)


def test_dynamisation_raises_winter_above_summer(load):
    winter = load.execute(
        "select avg(kwh) from fact l join dim_time t using (idx) where t.month = 12 and t.weekday < 6"
    ).fetchone()[0]
    summer = load.execute(
        "select avg(kwh) from fact l join dim_time t using (idx) where t.month = 7 and t.weekday < 6"
    ).fetchone()[0]
    assert winter > summer  # silver is constant, so only the dynamisation factor differs


# ---------------------------------------------------------------- silver_heat_qh


@pytest.fixture
def heat_silver(tmp_path):
    folder = tmp_path / "when2heat"
    folder.mkdir()
    lines = ["utc_timestamp;cet_cest_timestamp;SPACE;WATER;COPS;COPW;OTHER"]
    for h in range(10):  # 2020-03-01T00:00Z .. 09:00Z; hour 4 is missing (the DST hole)
        if h == 4:
            continue
        lines.append(f"2020-03-01T0{h}:00:00Z;x;{10 + h},5;{2 + h};3,{h};2,5;1")
    (folder / "when2heat.csv").write_text("\n".join(lines))
    cols = {
        "heat_profile_space": "SPACE", "heat_profile_water": "WATER",
        "cop_space": "COPS", "cop_water": "COPW",
    }  # fmt: skip
    sql = render_model(
        "silver/silver_heat_qh.sql", {"bronze_dir": tmp_path.as_posix(), "when2heat_columns": cols}
    )
    con = duckdb.connect()
    con.execute(f"create table silver as {sql}")
    return con


def test_decimal_comma_is_parsed_and_each_hour_becomes_four_rows(heat_silver):
    n, first_cop = heat_silver.execute("select count(*), min(cop_space) from silver").fetchone()
    assert n == 10 * 4  # 9 observed hours + the filled one
    assert first_cop == pytest.approx(3.0)  # '3,0'


def test_single_missing_hour_is_filled_with_neighbour_mean_and_flagged(heat_silver):
    rows = heat_silver.execute(
        "select cop_space, is_estimated from silver where ts_utc >= timestamp '2020-03-01 04:00:00' "
        "and ts_utc < timestamp '2020-03-01 05:00:00'"
    ).fetchall()
    assert len(rows) == 4 and all(est for _, est in rows)
    assert rows[0][0] == pytest.approx((3.3 + 3.5) / 2)
    assert heat_silver.execute("select count(*) from silver where is_estimated").fetchone() == (4,)


def test_heat_shape_sums_to_one_per_source_year(heat_silver):
    space, water = heat_silver.execute(
        "select sum(heat_space_norm), sum(heat_water_norm) from silver"
    ).fetchone()
    assert space == pytest.approx(1.0) and water == pytest.approx(1.0)


# ---------------------------------------------------------------- fact_heat_qh


def test_heat_season_mapping_and_renormalisation():
    con = build_dim_time(*SIM, HOLIDAYS)
    # A complete synthetic silver table for the season 2014/15 where cop_space encodes year*100+month
    con.execute(
        """
        create table silver_heat_qh as
        select ts_utc, year(ts_utc) * 100 + month(ts_utc) as cop_space, 3.0 as cop_water,
               0.25 as heat_space_norm, 0.5 as heat_water_norm
        from (select unnest(generate_series(timestamp '2014-09-30 00:00:00',
                                            timestamp '2015-10-02 00:00:00', interval 15 minute)) as ts_utc)
        """
    )
    con.execute(f"create table fact as {render_model('gold/fact_heat_qh.sql')}")
    n = con.execute("select count(*) from fact").fetchone()[0]
    assert n == 35040

    def season_code(d):
        return con.execute(
            "select cop_space from fact f join dim_time t using (idx) where t.date_local = ? "
            "and t.qh_of_day = 48",
            [d],
        ).fetchone()[0]

    assert season_code("2025-10-15") == 201410  # simulation Oct-Dec -> Oct-Dec 2014
    assert season_code("2026-02-10") == 201502  # simulation Jan-Sep -> Jan-Sep 2015
    assert season_code("2026-09-20") == 201509
    sums = con.execute("select sum(heat_space_norm), sum(heat_water_norm) from fact").fetchone()
    assert sums == (pytest.approx(1.0), pytest.approx(1.0))
    assert con.execute("select count(*) from fact where cop_space is null").fetchone() == (0,)

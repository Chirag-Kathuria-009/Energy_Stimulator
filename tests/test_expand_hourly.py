import duckdb
import pytest
from tests.sql_helpers import render_text


def expand(con, relation="hourly", **kwargs) -> list[tuple]:
    args = ", ".join(f"{k}={v!r}" for k, v in kwargs.items())
    sql = render_text(f"{{{{ expand_hourly_to_qh('{relation}', 'ts_utc', {args}) }}}}")
    return con.execute(sql + " order by ts_utc").fetchall()


@pytest.fixture
def con():
    c = duckdb.connect()
    c.execute(
        "create table hourly as select * from (values "
        "(timestamp '2026-01-01 10:00:00', 5.0, 3.5, 8.0), "
        "(timestamp '2026-01-01 11:00:00', 6.0, 3.0, 4.0)) t(ts_utc, price, cop, energy)"
    )
    return c


def test_one_hourly_row_becomes_four_quarter_hours_with_same_price(con):
    con.execute("delete from hourly where ts_utc >= timestamp '2026-01-01 11:00:00'")
    rows = expand(con, repeat_cols=["price"])
    assert [r[0].strftime("%H:%M") for r in rows] == ["10:00", "10:15", "10:30", "10:45"]
    assert {r[1] for r in rows} == {5.0}


def test_energy_columns_are_split_and_their_sum_is_preserved(con):
    rows = expand(con, repeat_cols=["cop"], split_cols=["energy"])
    assert len(rows) == 8
    assert {r[1] for r in rows[:4]} == {3.5} and {r[2] for r in rows[:4]} == {2.0}
    assert sum(r[2] for r in rows) == pytest.approx(8.0 + 4.0)

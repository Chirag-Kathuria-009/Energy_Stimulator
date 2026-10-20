"""Silver models run against tiny hand-made bronze files in a temporary directory."""

import json
from datetime import UTC, datetime, timedelta

import duckdb
import pytest
from tests.sql_helpers import render_model

BASE = datetime(2025, 9, 30, 22, 0, tzinfo=UTC)  # = first true 15-minute slot (2025-10-01 local)


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def qh(n: int) -> datetime:
    return BASE + timedelta(minutes=15 * n)


# ---------------------------------------------------------------- silver_price_qh


def write_chunk(folder, name, points):
    folder.mkdir(parents=True, exist_ok=True)
    series = [[ms(t), v] for t, v in points]
    (folder / name).write_text(json.dumps({"meta_data": {"version": 1}, "series": series}))


@pytest.fixture
def price(tmp_path):
    folder = tmp_path / "smard" / "4169" / "quarterhour"
    pre = BASE - timedelta(hours=2)  # before the 15-minute regime: hourly values repeated
    points = [(pre + timedelta(minutes=15 * i), 40.0 + i // 4) for i in range(8)]
    points += [(qh(0), 100.0), (qh(1), 110.0), (qh(2), 120.0)]
    points += [(qh(3), None), (qh(4), None), (qh(5), 150.0)]  # 2 missing -> interpolated
    points += [(qh(6), 160.0)]
    points += [(qh(i), None) for i in range(7, 13)] + [(qh(13), 200.0)]  # 6 missing -> stay null
    points += [(qh(14), 210.0), (qh(15), None), (qh(16), None)]  # future slots in newest chunk
    write_chunk(folder, "1000.json", points)
    # An overlapping later chunk wins for the shared timestamp.
    write_chunk(folder, "2000.json", [(qh(0), 105.0)])
    sql = render_model("silver/silver_price_qh.sql", {"bronze_dir": tmp_path.as_posix()})
    con = duckdb.connect()
    con.execute(f"create table silver as {sql}")
    return con


def row(con, n):
    return con.execute(
        "select price_eur_mwh, price_ct_kwh, source_resolution, is_estimated from silver "
        "where ts_utc = ?",
        [qh(n).replace(tzinfo=None)],
    ).fetchone()


def test_unit_conversion_and_dedup_prefers_the_later_chunk(price):
    eur_mwh, ct_kwh, _, est = row(price, 0)
    assert (eur_mwh, ct_kwh, est) == (105.0, 10.5, False)


def test_source_resolution_flag_is_set_by_date(price):
    res = dict(price.execute("select ts_utc, source_resolution from silver").fetchall())
    assert res[(BASE - timedelta(minutes=15)).replace(tzinfo=None)] == "h"
    assert res[BASE.replace(tzinfo=None)] == "qh"


def test_gap_of_two_is_linearly_interpolated_and_flagged(price):
    assert row(price, 3) == (pytest.approx(130.0), pytest.approx(13.0), "qh", True)
    assert row(price, 4)[0] == pytest.approx(140.0) and row(price, 4)[3] is True
    assert row(price, 5)[3] is False  # observed value is not flagged


def test_gap_of_six_is_left_null_and_not_flagged(price):
    for n in range(7, 13):
        assert row(price, n)[0] is None and row(price, n)[3] is False


def test_future_null_slots_are_dropped(price):
    last = price.execute("select max(ts_utc) from silver").fetchone()[0]
    assert last == qh(14).replace(tzinfo=None)


def test_one_row_per_quarter_hour_without_holes(price):
    n, lo, hi = price.execute("select count(*), min(ts_utc), max(ts_utc) from silver").fetchone()
    assert n == int((hi - lo).total_seconds() // 900) + 1


# ---------------------------------------------------------------- silver_load_opsd_qh


CHANNELS = {"base_grid_import_r1": "COL_GRID", "pv_r1": "COL_PV"}


@pytest.fixture
def opsd(tmp_path):
    """Three local days (2025-06-10..12, CEST = UTC+2); cumulative meter rising 0.1 kWh/qh."""
    start = datetime(2025, 6, 9, 21, 45, tzinfo=UTC)  # one reading before the first local midnight
    lines = ["utc_timestamp,COL_GRID,COL_PV,OTHER"]
    cum = 0.0
    for i in range(3 * 96 + 1):
        t = start + timedelta(minutes=15 * i)
        cum += 0.1
        day = (t + timedelta(hours=2)).date().day  # local day of the reading
        grid = f"{cum:.4f}"
        if day == 11 and i in range(100, 102):
            continue  # day 2: two readings missing -> spread evenly over 3 quarter-hours
        if day == 12 and i == 200:
            cum -= 50.0  # day 3: meter reset -> that quarter-hour is dropped, day is incomplete
            grid = f"{cum:.4f}"
        lines.append(f"{t.strftime('%Y-%m-%dT%H:%M:%SZ')},{grid},{cum:.4f},1")
    folder = tmp_path / "opsd"
    folder.mkdir()
    (folder / "household_data_15min.csv").write_text("\n".join(lines))
    sql = render_model(
        "silver/silver_load_opsd_qh.sql",
        {"bronze_dir": tmp_path.as_posix(), "opsd_channels": CHANNELS},
    )
    con = duckdb.connect()
    con.execute(f"create table silver as {sql}")
    return con


def test_only_complete_local_days_are_kept(opsd):
    days = opsd.execute(
        "select channel, date_local, count(*) from silver where channel = 'base_grid_import' "
        "group by all order by 2"
    ).fetchall()
    assert [(str(d), n) for _, d, n in days] == [("2025-06-10", 96), ("2025-06-11", 96)]


def test_site_and_channel_come_from_the_config_key(opsd):
    assert set(opsd.execute("select distinct site, channel from silver").fetchall()) == {
        ("r1", "base_grid_import"),
        ("r1", "pv"),
    }


def test_increments_not_cumulative_values(opsd):
    assert opsd.execute("select min(kwh), max(kwh) from silver").fetchone() == (
        pytest.approx(0.1, abs=1e-6),
        pytest.approx(0.1, abs=1e-6),
    )


def test_small_gap_is_spread_evenly_and_flagged(opsd):
    flagged = opsd.execute(
        "select count(*) from silver where is_estimated and channel = 'base_grid_import'"
    ).fetchone()[0]
    assert flagged == 3  # the two missing readings plus the reading that ends the span


def test_meter_reset_drops_the_day(opsd):
    assert opsd.execute(
        "select count(*) from silver where date_local = date '2025-06-12'"
    ).fetchone() == (0,)

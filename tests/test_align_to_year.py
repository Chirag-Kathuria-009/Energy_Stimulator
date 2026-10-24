from datetime import date

import numpy as np
import pytest
from tests.gold_fixture import make_gold_con

from ets.profiles.calendar import align_to_year, day_class
from ets.simulate.context import load_gold_context


@pytest.fixture(scope="module")
def ctx():
    return load_gold_context(make_gold_con(), opsd_parquet=None)


def const_day(value: float) -> np.ndarray:
    return np.full(96, value)


def per_date(ctx, aligned: np.ndarray, d: date) -> np.ndarray:
    return aligned[ctx.date_local == np.datetime64(d)]


def test_day_class():
    assert day_class(3, False) == "wd" and day_class(6, False) == "sa"
    assert day_class(7, False) == "so" and day_class(2, True) == "so"  # holiday = Sunday class


def test_weekday_class_is_matched_including_holidays(ctx):
    days = {
        date(2016, 6, 15): const_day(1.0),  # Wednesday
        date(2016, 6, 18): const_day(2.0),  # Saturday
        date(2016, 6, 19): const_day(3.0),  # Sunday
    }
    out = align_to_year(days, ctx, total=ctx.n * 1.0)  # keep the raw values readable
    # Rescaling keeps ratios: weekday : Saturday : Sunday = 1 : 2 : 3.
    wd = per_date(ctx, out, date(2025, 11, 12))[10]  # Wednesday
    sa = per_date(ctx, out, date(2025, 11, 15))[10]  # Saturday
    so = per_date(ctx, out, date(2025, 11, 16))[10]  # Sunday
    holiday = per_date(ctx, out, date(2025, 10, 3))[10]  # Friday, national holiday
    assert (sa / wd, so / wd, holiday / wd) == (
        pytest.approx(2),
        pytest.approx(3),
        pytest.approx(3),
    )


def test_closest_day_of_year_wins_with_wraparound(ctx):
    days = {date(2016, 1, 13): const_day(1.0), date(2016, 7, 13): const_day(5.0)}  # both Wednesdays
    out = align_to_year(days, ctx, match_daytype=False, total=ctx.n * 1.0)
    jan_like = per_date(ctx, out, date(2025, 12, 17))[0]  # Dec 17 is 29 days from Jan 13 (wrapped)
    jul_like = per_date(ctx, out, date(2026, 6, 1))[0]
    assert jul_like / jan_like == pytest.approx(5.0)


def test_each_day_keeps_its_energy_and_dst_days_follow_the_clock(ctx):
    out = align_to_year({date(2016, 6, 15): const_day(1.0)}, ctx, match_daytype=False, total=ctx.n)
    normal = per_date(ctx, out, date(2025, 11, 4)).sum()
    october = per_date(ctx, out, date(2025, 10, 26))  # 100 rows: the repeated hour is halved
    march = per_date(ctx, out, date(2026, 3, 29))  # 92 rows: the 02:00 hour is missing
    assert len(october) == 100 and october.sum() == pytest.approx(normal)
    assert len(march) == 92 and march.sum() == pytest.approx(normal * 92 / 96)


def test_total_is_rescaled(ctx):
    out = align_to_year({date(2016, 6, 15): const_day(7.0)}, ctx, match_daytype=False, total=2.5)
    assert out.sum() == pytest.approx(2.5) and len(out) == ctx.n


def test_errors(ctx):
    with pytest.raises(ValueError, match="no source days"):
        align_to_year({}, ctx)
    with pytest.raises(ValueError, match="96 values"):
        align_to_year({date(2016, 6, 15): np.ones(95)}, ctx)
    with pytest.raises(ValueError, match="no source day of class"):
        align_to_year({date(2016, 6, 15): const_day(1.0)}, ctx)  # only weekdays, targets need all

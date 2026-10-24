import numpy as np
import pytest
from tests.gold_fixture import make_gold_con
from tests.tiny import tiny_ctx

from ets.config import load_config
from ets.domain import TariffVariant
from ets.simulate.context import load_gold_context
from ets.tariffs.billing import build_variants
from ets.tariffs.grid14a import device_grid_fee_ct, module1_credit_eur, window_of_qh

CFG = load_config()


def variant(*modules) -> TariffVariant:
    return TariffVariant(id="v", kind="dynamic", modules=tuple(modules))


def test_variants_from_config():
    vs = {v.id: v for v in build_variants(CFG)}
    assert vs["fixed"].kind == "fixed" and vs["fixed"].modules == ()
    assert vs["dynamic_m1_m3"].modules == (1, 3) and vs["dynamic_m2"].modules == (2,)
    assert len(vs) == 6


def test_window_lookup_by_local_clock():
    # (local time, expected window), from the Preisblatt: low 01:30-04:30 and 10:00-17:00;
    # high 07:00-08:00 and 19:00-22:00; standard otherwise.
    cases = [
        ("00:00", "standard"), ("01:15", "standard"), ("01:30", "low"), ("04:15", "low"),
        ("04:30", "standard"), ("07:00", "high"), ("07:45", "high"), ("08:00", "standard"),
        ("09:45", "standard"), ("10:00", "low"), ("16:45", "low"), ("17:00", "standard"),
        ("18:45", "standard"), ("19:00", "high"), ("21:45", "high"), ("22:00", "standard"),
        ("23:45", "standard"),
    ]  # fmt: skip
    qh = np.array([int(t[:2]) * 4 + int(t[3:]) // 15 for t, _ in cases])
    assert list(window_of_qh(qh, CFG.grid_14a.module3)) == [w for _, w in cases]


def test_every_quarter_hour_gets_exactly_one_window():
    names = window_of_qh(np.arange(96), CFG.grid_14a.module3)
    assert set(names) == {"high", "standard", "low"} and len(names) == 96


def test_device_fee_without_module_and_with_module1_is_the_standard_fee():
    ctx = tiny_ctx([10, 10, 10], qh_of_day=[0, 6, 80])
    for modules in ((), (1,)):
        assert list(device_grid_fee_ct(variant(*modules), ctx, CFG)) == [9.43] * 3


def test_module2_is_40_percent_of_the_grid_fee():
    ctx = tiny_ctx([10, 10], qh_of_day=[0, 80])
    assert list(device_grid_fee_ct(variant(2), ctx, CFG)) == [pytest.approx(9.43 * 0.4)] * 2
    assert 9.43 * 0.4 == pytest.approx(3.77, abs=0.01)  # the published Module 2 price


def test_module3_fee_follows_the_window_with_and_without_module1():
    ctx = tiny_ctx([10] * 4, qh_of_day=[0, 6, 40, 80])  # standard, low, low, high
    for modules in ((3,), (1, 3)):
        assert list(device_grid_fee_ct(variant(*modules), ctx, CFG)) == [9.43, 3.77, 3.77, 18.71]


def test_module1_credit_is_gross_and_only_for_module1():
    assert module1_credit_eur(variant(1), CFG) == pytest.approx(137.95 * 1.19)
    assert module1_credit_eur(variant(1, 3), CFG) == pytest.approx(164.1605)
    assert module1_credit_eur(variant(), CFG) == module1_credit_eur(variant(3), CFG) == 0.0


def test_module3_windows_follow_the_clock_on_dst_days():
    """On clock-change days the UTC offset changes; the window must follow local time."""
    ctx = load_gold_context(make_gold_con(), opsd_parquet=None)
    m3 = CFG.grid_14a.module3

    def window_at(local_date, qh):
        rows = np.where((ctx.date_local == np.datetime64(local_date)) & (ctx.qh_of_day == qh))[0]
        if len(rows) == 0:
            return set(), 0
        return set(window_of_qh(ctx.qh_of_day[rows], m3)), len(rows)

    # 2025-10-26 has 100 quarter-hours: 02:00-02:45 happens twice, both inside the low window.
    assert window_at("2025-10-26", 8) == ({"low"}, 2)
    # 2026-03-29 skips 02:00-02:59: 01:15 standard, 01:30 low, no 02:00, 03:00 low (UTC 01:00).
    assert window_at("2026-03-29", 5) == ({"standard"}, 1)
    assert window_at("2026-03-29", 6) == ({"low"}, 1)
    assert window_at("2026-03-29", 8) == (set(), 0)
    assert window_at("2026-03-29", 12) == ({"low"}, 1)
    # Whole-day fee arrays: low 01:30-04:30 (12 qh, plus the 4 repeated ones on 26 Oct) + 10-17 (28).
    fees = device_grid_fee_ct(variant(3), ctx, CFG)
    october = fees[ctx.date_local == np.datetime64("2025-10-26")]
    march = fees[ctx.date_local == np.datetime64("2026-03-29")]
    assert len(october) == 100 and (october == 3.77).sum() == 12 + 4 + 28
    assert len(march) == 92 and (march == 3.77).sum() == 12 - 4 + 28 + 0  # 02:00-02:45 is missing

import numpy as np
import pytest
from tests.tiny import tiny_ctx, tiny_household

from ets.config import load_config
from ets.domain import TariffVariant
from ets.tariffs.billing import (
    bill,
    build_variants,
    grid_flows,
    gross_price_ct,
    has_device,
    variant_prices,
    variants_for,
)

CFG = load_config()
SPOT = [10.0, 0.0, -5.0, 20.0]
OTHER = 1.666 + 0.941 + 1.559 + 0.446 + 2.05  # concession, offshore, s19, kwkg, tax (net ct/kWh)
V = {v.id: v for v in build_variants(CFG)}


def hand_gross(spot, grid_fee):
    """The billing formula written out again, with the literal config numbers."""
    return (spot + 1.81 + grid_fee + OTHER) * 1.19


def annual(hh, variant_id, ctx):
    v = V[variant_id]
    device = hh.ev_baseline_kwh + hh.hp_baseline_kwh
    return bill(hh, device, variant_prices(v, ctx, CFG), v, CFG)


def test_gross_price_matches_hand_calculation():
    got = gross_price_ct(np.array(SPOT), 9.43, CFG, 1.81)
    assert list(got) == pytest.approx([33.20338, 21.30338, 15.35338, 45.10338])


def test_four_quarter_hour_example_dynamic_bill():
    ctx = tiny_ctx(SPOT)
    hh = tiny_household("A_standard", 4, base=[0.5, 1.0, 2.0, 0.25])
    cost_ct, total = annual(hh, "dynamic", ctx)
    # 0.5 x 33.20338 + 1 x 21.30338 + 2 x 15.35338 + 0.25 x 45.10338 = 79.887675 ct
    assert cost_ct.sum() == pytest.approx(79.887675)
    # + 12 x 5.99 base fee + 30 EUR smart-meter share of household A
    assert total == pytest.approx(0.79887675 + 71.88 + 30.0)


def test_fixed_tariff_bill_is_kwh_times_the_fixed_price_without_fees():
    ctx = tiny_ctx(SPOT)
    hh = tiny_household("A_standard", 4, base=[0.5, 1.0, 2.0, 0.25])
    cost_ct, total = annual(hh, "fixed", ctx)
    assert CFG.price_components.fixed_tariff_gross_ct_kwh == pytest.approx(37.20178)
    assert total == pytest.approx(3.75 * 37.20178 / 100)  # no base fee, no meter fee
    assert list(cost_ct) == pytest.approx([k * 37.20178 for k in (0.5, 1.0, 2.0, 0.25)])


def test_fixed_prices_are_flat_and_equal_for_base_and_device():
    p = variant_prices(V["fixed"], tiny_ctx(SPOT), CFG)
    assert (p.base_ct == p.device_ct).all() and len(set(p.base_ct)) == 1


def test_negative_spot_lowers_the_price_and_it_stays_positive_until_below_the_fixed_parts():
    fixed_parts = 1.81 + 9.43 + OTHER  # everything except the spot price, net
    spots = np.array([0.0, -5.0, -fixed_parts, -fixed_parts - 1.0])
    gross = gross_price_ct(spots, 9.43, CFG, 1.81)
    assert gross[1] < gross[0]  # lower than at spot 0 ...
    assert gross[0] == pytest.approx(fixed_parts * 1.19) and gross[1] > 0  # ... but still positive
    assert gross[2] == pytest.approx(0.0, abs=1e-9)  # zero exactly at spot = -(fixed parts)
    assert gross[3] < 0  # only below that is the customer paid to consume


def test_module2_device_price_is_the_base_price_with_40_percent_grid_fee():
    p = variant_prices(V["dynamic_m2"], tiny_ctx(SPOT), CFG)
    assert list(p.device_ct) == pytest.approx([hand_gross(s, 9.43 * 0.4) for s in SPOT])
    assert list(p.base_ct) == pytest.approx([hand_gross(s, 9.43) for s in SPOT])  # base unchanged


def test_module3_device_price_uses_the_window_fee():
    ctx = tiny_ctx([10.0] * 3, qh_of_day=[0, 40, 80])  # standard, low, high
    p = variant_prices(V["dynamic_m3"], ctx, CFG)
    assert list(p.device_ct) == pytest.approx([hand_gross(10.0, f) for f in (9.43, 3.77, 18.71)])
    assert list(p.base_ct) == pytest.approx([hand_gross(10.0, 9.43)] * 3)


def test_variant_without_module_has_identical_base_and_device_prices():
    p = variant_prices(V["dynamic"], tiny_ctx(SPOT), CFG)
    assert (p.base_ct == p.device_ct).all()


def test_module_credit_grid_fee_and_meter_fee_in_the_annual_bill():
    ctx = tiny_ctx(SPOT)
    hh = tiny_household("B_ev", 4, base=[1.0] * 4, ev=[0.0, 2.0, 0.0, 0.0])
    dyn = annual(hh, "dynamic", ctx)[1]
    m1 = annual(hh, "dynamic_m1", ctx)[1]
    m2 = annual(hh, "dynamic_m2", ctx)[1]
    assert dyn - m1 == pytest.approx(137.95 * 1.19)  # Module 1 changes nothing but the credit
    assert dyn - m2 == pytest.approx(2.0 * 9.43 * 0.6 * 1.19 / 100)  # 2 kWh at 60% less grid fee
    energy = sum(hand_gross(s, 9.43) * k for s, k in zip(SPOT, (1, 3, 1, 1), strict=True))
    assert dyn == pytest.approx(energy / 100 + 71.88 + 50.0)  # household B: 50 EUR meter share


def test_grid_flows_pv_covers_base_first_then_device_and_exports_the_rest():
    base = np.array([1.0, 1.0, 0.0, 0.0])
    device = np.array([0.0, 2.0, 2.0, 0.0])
    pv = np.array([3.0, 1.0, 0.5, 2.0])
    imp_base, imp_device, export = grid_flows(base, device, pv)
    assert list(imp_base) == [0, 0, 0, 0]
    assert list(imp_device) == [0, 2, 1.5, 0]
    assert list(export) == [2, 0, 0, 2]
    assert (base + device + export).sum() == pytest.approx((imp_base + imp_device + pv).sum())


def test_exports_earn_the_feed_in_rate_under_every_tariff():
    ctx = tiny_ctx([10.0, 10.0])
    hh = tiny_household("D_full", 2, base=[0.0, 0.0], pv=[1.0, 3.0])  # D_full: 7.78 ct feed-in
    for vid in ("fixed", "dynamic"):
        assert annual(hh, vid, ctx)[0].sum() == pytest.approx(-4.0 * 7.78)


def test_pv_lowers_the_bill_by_the_avoided_import():
    ctx = tiny_ctx(SPOT)
    plain = tiny_household("D_full", 4, base=[1.0] * 4)
    solar = tiny_household("D_full", 4, base=[1.0] * 4, pv=[0.5] * 4)
    saved = annual(plain, "dynamic", ctx)[0].sum() - annual(solar, "dynamic", ctx)[0].sum()
    assert saved == pytest.approx(0.5 * sum(hand_gross(s, 9.43) for s in SPOT))


def test_module_variants_only_for_households_with_a_device():
    plain = tiny_household("A_standard", 4, base=[1.0] * 4)
    ev = tiny_household("B_ev", 4, base=[1.0] * 4, ev=[1.0] * 4)
    hp = tiny_household("C_heat_pump", 4, base=[1.0] * 4, hp=[1.0] * 4)
    variants = list(V.values())
    assert not has_device(plain) and has_device(ev) and has_device(hp)
    assert [v.id for v in variants_for(plain, variants)] == ["fixed", "dynamic"]
    assert len(variants_for(ev, variants)) == len(variants_for(hp, variants)) == 6


def test_unknown_household_is_an_error():
    ctx = tiny_ctx(SPOT)
    hh = tiny_household("nobody", 4, base=[1.0] * 4)
    with pytest.raises(KeyError, match="nobody"):
        annual(hh, "dynamic", ctx)


def test_tariff_variant_is_a_value_object():
    assert TariffVariant("x", "dynamic", (1, 3)) == TariffVariant("x", "dynamic", (1, 3))

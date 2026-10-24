import logging

import numpy as np
import pytest

from ets.domain import Session
from ets.optimize.greedy import shift_ev_greedy, shift_hp_greedy
from ets.optimize.lp import shift_ev_lp, shift_hp_lp

CAP_KW = 6.0
BUFFER = 10.0
STEP = CAP_KW * 0.25  # electricity per quarter-hour at full power


# ------------------------------------------------------------------ EV


def session(start, end, energy, kw=4.0) -> Session:
    return Session(start, end, energy, kw)


def test_ev_greedy_fills_the_cheapest_slots_first():
    price = np.array([5.0, 3.0, 4.0, 1.0, 2.0])
    out, short = shift_ev_greedy(price, [session(0, 5, 2.5)])  # 4 kW -> 1 kWh per slot
    assert list(out) == [0, 0.5, 0, 1, 1] and short == 0


def test_ev_greedy_breaks_ties_by_the_earlier_slot():
    out, _ = shift_ev_greedy(np.ones(4), [session(0, 4, 2.0)])
    assert list(out) == [1, 1, 0, 0]


def test_ev_greedy_reports_energy_that_does_not_fit():
    out, short = shift_ev_greedy(np.array([3.0, 1.0, 2.0]), [session(0, 3, 5.0)])
    assert list(out) == [1, 1, 1] and short == pytest.approx(2.0)


def test_ev_greedy_only_charges_inside_each_session_and_within_power():
    rng = np.random.default_rng(1)
    price = rng.normal(10, 5, 400)
    sessions = [session(10, 60, 7.3), session(100, 150, 12.0, 11.0), session(300, 330, 3.3)]
    out, short = shift_ev_greedy(price, sessions)
    assert short == 0
    for s in sessions:
        seg = out[s.start_idx : s.end_idx]
        assert seg.sum() == pytest.approx(s.energy_kwh)
        assert seg.min() >= 0 and seg.max() <= s.p_max_kw * 0.25 + 1e-12
    inside = np.zeros(400, dtype=bool)
    for s in sessions:
        inside[s.start_idx : s.end_idx] = True
    assert out[~inside].sum() == 0


def test_ev_lp_matches_greedy_cost_and_respects_the_constraints():
    rng = np.random.default_rng(7)
    price = rng.normal(10, 8, 600)  # includes negative prices
    sessions = [session(i * 100, i * 100 + 52, 6.0 + i, 11.0) for i in range(6)]
    greedy, _ = shift_ev_greedy(price, sessions)
    lp, short = shift_ev_lp(price, sessions)
    assert short == 0
    assert (lp * price).sum() == pytest.approx((greedy * price).sum(), abs=1e-6)
    for s in sessions:
        seg = lp[s.start_idx : s.end_idx]
        assert seg.sum() == pytest.approx(s.energy_kwh, abs=1e-6)
        assert seg.min() >= 0 and seg.max() <= 11 * 0.25 + 1e-9
    assert lp[52:100].sum() == 0  # nothing between sessions


def test_ev_lp_with_a_too_short_window_falls_back_to_greedy():
    price = np.array([3.0, 1.0, 2.0])
    out, short = shift_ev_lp(price, [session(0, 3, 5.0)])
    assert list(out) == [1, 1, 1] and short == pytest.approx(2.0)


# ------------------------------------------------------------------ heat pump


def random_day(rng, t=96):
    heat = 0.2 + rng.random(t) * 0.8
    cop = 2.5 + rng.random(t) * 1.5
    price = rng.normal(10, 6, t)  # some negative
    return price, heat, cop


def check_day(el, heat, cop, label=""):
    """Every constraint of the heat-pump model on one day."""
    level = BUFFER / 2 + np.cumsum(el * cop - heat)
    assert el.min() >= -1e-9 and el.max() <= STEP + 1e-9, f"{label}: power limit"
    assert level.min() >= -1e-7 and level.max() <= BUFFER + 1e-7, f"{label}: buffer bounds"
    assert level[-1] == pytest.approx(BUFFER / 2, abs=1e-7), f"{label}: end level"
    assert (el * cop).sum() == pytest.approx(heat.sum(), abs=1e-7), f"{label}: daily heat"


def jit_cost(price, heat, cop):
    return float((price * heat / cop).sum())


def run_hp(fn, price, heat, cop):
    return fn(price, heat, cop, CAP_KW, BUFFER, [(0, len(heat))])


@pytest.mark.parametrize("fn", [shift_hp_greedy, shift_hp_lp], ids=["greedy", "lp"])
def test_hp_schedules_satisfy_every_constraint_on_random_days(fn):
    rng = np.random.default_rng(3)
    for k in range(15):
        price, heat, cop = random_day(rng)
        check_day(run_hp(fn, price, heat, cop), heat, cop, f"day {k}")


def test_hp_lp_is_never_more_expensive_than_greedy_or_just_in_time():
    rng = np.random.default_rng(11)
    for k in range(20):
        price, heat, cop = random_day(rng)
        lp = (run_hp(shift_hp_lp, price, heat, cop) * price).sum()
        greedy = (run_hp(shift_hp_greedy, price, heat, cop) * price).sum()
        assert lp <= greedy + 1e-6, f"day {k}: LP {lp:.4f} > greedy {greedy:.4f}"
        assert lp <= jit_cost(price, heat, cop) + 1e-6, f"day {k}: LP worse than just in time"


@pytest.mark.parametrize("fn", [shift_hp_greedy, shift_hp_lp], ids=["greedy", "lp"])
def test_hp_shifts_heating_into_cheap_hours(fn):
    t = 96
    price = np.where(np.arange(t) < 24, 2.0, 20.0)  # cheap 00:00-06:00, expensive otherwise
    heat, cop = np.full(t, 0.4), np.full(t, 3.0)
    el = run_hp(fn, price, heat, cop)
    check_day(el, heat, cop)
    # The buffer (10 kWh, starts and ends half full) can move 5 kWh of heat from price 20 to 2:
    # saving = 5 / 3 x (20 - 2) = 30 ct on a just-in-time cost of 198.4 ct, the most possible.
    assert jit_cost(price, heat, cop) == pytest.approx(198.4)
    assert (el * price).sum() == pytest.approx(198.4 - 30.0, abs=1e-6)
    assert el[:24].sum() > (heat / cop)[:24].sum()  # more electricity in the cheap hours


@pytest.mark.parametrize("fn", [shift_hp_greedy, shift_hp_lp], ids=["greedy", "lp"])
def test_hp_flat_price_gives_no_saving(fn):
    t = 96
    price, heat, cop = np.full(t, 12.0), np.full(t, 0.5), np.full(t, 3.0)
    el = run_hp(fn, price, heat, cop)
    check_day(el, heat, cop)
    assert (el * price).sum() == pytest.approx(jit_cost(price, heat, cop))


def test_hp_lp_uses_negative_prices():
    t = 96
    price = np.full(t, 10.0)
    price[40:44] = -20.0
    heat, cop = np.full(t, 0.5), np.full(t, 3.0)
    el = run_hp(shift_hp_lp, price, heat, cop)
    check_day(el, heat, cop)
    assert el[40:44].sum() > 3 * (heat / cop)[40:44].sum()  # fills the buffer while paid to
    assert (el * price).sum() < jit_cost(price, heat, cop)


def test_hp_prefers_hours_with_a_better_cop_at_a_flat_price():
    t = 96
    price, heat = np.full(t, 10.0), np.full(t, 0.5)
    cop = np.where(np.arange(t) % 48 < 24, 2.0, 4.0)
    lp = run_hp(shift_hp_lp, price, heat, cop)
    check_day(lp, heat, cop)
    assert lp.sum() < (heat / cop).sum()  # less electricity for the same heat


@pytest.mark.parametrize("fn", [shift_hp_greedy, shift_hp_lp], ids=["greedy", "lp"])
def test_hp_day_that_cannot_be_served_runs_just_in_time_and_is_logged(fn, caplog):
    t = 8
    price, cop = np.full(t, 10.0), np.full(t, 3.0)
    heat = np.full(t, 10.0)  # 10 kWh heat per slot, but at most 4.5 kWh per slot is possible
    with caplog.at_level(logging.WARNING):
        el = run_hp(fn, price, heat, cop)
    assert np.allclose(el, STEP)  # full power all day
    assert "cannot be shifted" in caplog.text or "infeasible" in caplog.text


def test_hp_days_are_planned_independently():
    rng = np.random.default_rng(5)
    price, heat, cop = random_day(rng, 192)
    days = [(0, 96), (96, 192)]
    el = shift_hp_lp(price, heat, cop, CAP_KW, BUFFER, days)
    for a, b in days:
        check_day(el[a:b], heat[a:b], cop[a:b])

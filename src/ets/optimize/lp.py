"""Linear-programming load shifting (PuLP with the HiGHS solver): the optimal reference.

Same inputs and constraints as the greedy functions, so the LP cost is a lower bound for them.
Written for PuLP 4 (variables via `prob.add_variable`, `solve` returns a stats object).
"""

import logging

import numpy as np
import pulp

from ets.domain import Session
from ets.optimize.greedy import _hp_day_greedy, shift_ev_greedy
from ets.settings import QH_HOURS

log = logging.getLogger(__name__)


def _solve(prob: pulp.LpProblem):
    return prob.solve(pulp.HiGHS(msg=False))


def _ev_session_lp(price: np.ndarray, energy_kwh: float, step_kwh: float) -> np.ndarray | None:
    """min sum(price x e) with 0 <= e <= step and sum(e) = energy; None if infeasible."""
    prob = pulp.LpProblem("ev_session", pulp.LpMinimize)
    e = [prob.add_variable(f"e{i}", 0, step_kwh) for i in range(len(price))]
    prob += pulp.lpSum(float(p) * v for p, v in zip(price, e, strict=True))
    prob += pulp.lpSum(e) == energy_kwh
    stats = _solve(prob)
    if not stats.has_solution or stats.status != pulp.LpSolveStatus.Optimal:
        return None
    return np.clip([v.value() for v in e], 0.0, step_kwh)


def shift_ev_lp(price_ct: np.ndarray, sessions: list[Session]) -> tuple[np.ndarray, float]:
    """Optimal charging of every session. A session whose window is too short is charged by the
    greedy rule (full power in the cheapest slots) and the rest counts as shortfall."""
    out = np.zeros(len(price_ct))
    shortfall = 0.0
    for s in sessions:
        sl = slice(s.start_idx, s.end_idx)
        step = s.p_max_kw * QH_HOURS
        plan = _ev_session_lp(price_ct[sl], s.energy_kwh, step)
        if plan is None:
            fallback, missing = shift_ev_greedy(price_ct, [s])
            out += fallback
            shortfall += missing
        else:
            out[sl] += plan
    return out, shortfall


def _hp_day_lp(
    price: np.ndarray, heat: np.ndarray, cop: np.ndarray, p_max_kw: float, buffer_kwh: float
) -> np.ndarray | None:
    """Electricity per slot for one day, or None if infeasible.

    min sum(price x e)   with 0 <= e <= p_max x 0.25,
    level[t] = level[t-1] + e[t] x cop[t] - heat[t],  0 <= level[t] <= buffer,
    level starts at half the buffer and ends there.
    """
    t = len(heat)
    start = buffer_kwh / 2
    cap = p_max_kw * QH_HOURS
    prob = pulp.LpProblem("hp_day", pulp.LpMinimize)
    e = [prob.add_variable(f"e{i}", 0, cap) for i in range(t)]
    level = [prob.add_variable(f"s{i}", 0, buffer_kwh) for i in range(t)]
    prob += pulp.lpSum(float(p) * v for p, v in zip(price, e, strict=True))
    for k in range(t):
        previous = start if k == 0 else level[k - 1]
        prob += level[k] == previous + float(cop[k]) * e[k] - float(heat[k])
    prob += level[t - 1] == start
    stats = _solve(prob)
    if not stats.has_solution or stats.status != pulp.LpSolveStatus.Optimal:
        return None
    return np.clip([v.value() for v in e], 0.0, cap)


def shift_hp_lp(
    price_ct: np.ndarray,
    heat: np.ndarray,
    cop: np.ndarray,
    p_max_kw: float,
    buffer_kwh: float,
    days: list[tuple[int, int]],
) -> np.ndarray:
    """Heat-pump electricity after optimal shifting, one small LP per local day (a controller
    plans with day-ahead prices published once a day). An infeasible day falls back to the greedy
    result for that day (which itself runs just in time if the day cannot be served)."""
    out = np.zeros(len(heat))
    for a, b in days:
        plan = _hp_day_lp(price_ct[a:b], heat[a:b], cop[a:b], p_max_kw, buffer_kwh)
        if plan is None:
            log.warning("heat pump: LP infeasible for the day at position %d; using greedy", a)
            thermal = _hp_day_greedy(price_ct[a:b], heat[a:b], cop[a:b], p_max_kw, buffer_kwh)
            plan = (
                np.minimum(heat[a:b] / cop[a:b], p_max_kw * QH_HOURS)
                if thermal is None
                else thermal / cop[a:b]
            )
        out[a:b] = plan
    return out

"""Greedy load shifting: simple, explainable, feasible by construction.

`price_ct` is the gross price the device pays per quarter-hour (VariantPrices.device_ct), so a
Module 3 window steers the shifting. Energy is kWh per quarter-hour, power limits in kW.
"""

import logging

import numpy as np

from ets.domain import Session
from ets.settings import QH_HOURS

log = logging.getLogger(__name__)
_EPS = 1e-9


def shift_ev_greedy(price_ct: np.ndarray, sessions: list[Session]) -> tuple[np.ndarray, float]:
    """Charge each session in its cheapest quarter-hours first (ties: earlier slot).

    Returns the charging array and the energy that did not fit into the plug-in windows.
    """
    out = np.zeros(len(price_ct))
    shortfall = 0.0
    for s in sessions:
        step = s.p_max_kw * QH_HOURS
        slots = np.arange(s.start_idx, s.end_idx)
        order = slots[np.lexsort((slots, price_ct[slots]))]  # price ascending, then earlier slot
        remaining = s.energy_kwh
        for slot in order:
            if remaining <= _EPS:
                break
            energy = min(step, remaining)
            out[slot] += energy
            remaining -= energy
        shortfall += max(remaining, 0.0)
    return out, shortfall


def _hp_day_greedy(
    price: np.ndarray, heat: np.ndarray, cop: np.ndarray, p_max_kw: float, buffer_kwh: float
) -> np.ndarray | None:
    """Thermal energy delivered into the buffer per slot for one day, or None if infeasible.

    The buffer starts and ends the day at half full. Two bounds keep every choice feasible:
    `floor` (the buffer must be able to reach the end-of-day level and never run empty) and
    `cap` (never overfill, never end above the start level). Between them the controller charges
    to the cap when this slot is at least as cheap (per unit of heat) as every slot within reach
    of a full buffer, otherwise it only does the minimum.
    """
    t = len(heat)
    start = buffer_kwh / 2
    unit_cost = price / cop  # ct per kWh of heat
    max_heat = p_max_kw * QH_HOURS * cop  # thermal kWh per slot at full power

    # level needed after slot k so that later slots can still finish at the start level
    need = np.empty(t)
    need[-1] = start
    for k in range(t - 1, 0, -1):
        need[k - 1] = max(0.0, need[k] + heat[k] - max_heat[k])
    if need.max() > buffer_kwh + _EPS or max(0.0, need[0] + heat[0] - max_heat[0]) > start + _EPS:
        return None
    future_heat = np.concatenate([np.cumsum(heat[::-1])[::-1][1:], [0.0]])  # heat after slot k
    ceiling = start + future_heat  # highest level after slot k that can still end at `start`

    cum_heat = np.cumsum(heat)
    reach_end = np.minimum(np.searchsorted(cum_heat, cum_heat + buffer_kwh, side="left"), t - 1)

    out = np.zeros(t)
    level = start
    for k in range(t):
        before = level - heat[k]
        floor = max(0.0, need[k] - before)
        cap = min(max_heat[k], buffer_kwh - before, ceiling[k] - before)
        if cap < floor - _EPS:
            return None
        window = unit_cost[k + 1 : reach_end[k] + 1]
        # `<=`: within a run of equally cheap slots every slot must charge, because one slot's
        # power limit cannot fill the buffer (a strict `<` would charge only the last of them)
        charge_fully = window.size == 0 or unit_cost[k] <= window.min()
        out[k] = max(cap, floor) if charge_fully else floor
        level = before + out[k]
    return out


def shift_hp_greedy(
    price_ct: np.ndarray,
    heat: np.ndarray,
    cop: np.ndarray,
    p_max_kw: float,
    buffer_kwh: float,
    days: list[tuple[int, int]],
) -> np.ndarray:
    """Heat-pump electricity per quarter-hour after greedy shifting, planned one day at a time
    (as a controller would with day-ahead prices). A day that cannot be served within the power
    limit falls back to running just in time at the power limit, and is logged."""
    out = np.zeros(len(heat))
    for a, b in days:
        thermal = _hp_day_greedy(price_ct[a:b], heat[a:b], cop[a:b], p_max_kw, buffer_kwh)
        if thermal is None:
            log.warning("heat pump: day at position %d cannot be shifted; running just in time", a)
            out[a:b] = np.minimum(heat[a:b] / cop[a:b], p_max_kw * QH_HOURS)
        else:
            out[a:b] = thermal / cop[a:b]
    return out


def shift_ev_spread(
    price_ct: np.ndarray, sessions: list[Session], k: int, rng: np.random.Generator
) -> tuple[np.ndarray, float]:
    """Herding mitigation: charge in random order among the k cheapest quarter-hours of the
    window instead of strictly cheapest-first, so many households do not pick the same slot.

    At least as many slots as the energy needs are always considered, so the session is served
    whenever the window allows it; the cost is slightly above the greedy cost.
    """
    out = np.zeros(len(price_ct))
    shortfall = 0.0
    for s in sessions:
        step = s.p_max_kw * QH_HOURS
        slots = np.arange(s.start_idx, s.end_idx)
        order = slots[np.lexsort((slots, price_ct[slots]))]
        needed = int(np.ceil(s.energy_kwh / step - _EPS))
        pool = rng.permutation(order[: max(k, needed)])
        remaining = s.energy_kwh
        for slot in pool:
            if remaining <= _EPS:
                break
            energy = min(step, remaining)
            out[slot] += energy
            remaining -= energy
        shortfall += max(remaining, 0.0)
    return out, shortfall

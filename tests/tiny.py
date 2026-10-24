"""Hand-sized contexts and households for billing examples that can be checked by calculator."""

import numpy as np

from ets.domain import Household, Session
from ets.simulate.context import GoldContext


def tiny_ctx(prices, qh_of_day=None) -> GoldContext:
    """A GoldContext of a few quarter-hours; only prices and the local clock position matter."""
    n = len(prices)
    qh = np.arange(n) if qh_of_day is None else np.asarray(qh_of_day)
    zeros, ones = np.zeros(n), np.ones(n)
    return GoldContext(
        n=n,
        ts_utc=np.arange(n).astype("datetime64[us]"),
        date_local=np.full(n, np.datetime64("2025-10-01")),
        qh_of_day=qh,
        weekday=np.full(n, 3),
        month=np.full(n, 10),
        is_holiday=np.zeros(n, dtype=bool),
        price_ct=np.asarray(prices, dtype=float),
        heat_space_norm=zeros,
        heat_water_norm=zeros,
        cop_space=ones,
        cop_water=ones,
    )


def tiny_household(household_id, n, *, base=None, ev=None, hp=None, pv=None) -> Household:
    """A Household of n quarter-hours; `ev` / `hp` are unshifted device energy arrays."""
    z = np.zeros(n)
    return Household(
        id=household_id,
        base_kwh=z if base is None else np.asarray(base, dtype=float),
        ev_sessions=[] if ev is None else [Session(0, n, float(np.sum(ev)), 11.0)],
        ev_baseline_kwh=z if ev is None else np.asarray(ev, dtype=float),
        hp_heat_kwh=z,
        hp_cop=np.ones(n),
        hp_baseline_kwh=z if hp is None else np.asarray(hp, dtype=float),
        hp_p_max_kw=6.0 if hp is not None else 0.0,
        hp_buffer_kwh=10.0 if hp is not None else 0.0,
        pv_kwh=z if pv is None else np.asarray(pv, dtype=float),
    )

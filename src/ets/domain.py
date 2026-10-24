"""Dataclasses shared by Phases 3-6. All arrays have length N (quarter-hours in the sim year)."""

from dataclasses import dataclass
from typing import Literal

import numpy as np


@dataclass(frozen=True)
class Session:
    """One EV plug-in period."""

    start_idx: int  # index into the simulation year's quarter-hour array
    end_idx: int  # exclusive
    energy_kwh: float  # needed before end_idx
    p_max_kw: float


@dataclass
class Household:
    id: str
    base_kwh: np.ndarray  # inflexible load per quarter-hour
    ev_sessions: list[Session]
    ev_baseline_kwh: np.ndarray  # charging without optimisation
    hp_heat_kwh: np.ndarray  # heat demand per quarter-hour
    hp_cop: np.ndarray
    hp_baseline_kwh: np.ndarray  # electricity without optimisation
    hp_p_max_kw: float
    hp_buffer_kwh: float
    pv_kwh: np.ndarray


@dataclass(frozen=True)
class TariffVariant:
    id: str
    kind: Literal["fixed", "dynamic"]
    modules: tuple[int, ...]  # () or (1,), (2,), (3,), (1, 3)


@dataclass
class SimResult:
    household_id: str
    variant_id: str
    method: Literal["none", "greedy", "lp"]
    grid_import_kwh: np.ndarray
    cost_ct: np.ndarray
    annual_eur: float
    shifted_kwh: float
    shortfall_kwh: float
    device_kwh: np.ndarray | None = None  # EV + heat-pump electricity after shifting

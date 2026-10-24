"""§14a EnWG grid-fee modules for controllable devices (EV wallbox, heat pump).

All fees here are net of VAT, in ct/kWh. ASSUMPTION (DECISIONS.md #6): the module reductions
apply to the controllable device's energy only; the base load always pays the standard grid fee.
The Preisblatt's grid base price (80 EUR/year; 0 EUR for Module 2) is not modelled (DECISIONS.md).
"""

import numpy as np

from ets.config import AppConfig, Module3Cfg
from ets.domain import TariffVariant
from ets.simulate.context import GoldContext


def window_of_qh(qh_of_day: np.ndarray, module3: Module3Cfg) -> np.ndarray:
    """Map local clock quarter-hours (0..95) to 'high', 'standard' or 'low'.

    `qh_of_day` is the local clock position from dim_time, so DST days are already handled: the
    repeated October hour appears twice, the skipped March hour does not appear at all.
    """
    return np.array(module3.window_by_qh())[qh_of_day]


def device_grid_fee_ct(variant: TariffVariant, ctx: GoldContext, cfg: AppConfig) -> np.ndarray:
    """Net grid fee per quarter-hour that the controllable device pays under this variant.

    No module or Module 1: the standard fee everywhere. Module 2: standard x 0.40. Module 3 (with
    or without Module 1): the fee of the time window.
    """
    standard = cfg.price_components.grid_fee_ct_kwh
    if 3 in variant.modules:
        m3 = cfg.grid_14a.module3
        fee_by_qh = np.array([m3.grid_fee_ct_kwh[w] for w in m3.window_by_qh()])  # length 96
        return fee_by_qh[ctx.qh_of_day]
    if 2 in variant.modules:
        return np.full(ctx.n, standard * cfg.grid_14a.module2_reduction_factor)
    return np.full(ctx.n, standard)


def module1_credit_eur(variant: TariffVariant, cfg: AppConfig) -> float:
    """Annual Module 1 reduction, gross (the Preisblatt amount is net of VAT)."""
    return cfg.module1_credit_eur_year_gross if 1 in variant.modules else 0.0

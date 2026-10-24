"""Gross prices per quarter-hour and annual bills for every tariff variant.

Prices are ct/kWh gross of VAT, amounts of energy are kWh per quarter-hour, annual totals EUR.
"""

from dataclasses import dataclass

import numpy as np

from ets.config import AppConfig, HouseholdCfg
from ets.domain import Household, TariffVariant
from ets.simulate.context import GoldContext
from ets.tariffs.grid14a import device_grid_fee_ct, module1_credit_eur


@dataclass(frozen=True)
class VariantPrices:
    """Two price arrays because under §14a the device and the rest of the house can pay different
    grid fees in the same quarter-hour, and the optimiser (Phase 5) must see the device price."""

    base_ct: np.ndarray  # price for the inflexible base load (standard grid fee)
    device_ct: np.ndarray  # price for EV / heat-pump energy (module grid fee)


def build_variants(cfg: AppConfig) -> list[TariffVariant]:
    return [
        TariffVariant(id=v.id, kind=v.type, modules=tuple(v.grid14a_module))
        for v in cfg.tariffs.variants
    ]


def has_device(hh: Household) -> bool:
    """A household can use a §14a module only if it has a controllable device."""
    return bool(hh.ev_sessions) or hh.hp_baseline_kwh.sum() > 0


def variants_for(hh: Household, variants: list[TariffVariant]) -> list[TariffVariant]:
    """Module variants only make sense for households with a controllable device."""
    return [v for v in variants if not v.modules or has_device(hh)]


def gross_price_ct(
    spot_ct: np.ndarray | float,
    grid_fee_ct: np.ndarray | float,
    cfg: AppConfig,
    markup_ct: float,
) -> np.ndarray:
    """(spot + markup + grid fee + concession + offshore + s19 + kwkg + electricity tax) x (1+VAT).

    The spot price can be negative; the other components stay, so the gross price shrinks but
    is positive as long as spot > -(sum of the other components).
    """
    pc = cfg.price_components
    net = (
        np.asarray(spot_ct, dtype=float)
        + markup_ct
        + grid_fee_ct
        + pc.concession_ct_kwh
        + pc.offshore_levy_ct_kwh
        + pc.s19_levy_ct_kwh
        + pc.kwkg_levy_ct_kwh
        + pc.electricity_tax_ct_kwh
    )
    return net * (1 + pc.vat_rate)


def variant_prices(variant: TariffVariant, ctx: GoldContext, cfg: AppConfig) -> VariantPrices:
    """Fixed: the same all-in price everywhere. Dynamic: spot plus supplier markup plus the grid
    fee, which is the standard fee for the base load and the module fee for the device."""
    if variant.kind == "fixed":
        flat = np.full(ctx.n, cfg.price_components.fixed_tariff_gross_ct_kwh)
        return VariantPrices(base_ct=flat, device_ct=flat)
    markup = cfg.tariffs.supplier.markup_ct_kwh
    base = gross_price_ct(ctx.price_ct, cfg.price_components.grid_fee_ct_kwh, cfg, markup)
    if not variant.modules:
        return VariantPrices(base_ct=base, device_ct=base)
    device = gross_price_ct(ctx.price_ct, device_grid_fee_ct(variant, ctx, cfg), cfg, markup)
    return VariantPrices(base_ct=base, device_ct=device)


def grid_flows(
    base: np.ndarray, device: np.ndarray, pv: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Split the PV output between the loads and the grid.

    ASSUMPTION: PV covers the base load first, then the device. Returns
    (grid import for base, grid import for device, export).
    """
    pv_to_base = np.minimum(base, pv)
    pv_to_device = np.minimum(device, pv - pv_to_base)
    export = pv - pv_to_base - pv_to_device
    return base - pv_to_base, device - pv_to_device, export


def household_cfg(cfg: AppConfig, household_id: str) -> HouseholdCfg:
    for h in cfg.households:
        if h.id == household_id:
            return h
    raise KeyError(f"household {household_id!r} is not in the config")


def bill(
    hh: Household,
    device_kwh: np.ndarray,
    prices: VariantPrices,
    variant: TariffVariant,
    cfg: AppConfig,
) -> tuple[np.ndarray, float]:
    """Cost per quarter-hour (ct, energy part only) and the annual bill in EUR.

    annual = sum(cost) / 100 + 12 x base fee + smart-meter fee - Module 1 credit. A fixed tariff
    has no base or meter fee (already inside its all-in price) and no module credit. Exports are
    paid at the household's feed-in rate under every tariff.
    """
    h_cfg = household_cfg(cfg, hh.id)
    import_base, import_device, export = grid_flows(hh.base_kwh, device_kwh, hh.pv_kwh)
    feed_in = h_cfg.pv.feed_in_ct_kwh if h_cfg.pv is not None else 0.0
    cost_ct = import_base * prices.base_ct + import_device * prices.device_ct - export * feed_in
    annual = cost_ct.sum() / 100.0
    if variant.kind == "dynamic":
        annual += 12 * cfg.tariffs.supplier.base_fee_eur_month
        annual += h_cfg.smart_meter_fee_eur_year
        annual -= module1_credit_eur(variant, cfg)
    return cost_ct, float(annual)

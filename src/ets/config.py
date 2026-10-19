"""Typed configuration. Unverified values are `null` in YAML and fail loudly here."""

import hashlib
import json
import re
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ets.settings import CONFIG_DIR

# ct/kWh values may be negative (rebates) but never absurd.
CtKwh = Annotated[float, Field(ge=-100, le=200)]
Hhmm = Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]

WINDOW_NAMES = ("high", "standard", "low")
QH_PER_DAY = 96


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PriceComponents(_Strict):
    year: int
    fixed_tariff_gross_ct_kwh: CtKwh
    grid_fee_ct_kwh: CtKwh
    concession_ct_kwh: CtKwh
    offshore_levy_ct_kwh: CtKwh
    s19_levy_ct_kwh: CtKwh
    kwkg_levy_ct_kwh: CtKwh
    electricity_tax_ct_kwh: CtKwh
    vat_rate: Annotated[float, Field(ge=0, le=0.3)]


class SupplierCfg(_Strict):
    """Dynamic-tariff supplier terms, shared by every dynamic variant."""

    name: str
    markup_ct_kwh: CtKwh  # net of VAT; VAT is added in billing
    base_fee_eur_month: Annotated[float, Field(ge=0, le=50)]  # gross, added after VAT
    smart_meter_fee_eur_year: Annotated[float, Field(ge=0, le=300)]  # gross


class TariffCfg(_Strict):
    id: str
    type: Literal["fixed", "dynamic"]
    grid14a_module: list[int] = Field(default_factory=list)

    @field_validator("grid14a_module", mode="before")
    @classmethod
    def _as_list(cls, v):
        if v is None:
            return []
        return [v] if isinstance(v, int) else v

    @field_validator("grid14a_module")
    @classmethod
    def _valid_modules(cls, v: list[int]) -> list[int]:
        if not set(v) <= {1, 2, 3}:
            raise ValueError(f"grid14a_module must be a subset of 1,2,3, got {v}")
        if 2 in v and 3 in v:
            raise ValueError("Modules 2 and 3 cannot be combined")
        return sorted(set(v))

    @model_validator(mode="after")
    def _fixed_has_no_module(self):
        if self.type == "fixed" and self.grid14a_module:
            raise ValueError("fixed tariff cannot have a §14a module")
        return self


class TariffsCfg(_Strict):
    supplier: SupplierCfg
    variants: list[TariffCfg] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_ids(self):
        ids = [v.id for v in self.variants]
        if len(ids) != len(set(ids)):
            raise ValueError(f"duplicate tariff ids: {ids}")
        return self


def _parse_window(text: str) -> list[int]:
    """'HH:MM-HH:MM' -> quarter-hour-of-day indices; end <= start wraps past midnight."""
    m = re.fullmatch(r"(\d\d):(\d\d)-(\d\d):(\d\d)", text.strip())
    if not m:
        raise ValueError(f"bad window {text!r}, expected 'HH:MM-HH:MM'")
    h1, m1, h2, m2 = map(int, m.groups())
    if m1 % 15 or m2 % 15 or h1 > 23 or h2 > 24 or m1 > 59 or m2 > 59:
        raise ValueError(f"window {text!r} must sit on quarter-hour boundaries")
    start = (h1 * 60 + m1) // 15
    end = (h2 * 60 + m2) // 15
    if end <= start:
        end += QH_PER_DAY
    return [q % QH_PER_DAY for q in range(start, end)]


class Module3Cfg(_Strict):
    windows_local_time: dict[Literal["high", "standard", "low"], list[str]]
    grid_fee_ct_kwh: dict[Literal["high", "standard", "low"], CtKwh]

    @model_validator(mode="after")
    def _windows_cover_day_once(self):
        if set(self.windows_local_time) != set(WINDOW_NAMES):
            raise ValueError(f"windows_local_time needs keys {WINDOW_NAMES}")
        if set(self.grid_fee_ct_kwh) != set(WINDOW_NAMES):
            raise ValueError(f"grid_fee_ct_kwh needs keys {WINDOW_NAMES}")
        seen: dict[int, str] = {}
        for name, wins in self.windows_local_time.items():
            for w in wins:
                for q in _parse_window(w):
                    if q in seen:
                        raise ValueError(
                            f"Module 3 windows overlap at quarter-hour {q} "
                            f"({seen[q]} and {name})"
                        )
                    seen[q] = name
        missing = sorted(set(range(QH_PER_DAY)) - set(seen))
        if missing:
            raise ValueError(f"Module 3 windows leave {len(missing)} quarter-hours uncovered: {missing[:5]}...")
        return self

    def window_by_qh(self) -> list[str]:
        """Length-96 list: window name for each local quarter-hour of day."""
        out = [""] * QH_PER_DAY
        for name, wins in self.windows_local_time.items():
            for w in wins:
                for q in _parse_window(w):
                    out[q] = name
        return out


class Grid14aCfg(_Strict):
    grid_operator: str
    source_url: str
    module1_credit_eur_year_net: Annotated[float, Field(ge=0, le=500)]
    module2_reduction_factor: Annotated[float, Field(gt=0, le=1)]
    module3: Module3Cfg
    dimming_floor_kw: Annotated[float, Field(gt=0)]


class EVCfg(_Strict):
    km_per_year: Annotated[float, Field(gt=0)]
    kwh_per_100km: Annotated[float, Field(gt=0)]
    battery_kwh: Annotated[float, Field(gt=0)]
    wallbox_kw: Annotated[float, Field(gt=0)]
    plug_in_weekday: Hhmm
    plug_out_weekday: Hhmm
    plug_in_weekend: Hhmm | None = None  # None: same window as weekdays (ASSUMPTION)
    plug_out_weekend: Hhmm | None = None


class HeatPumpCfg(_Strict):
    annual_heat_kwh: Annotated[float, Field(gt=0)]
    p_max_kw: Annotated[float, Field(gt=0)]
    thermal_buffer_kwh: Annotated[float, Field(gt=0)]


class PVCfg(_Strict):
    kwp: Annotated[float, Field(gt=0)]
    specific_yield_kwh_per_kwp: Annotated[float, Field(gt=0, le=1500)]
    feed_in_ct_kwh: CtKwh


class HouseholdCfg(_Strict):
    id: str
    annual_kwh: Annotated[float, Field(gt=0)]
    ev: EVCfg | None = None
    heat_pump: HeatPumpCfg | None = None
    pv: PVCfg | None = None


class SourcesCfg(_Strict):
    smard_base: str
    smard_region: str
    smard_price_filter: int
    smard_context_filters: list[int]
    smard_resolution: str
    opsd_household_15min_url: str
    opsd_channels: dict[str, str]  # logical channel -> OPSD column
    when2heat_url: str
    when2heat_columns: dict[str, str]  # logical name -> column
    bdew_local_path: str
    bdew_download_url: str


class AppConfig(_Strict):
    price_components: PriceComponents
    tariffs: TariffsCfg
    grid_14a: Grid14aCfg
    households: list[HouseholdCfg] = Field(min_length=1)
    sources: SourcesCfg

    @model_validator(mode="after")
    def _unique_household_ids(self):
        ids = [h.id for h in self.households]
        if len(ids) != len(set(ids)):
            raise ValueError(f"duplicate household ids: {ids}")
        return self

    @property
    def module1_credit_eur_year_gross(self) -> float:
        return self.grid_14a.module1_credit_eur_year_net * (1 + self.price_components.vat_rate)


_FILES = {
    "price_components": "price_components.yaml",
    "tariffs": "tariffs.yaml",
    "grid_14a": "grid_14a.yaml",
    "households": "households.yaml",
    "sources": "sources.yaml",
}


def load_raw(path: Path = CONFIG_DIR) -> dict:
    """Read the YAML files into one dict, unvalidated."""
    return {key: yaml.safe_load((path / name).read_text(encoding="utf-8")) for key, name in _FILES.items()}


def load_config(path: Path = CONFIG_DIR) -> AppConfig:
    return AppConfig.model_validate(load_raw(path))


def config_hash(cfg: AppConfig) -> str:
    """sha256 of the canonical JSON dump; stored with every simulation run."""
    canonical = json.dumps(cfg.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()

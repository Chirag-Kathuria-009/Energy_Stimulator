import copy

import pytest
from pydantic import ValidationError

from ets.config import AppConfig, config_hash, load_config, load_raw
from ets.settings import N_QH


@pytest.fixture
def raw() -> dict:
    return load_raw()


def test_valid_config_loads():
    cfg = load_config()
    assert [h.id for h in cfg.households] == ["A_standard", "B_ev", "C_heat_pump", "D_full"]
    assert [v.id for v in cfg.tariffs.variants][0] == "fixed"
    assert cfg.tariffs.variants[-1].grid14a_module == [1, 3]
    assert cfg.module1_credit_eur_year_gross == pytest.approx(137.95 * 1.19)
    assert len(config_hash(cfg)) == 64


def test_module3_windows_expand_to_full_day():
    windows = load_config().grid_14a.module3.window_by_qh()
    assert len(windows) == 96 and "" not in windows
    assert windows[0] == "standard"  # 00:00, wraps from the 22:00-01:30 window
    assert windows[4 * 2] == "low"  # 02:00
    assert windows[4 * 20] == "high"  # 20:00
    assert windows[4 * 12] == "low"  # 12:00


def test_simulation_year_has_35040_quarter_hours():
    assert N_QH == 365 * 96


def test_vat_out_of_range_fails(raw):
    raw["price_components"]["vat_rate"] = 1.9
    with pytest.raises(ValidationError, match="vat_rate"):
        AppConfig.model_validate(raw)


def test_overlapping_module3_windows_fail(raw):
    raw = copy.deepcopy(raw)
    raw["grid_14a"]["module3"]["windows_local_time"]["high"].append("18:00-19:30")  # overlaps standard
    with pytest.raises(ValidationError, match="overlap"):
        AppConfig.model_validate(raw)


def test_module3_gap_fails(raw):
    raw["grid_14a"]["module3"]["windows_local_time"]["low"].remove("10:00-17:00")
    with pytest.raises(ValidationError, match="uncovered"):
        AppConfig.model_validate(raw)


def test_null_required_value_fails(raw):
    raw["tariffs"]["supplier"]["markup_ct_kwh"] = None
    with pytest.raises(ValidationError, match="markup_ct_kwh"):
        AppConfig.model_validate(raw)


def test_ct_value_out_of_range_fails(raw):
    raw["price_components"]["grid_fee_ct_kwh"] = 900
    with pytest.raises(ValidationError, match="grid_fee_ct_kwh"):
        AppConfig.model_validate(raw)


def test_modules_2_and_3_cannot_combine(raw):
    raw["tariffs"]["variants"][-1]["grid14a_module"] = [2, 3]
    with pytest.raises(ValidationError, match="cannot be combined"):
        AppConfig.model_validate(raw)

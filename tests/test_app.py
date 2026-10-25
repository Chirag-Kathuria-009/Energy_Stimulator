"""The Streamlit app, run headless with Streamlit's AppTest."""

import duckdb
import pytest
from streamlit.testing.v1 import AppTest
from tests.app_db import make_app_db

from ets.settings import GOLD_DB, ROOT

APP = str(ROOT / "app" / "streamlit_app.py")


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    return make_app_db(tmp_path_factory.mktemp("app_db"))


def run_app(db_path, monkeypatch) -> AppTest:
    monkeypatch.setenv("ETS_GOLD_DB", str(db_path))
    return AppTest.from_file(APP, default_timeout=120).run()


def test_app_renders_every_section_without_errors(db, monkeypatch):
    at = run_app(db, monkeypatch)
    assert not at.exception and not at.error
    assert at.title[0].value == "Dynamic tariff simulator"
    headings = [s.value for s in at.subheader]
    assert headings[0] == "Annual cost by tariff"
    assert "Where the saving comes from" in headings and "Monthly bill" in headings
    assert "What if everybody follows the price?" in headings
    assert any(h.startswith("One week of device load") for h in headings)
    assert len(at.metric) == 5
    assert at.sidebar.selectbox[0].options == ["A_standard", "B_ev", "C_heat_pump", "D_full"]


def test_key_figures_come_from_the_summary_table(db, monkeypatch):
    at = run_app(db, monkeypatch)
    at.sidebar.selectbox[0].select("B_ev")
    at.run()  # the other widgets depend on the household, so they are set after this rerun
    at.sidebar.selectbox[1].select("dynamic")
    at.sidebar.radio[0].set_value("none")
    at.run()
    assert not at.exception
    summary = (
        duckdb.connect(str(db), read_only=True)
        .execute(
            "select annual_eur from v_latest_summary where household_id = 'B_ev' "
            "and variant_id = 'dynamic' and method = 'none'"
        )
        .fetchone()[0]
    )
    assert at.metric[0].label == "Annual bill, Dynamic"
    assert at.metric[0].value == f"{summary:,.0f} €"


def test_household_without_devices_offers_no_shifting_and_says_so(db, monkeypatch):
    at = run_app(db, monkeypatch)
    at.sidebar.selectbox[0].select("A_standard")
    at.run()
    assert not at.exception
    assert at.sidebar.radio[0].options == ["unshifted"]  # labels; the values are raw method names
    assert any("no EV or heat pump" in i.value for i in at.info)
    assert at.sidebar.multiselect[0].options == ["Fixed tariff", "Dynamic"]  # no device, no §14a


def test_every_control_can_be_changed(db, monkeypatch):
    at = run_app(db, monkeypatch)
    at.sidebar.selectbox[0].select("D_full")
    at.run()
    at.sidebar.radio[0].set_value("greedy")
    at.sidebar.multiselect[0].set_value(["fixed", "dynamic_m2", "dynamic_m3"])
    at.sidebar.selectbox[1].select("dynamic_m3")
    at.sidebar.slider[0].set_value(30)
    at.run()
    assert not at.exception and not at.error
    assert at.metric[0].label == "Annual bill, Dynamic + Module 3"
    assert at.sidebar.caption[0].value.startswith("Week 30:")


def test_custom_household_is_computed_on_demand_and_compared_with_the_archetypes(db, monkeypatch):
    at = run_app(db, monkeypatch)
    assert not at.dataframe  # nothing is computed until the button is pressed
    sliders = {s.label: s for s in at.slider}
    sliders["EV driving (km/year, 0 = no EV)"].set_value(15000)
    sliders["Household electricity (kWh/year)"].set_value(4000)
    at.button[0].click()
    at.run()
    assert not at.exception and not at.error
    table = at.dataframe[0].value
    assert "custom" in set(table["household"]) and "D_full" in set(table["household"])
    custom_fixed = table[(table["household"] == "custom") & (table["variant"] == "Fixed tariff")]
    assert len(custom_fixed) == 1 and custom_fixed["€ per year"].iloc[0] > 1000


def test_custom_household_without_devices_has_no_shifting_rows(db, monkeypatch):
    at = run_app(db, monkeypatch)
    at.button[0].click()
    at.run()
    assert not at.exception
    custom = at.dataframe[0].value.query("household == 'custom'")
    assert set(custom["method"]) == {"unshifted"}  # nothing to shift, no §14a module


def test_missing_database_gives_a_clear_message_not_a_crash(tmp_path, monkeypatch):
    at = run_app(tmp_path / "nothing.duckdb", monkeypatch)
    assert not at.exception
    assert any("not found" in e.value for e in at.error)


def test_database_without_result_tables_gives_a_clear_message(tmp_path, monkeypatch):
    empty = tmp_path / "empty.duckdb"
    duckdb.connect(str(empty)).close()
    at = run_app(empty, monkeypatch)
    assert not at.exception
    assert any("result tables are missing" in e.value for e in at.error)


@pytest.mark.skipif(not GOLD_DB.exists(), reason="no real database")
def test_app_runs_on_the_real_results(monkeypatch):
    con = duckdb.connect(str(GOLD_DB), read_only=True)
    has = con.execute(
        "select count(*) from information_schema.tables where table_name = 'v_latest_summary'"
    ).fetchone()[0]
    con.close()
    if not has:
        pytest.skip("run `ets simulate` first")
    monkeypatch.setenv("ETS_GOLD_DB", str(GOLD_DB))
    at = AppTest.from_file(APP, default_timeout=120).run()
    assert not at.exception and not at.error and len(at.metric) == 5

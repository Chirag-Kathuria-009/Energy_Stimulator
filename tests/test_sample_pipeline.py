"""The committed CI sample builds the whole silver and gold layer, with no live data or network.

This is what the CI workflow does: copy the sample into a temporary data directory, point the
pipeline at it with environment variables and run `ets transform` (dbt build and gold checks).
"""

import json
import os
import shutil
import subprocess
import sys
from datetime import date

import duckdb
import pytest

from ets.config import load_config
from ets.sample import SAMPLE_DIR
from ets.settings import ROOT
from ets.simulate.context import load_gold_context
from ets.simulate.run import build_households, run_baseline

pytestmark = pytest.mark.skipif(not SAMPLE_DIR.exists(), reason="no data/sample (ets make-sample)")


def cli(*args: str, env: dict) -> subprocess.CompletedProcess:
    code = "from ets.cli import app; app()"
    return subprocess.run(
        [sys.executable, "-c", code, *args],
        env=env, cwd=ROOT, capture_output=True, text=True, timeout=600,
    )  # fmt: skip


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    work = tmp_path_factory.mktemp("ets_sample")
    for part in ("bronze", "staging"):
        shutil.copytree(SAMPLE_DIR / part, work / part)
    window = json.loads((SAMPLE_DIR / "window.json").read_text())
    env = {
        **os.environ,
        "ETS_DATA_DIR": str(work),
        "ETS_SIM_START": window["start"],
        "ETS_SIM_END": window["end"],
    }
    result = cli("transform", env=env)
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]
    n_days = (date.fromisoformat(window["end"]) - date.fromisoformat(window["start"])).days + 1
    return work, n_days, result


def test_transform_succeeds_on_the_sample_with_green_dbt_tests_and_gold_checks(built):
    _, _, result = built
    assert "Transform OK" in result.stdout
    assert "ERROR=0" in result.stdout


def test_gold_tables_cover_exactly_the_sample_window(built):
    work, n_days, _ = built
    con = duckdb.connect(str(work / "gold" / "energy.duckdb"), read_only=True)
    n = n_days * 96
    for table in ("dim_time", "fact_price_qh", "fact_heat_qh"):
        assert con.execute(f"select count(*) from {table}").fetchone()[0] == n
    assert con.execute("select count(*) from fact_load_profile_qh").fetchone()[0] == 2 * n
    assert con.execute(
        "select count(*) from fact_price_qh where price_ct_kwh is null"
    ).fetchone() == (0,)
    first_day = con.execute("select min(date_local), max(date_local) from dim_time").fetchone()
    assert first_day == (date(2026, 4, 20), date(2026, 5, 3))
    assert (
        con.execute("select count(*) from fact_price_qh where price_ct_kwh < 0").fetchone()[0] > 100
    )


def test_prices_come_from_the_sample_files_not_from_the_real_data(built):
    work, _, _ = built
    chunk = json.loads(
        next((SAMPLE_DIR / "bronze" / "smard").rglob("1776636000000.json")).read_text()
    )
    ts, value = chunk["series"][5]
    con = duckdb.connect(str(work / "gold" / "energy.duckdb"), read_only=True)
    got = con.execute(
        f"select price_eur_mwh from fact_price_qh where ts_utc = epoch_ms({int(ts)}::bigint)"
    ).fetchone()
    assert got == (value,)


def test_households_and_billing_run_on_the_sample(built):
    work, n_days, _ = built
    con = duckdb.connect(str(work / "gold" / "energy.duckdb"), read_only=True)
    cfg = load_config()
    ctx = load_gold_context(con, work / "silver" / "silver_load_opsd_qh.parquet")
    assert ctx.n == n_days * 96
    households = build_households(cfg, ctx)
    assert [h.id for h in households] == ["A_standard", "B_ev", "C_heat_pump", "D_full"]
    results = run_baseline(cfg, ctx, households)
    assert len(results) == 2 + 3 * 6 and all(r.cost_ct.shape == (ctx.n,) for r in results)


def test_price_refresh_build_works_on_a_machine_without_the_other_gold_tables(tmp_path):
    """Regression: on a fresh CI runner `dbt build --select +fact_price_qh` ran tests that refer
    to tables it does not build (gold_row_counts, relationships to fact_load_profile_qh) and
    failed every day. Reproduced here without network: sample prices, nothing else built."""
    shutil.copytree(SAMPLE_DIR / "bronze", tmp_path / "bronze")
    window = json.loads((SAMPLE_DIR / "window.json").read_text())
    env = {
        **os.environ,
        "ETS_DATA_DIR": str(tmp_path),
        "ETS_SIM_START": window["start"],
        "ETS_SIM_END": window["end"],
    }
    code = (
        "from ets.config import load_config; from ets.refresh import DBT_SELECTION; "
        "from ets.transform import run_dbt; run_dbt(load_config(), 'build', extra=DBT_SELECTION)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], env=env, cwd=ROOT, capture_output=True, text=True, timeout=600
    )
    assert result.returncode == 0, result.stdout[-2500:] + result.stderr[-1500:]
    con = duckdb.connect(str(tmp_path / "gold" / "energy.duckdb"), read_only=True)
    tables = {r[0] for r in con.execute("show tables").fetchall()}
    assert {"dim_time", "fact_price_qh"} <= tables
    assert not tables & {"fact_heat_qh", "fact_load_profile_qh"}  # really a price-only build

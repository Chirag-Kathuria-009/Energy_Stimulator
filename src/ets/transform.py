"""Phase 2b runner: staging, dbt build, gold checks. dbt gets its parameters from config."""

import json
import os
import subprocess
import sys
from pathlib import Path

import duckdb

from ets.config import AppConfig
from ets.ingest import manifest
from ets.profiles.bdew_staging import write_bdew_long_csv
from ets.profiles.calendar import HOLIDAY_SEED
from ets.quality.checks import run_gold_checks
from ets.settings import (
    BRONZE,
    DATA_DIR,
    DBT_DIR,
    GOLD_DB,
    N_QH,
    SIM_END_UTC,
    SIM_START_UTC,
)

STAGING = DATA_DIR / "staging"
_FMT = "%Y-%m-%d %H:%M:%S"


def dbt_vars(cfg: AppConfig) -> dict:
    """Everything the SQL needs from config/*.yaml and settings.py (single source of truth)."""
    s = cfg.sources
    return {
        "bronze_dir": BRONZE.as_posix(),
        "staging_dir": STAGING.as_posix(),
        "smard_price_filter": s.smard_price_filter,
        "smard_resolution": s.smard_resolution,
        "sim_start_utc": SIM_START_UTC.strftime(_FMT),
        "sim_end_utc": SIM_END_UTC.strftime(_FMT),
        "qh_start_utc": "2025-09-30 22:00:00",  # first true 15-minute day-ahead prices (2025-10-01)
        "n_qh": N_QH,
        "opsd_channels": s.opsd_channels,
        "opsd_max_kwh_per_qh": s.opsd_max_kwh_per_qh,
        "when2heat_columns": s.when2heat_columns,
        "when2heat_season_start_year": s.when2heat_season_start_year,
    }


def stage_bdew() -> Path:
    """Convert the bronze BDEW workbook to the long CSV that dbt reads. Without a bronze entry
    (the small CI sample ships the CSV directly) an existing staging file is used as it is."""
    target = STAGING / "bdew_profiles_long.csv"
    entries = manifest.entries("bdew")
    if entries:
        return write_bdew_long_csv(BRONZE / entries[0]["path"], target)
    if target.exists():
        return target
    raise FileNotFoundError(
        "no BDEW workbook in the manifest and no staged profile CSV: run `ets ingest`"
    )


def run_dbt(cfg: AppConfig, command: str = "build", extra: list[str] | None = None) -> None:
    """Run `dbt <command>` in a subprocess (so the DuckDB file lock is released afterwards)."""
    GOLD_DB.parent.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "silver").mkdir(parents=True, exist_ok=True)
    args = [
        sys.executable,
        "-c",
        "import sys; from dbt.cli.main import cli; cli()",
        command,
        "--project-dir", str(DBT_DIR),
        "--profiles-dir", str(DBT_DIR),
        "--vars", json.dumps(dbt_vars(cfg)),
        *(extra or []),
    ]  # fmt: skip
    env = {**os.environ, "ETS_DATA_DIR": str(DATA_DIR)}  # absolute: dbt runs in the dbt directory
    subprocess.run(args, check=True, cwd=DBT_DIR, env=env)


def transform(cfg: AppConfig) -> list[str]:
    """Stage BDEW, `dbt build` (seed + run + test), then gold checks. Returns problems."""
    if not HOLIDAY_SEED.exists():
        return [f"{HOLIDAY_SEED} missing: run `ets seed-holidays` first"]
    stage_bdew()
    run_dbt(cfg)
    con = duckdb.connect(str(GOLD_DB), read_only=True)
    try:
        return run_gold_checks(con)
    finally:
        con.close()

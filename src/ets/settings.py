"""Constants. The only input is the environment, so CI and tests can point the whole pipeline at
a small sample: ETS_DATA_DIR (where bronze/silver/gold live), ETS_SIM_START and ETS_SIM_END (the
simulation window, ISO dates, both inclusive). Nothing here reads files or the network."""

import os
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.environ.get("ETS_DATA_DIR") or ROOT / "data").resolve()
BRONZE = DATA_DIR / "bronze"
SILVER = DATA_DIR / "silver"
GOLD_DB = DATA_DIR / "gold" / "energy.duckdb"
CONFIG_DIR = ROOT / "config"
DBT_DIR = ROOT / "dbt"

TZ_LOCAL = "Europe/Berlin"
QH_HOURS = 0.25

# Simulation year in local time, both dates inclusive. First full year of true
# quarter-hour day-ahead prices (SMARD repeats hourly values before 2025-10-01,
# see DECISIONS.md item 2).
SIM_START: date = date.fromisoformat(os.environ.get("ETS_SIM_START") or "2025-10-01")
SIM_END: date = date.fromisoformat(os.environ.get("ETS_SIM_END") or "2026-09-30")

_TZ = ZoneInfo(TZ_LOCAL)
_UTC = ZoneInfo("UTC")


def _local_midnight_utc(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=_TZ).astimezone(_UTC)


# Half-open UTC interval [SIM_START_UTC, SIM_END_UTC): local midnight of the first day
# to local midnight after the last day. UTC has no DST gaps, so N_QH = 96 * days
# (DST changes only move local clock time, not the UTC count).
SIM_START_UTC: datetime = _local_midnight_utc(SIM_START)
SIM_END_UTC: datetime = _local_midnight_utc(SIM_END + timedelta(days=1))
N_QH: int = int((SIM_END_UTC - SIM_START_UTC).total_seconds() // 900)

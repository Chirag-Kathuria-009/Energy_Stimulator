"""Constants only. Nothing here reads files or the network."""

from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
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
SIM_START: date = date(2025, 10, 1)
SIM_END: date = date(2026, 9, 30)

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

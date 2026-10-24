"""Daily price refresh: fetch the newest SMARD price chunks, rebuild the price models, check them.

Runs unattended (GitHub Actions) and has two jobs: keep the price data current and notice early
when SMARD changes its format. It needs nothing but the internet: on a fresh machine it fetches
the price chunks of the simulation window (about 1 MB; chunks already on disk are skipped) and
builds only the models upstream of `fact_price_qh`.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from ets.config import AppConfig
from ets.ingest import smard
from ets.quality.checks import run_gold_checks
from ets.settings import BRONZE, GOLD_DB, SIM_START_UTC
from ets.transform import run_dbt

PRICE_TABLES = ("dim_time", "fact_price_qh")


class SourceFormatError(ValueError):
    """SMARD returned something that is not the chunk layout this pipeline was built for."""


def validate_chunk(path: Path) -> int:
    """Check one downloaded chunk against the layout the SQL relies on; return its row count.

    Layout (DECISIONS.md #1): {"series": [[timestamp_ms, value_or_null], ...]}.
    """
    try:
        series = json.loads(path.read_text(encoding="utf-8"))["series"]
    except (KeyError, ValueError) as e:
        raise SourceFormatError(f"{path.name}: no 'series' list ({e!r})") from e
    if not isinstance(series, list) or not series:
        raise SourceFormatError(f"{path.name}: empty or malformed 'series'")
    for row in series:
        ok = isinstance(row, list) and len(row) == 2 and isinstance(row[0], int)
        if not ok or not (row[1] is None or isinstance(row[1], int | float)):
            raise SourceFormatError(f"{path.name}: unexpected row {row!r}")
    return len(series)


def refresh(
    cfg: AppConfig, bronze: Path = BRONZE, now: datetime | None = None
) -> tuple[list[Path], list[str]]:
    """Fetch, validate, rebuild `+fact_price_qh`, run the checks. Returns (chunks, problems).

    The newest chunk is always downloaded again (it fills up during the week), so the refresh
    also picks up tomorrow's day-ahead prices as soon as SMARD publishes them.
    """
    s = cfg.sources
    now = now or datetime.now(UTC)
    chunks = smard.fetch_range(
        s.smard_base,
        s.smard_price_filter,
        s.smard_region,
        s.smard_resolution,
        min(SIM_START_UTC, now),
        now,
        bronze=bronze,
    )
    if not chunks:
        raise SourceFormatError("SMARD returned no price chunks for the requested range")
    for chunk in chunks:
        validate_chunk(chunk)
    run_dbt(cfg, "build", extra=["--select", "+fact_price_qh"])
    con = duckdb.connect(str(GOLD_DB), read_only=True)
    try:
        problems = run_gold_checks(con, tables=PRICE_TABLES)
    finally:
        con.close()
    return chunks, problems

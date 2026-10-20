"""German calendar helpers. Phase 2a: the holiday seed that dbt loads.

SQL has no German holiday calendar, so it is computed once in Python and loaded as a seed.
"""

import csv
from pathlib import Path

import holidays

from ets.settings import DBT_DIR

HOLIDAY_SEED = DBT_DIR / "seeds" / "holidays_de.csv"


def write_holiday_seed(start_year: int, end_year: int, path: Path = HOLIDAY_SEED) -> Path:
    """Write `date,name` for national German holidays in [start_year, end_year].

    ASSUMPTION: national holidays only (no state-specific days such as Reformationstag).
    """
    cal = holidays.Germany(years=range(start_year, end_year + 1))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["date", "name"])
        for d in sorted(cal):
            w.writerow([d.isoformat(), cal[d]])
    return path

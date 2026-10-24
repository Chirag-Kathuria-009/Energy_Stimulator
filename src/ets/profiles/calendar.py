"""German calendar helpers. Phase 2a: the holiday seed that dbt loads.

SQL has no German holiday calendar, so it is computed once in Python and loaded as a seed.
"""

import csv
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

import holidays
import numpy as np

from ets.settings import DBT_DIR

if TYPE_CHECKING:
    from ets.simulate.context import GoldContext

HOLIDAY_SEED = DBT_DIR / "seeds" / "holidays_de.csv"
QH_PER_DAY = 96


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


def day_class(weekday_iso: int, is_holiday: bool) -> str:
    """Weekday class used to pick a source day: 'so' (Sunday/holiday), 'sa' or 'wd'."""
    if weekday_iso == 7 or is_holiday:
        return "so"
    return "sa" if weekday_iso == 6 else "wd"


def _doy(d: date) -> int:
    """Day of year on a non-leap calendar, so 1 March is the same day number in every year."""
    return (
        (date(2001, 2, 28) if (d.month, d.day) == (2, 29) else d.replace(year=2001))
        .timetuple()
        .tm_yday
    )


def _circular_distance(a: np.ndarray, b: int) -> np.ndarray:
    diff = np.abs(a - b)
    return np.minimum(diff, 365 - diff)


def align_to_year(
    days: dict[date, np.ndarray],
    ctx: "GoldContext",
    *,
    match_daytype: bool = True,
    total: float = 1.0,
) -> np.ndarray:
    """Map source days (from any years) onto the simulation year, quarter-hour by quarter-hour.

    `days` maps a local source date to its 96 values by local clock time (DST days must be left
    out). For every simulation date the source day with the same weekday class (if
    `match_daytype`) and the closest day of year is chosen; ties go to the earlier source date.
    Its values are copied by local clock time. The repeated October hour gets half of the value
    in each copy so daily energy is kept; the skipped March hour is simply absent. The result is
    rescaled to sum to `total`.
    """
    if not days:
        raise ValueError("no source days to align")
    cand_dates = sorted(days)
    matrix = np.vstack([np.asarray(days[d], dtype=float) for d in cand_dates])
    if matrix.shape[1] != QH_PER_DAY:
        raise ValueError(f"source days must have {QH_PER_DAY} values, got {matrix.shape[1]}")
    cand_doy = np.array([_doy(d) for d in cand_dates])
    hol = holidays.Germany(years=range(cand_dates[0].year, cand_dates[-1].year + 1))
    cand_class = np.array([day_class(d.isoweekday(), d in hol) for d in cand_dates])

    target_dates, first_row, inverse = np.unique(
        ctx.date_local, return_index=True, return_inverse=True
    )
    chosen = np.empty(len(target_dates), dtype=int)
    for k, (d64, row) in enumerate(zip(target_dates, first_row, strict=True)):
        d = d64.astype("datetime64[D]").item()
        allowed = np.ones(len(cand_dates), dtype=bool)
        if match_daytype:
            cls = day_class(int(ctx.weekday[row]), bool(ctx.is_holiday[row]))
            allowed = cand_class == cls
            if not allowed.any():
                raise ValueError(f"no source day of class {cls!r} for {d}")
        dist = np.where(allowed, _circular_distance(cand_doy, _doy(d)), 10**6)
        chosen[k] = int(np.argmin(dist))

    out = matrix[chosen[inverse], ctx.qh_of_day]
    keys = inverse.astype(np.int64) * QH_PER_DAY + ctx.qh_of_day
    _, key_inv, key_counts = np.unique(keys, return_inverse=True, return_counts=True)
    out = out / key_counts[key_inv]
    if out.sum() <= 0:
        raise ValueError("aligned profile sums to zero")
    return out * (total / out.sum())

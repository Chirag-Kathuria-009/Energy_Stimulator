"""Reshape the BDEW workbook (H25, P25) into a long CSV that dbt can read.

DuckDB's Excel reader needs a network extension install, so the workbook is converted here with
openpyxl. The bronze workbook is not touched; the CSV is a derived staging file.
"""

import csv
from pathlib import Path

import openpyxl

PROFILES = ("H25", "P25")
DAYTYPES = ("SA", "FT", "WT")  # Saturday, Sunday/holiday, weekday
FIRST_ROW, QH_PER_DAY, FIRST_COL, N_COLS = 5, 96, 3, 36


def write_bdew_long_csv(xlsx: Path, out: Path) -> Path:
    """Write `profile_id,month,daytype,qh_of_day,kwh_per_million`.

    Values are kWh per quarter-hour for a household consuming 1,000,000 kWh/year, not yet
    dynamised (the dynamisation depends on the date and is applied in gold).
    """
    wb = openpyxl.load_workbook(xlsx, data_only=True, read_only=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    rows_written = 0
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["profile_id", "month", "daytype", "qh_of_day", "kwh_per_million"])
        for profile in PROFILES:
            grid = list(
                wb[profile].iter_rows(
                    min_row=3, max_row=FIRST_ROW - 1 + QH_PER_DAY, values_only=True
                )
            )
            months, types, body = grid[0], grid[1], grid[2:]
            if len(body) != QH_PER_DAY:
                raise ValueError(
                    f"{profile}: expected {QH_PER_DAY} quarter-hour rows, got {len(body)}"
                )
            for col in range(FIRST_COL - 1, FIRST_COL - 1 + N_COLS):
                month, daytype = months[col].month, types[col]
                if daytype not in DAYTYPES:
                    raise ValueError(
                        f"{profile}: unexpected day type {daytype!r} in column {col + 1}"
                    )
                for qh, row in enumerate(body):
                    value = row[col]
                    if value is None or value < 0:
                        raise ValueError(
                            f"{profile}: bad value {value!r} at month {month} {daytype} qh {qh}"
                        )
                    w.writerow([profile, month, daytype, qh, value])
                    rows_written += 1
    if rows_written != len(PROFILES) * N_COLS * QH_PER_DAY:
        raise ValueError(f"unexpected row count {rows_written}")
    return out

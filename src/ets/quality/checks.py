"""Schema and consistency checks on the gold tables (pandera on Polars)."""

import duckdb
import pandera.polars as pa
import polars as pl
from pandera.errors import SchemaErrors

from ets.settings import N_QH

PriceSchema = pa.DataFrameSchema(
    {
        "idx": pa.Column(pl.Int32, nullable=False, unique=True),
        "ts_utc": pa.Column(pl.Datetime, nullable=False, unique=True),
        "price_ct_kwh": pa.Column(pl.Float64, pa.Check.in_range(-50, 100), nullable=False),
        "price_eur_mwh": pa.Column(pl.Float64, nullable=False),
        "source_resolution": pa.Column(pl.String, pa.Check.isin(["qh", "h"]), nullable=False),
        "is_estimated": pa.Column(pl.Boolean, nullable=False),
    },
    strict=True,
)

TimeSchema = pa.DataFrameSchema(
    {
        "idx": pa.Column(pl.Int32, nullable=False, unique=True),
        "ts_utc": pa.Column(pl.Datetime, nullable=False, unique=True),
        "ts_local": pa.Column(pl.Datetime, nullable=False),
        "date_local": pa.Column(pl.Date, nullable=False),
        "qh_of_day": pa.Column(pl.Int32, pa.Check.in_range(0, 95), nullable=False),
        "weekday": pa.Column(pl.Int32, pa.Check.in_range(1, 7), nullable=False),
        "is_holiday": pa.Column(pl.Boolean, nullable=False),
        "is_dst_day": pa.Column(pl.Boolean, nullable=False),
        "month": pa.Column(pl.Int32, pa.Check.in_range(1, 12), nullable=False),
        "season": pa.Column(
            pl.String, pa.Check.isin(["winter", "spring", "summer", "autumn"]), nullable=False
        ),
    },
    strict=True,
)

HeatSchema = pa.DataFrameSchema(
    {
        "idx": pa.Column(pl.Int32, nullable=False, unique=True),
        "ts_utc": pa.Column(pl.Datetime, nullable=False, unique=True),
        "heat_space_norm": pa.Column(pl.Float64, pa.Check.ge(0), nullable=False),
        "heat_water_norm": pa.Column(pl.Float64, pa.Check.ge(0), nullable=False),
        "cop_space": pa.Column(pl.Float64, pa.Check.in_range(1, 8), nullable=False),
        "cop_water": pa.Column(pl.Float64, pa.Check.in_range(1, 8), nullable=False),
    },
    strict=True,
)

LoadProfileSchema = pa.DataFrameSchema(
    {
        "idx": pa.Column(pl.Int32, nullable=False),
        "profile_id": pa.Column(pl.String, pa.Check.isin(["H25", "P25"]), nullable=False),
        "kwh": pa.Column(pl.Float64, pa.Check.ge(0), nullable=False),
        "is_estimated": pa.Column(pl.Boolean, nullable=False),
    },
    strict=True,
)

_SCHEMAS = {
    "dim_time": TimeSchema,
    "fact_price_qh": PriceSchema,
    "fact_heat_qh": HeatSchema,
    "fact_load_profile_qh": LoadProfileSchema,
}


def _validate(name: str, df: pl.DataFrame) -> list[str]:
    try:
        _SCHEMAS[name].validate(df, lazy=True)
    except SchemaErrors as e:
        cases = e.failure_cases
        return [
            f"{name}.{row['column']}: {row['check']} failed ({row['failure_case']})"
            for row in cases.head(5).iter_rows(named=True)
        ] + ([f"{name}: {cases.height - 5} more failures"] if cases.height > 5 else [])
    return []


def run_gold_checks(con: duckdb.DuckDBPyConnection, n: int = N_QH) -> list[str]:
    """Validate every gold table; return a list of problems (empty means OK).

    Besides the schemas: all arrays must have the same length N, idx must be exactly 0..N-1 in
    order, and the profile table must hold N rows per profile.
    """
    problems: list[str] = []
    frames: dict[str, pl.DataFrame] = {}
    for name in _SCHEMAS:
        try:
            frames[name] = con.sql(f"select * from {name}").pl()
        except duckdb.Error as e:
            problems.append(f"{name}: cannot read table ({e})")
    for name, df in frames.items():
        problems += _validate(name, df)

    for name in ("dim_time", "fact_price_qh", "fact_heat_qh"):
        if name not in frames:
            continue
        df = frames[name]
        if df.height != n:
            problems.append(f"{name}: {df.height} rows, expected {n}")
        elif not df["idx"].sort().equals(pl.Series("idx", range(n), dtype=pl.Int32)):
            problems.append(f"{name}: idx is not exactly 0..{n - 1}")
    if "fact_load_profile_qh" in frames:
        counts = frames["fact_load_profile_qh"].group_by("profile_id").len()
        for row in counts.iter_rows(named=True):
            if row["len"] != n:
                problems.append(
                    f"fact_load_profile_qh[{row['profile_id']}]: {row['len']} rows, expected {n}"
                )
    if "dim_time" in frames:
        per_day = frames["dim_time"].group_by("date_local").len()["len"]
        bad = per_day.filter(~per_day.is_in([92, 96, 100]))
        if len(bad):
            problems.append(f"dim_time: {len(bad)} local days without 92/96/100 quarter-hours")
    return problems

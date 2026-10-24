"""Gold tables as NumPy arrays, loaded once and used by Phases 3-5 (array arithmetic)."""

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

import duckdb
import numpy as np

from ets.profiles.calendar import QH_PER_DAY, align_to_year
from ets.settings import SILVER

OPSD_SILVER = SILVER / "silver_load_opsd_qh.parquet"


@dataclass
class GoldContext:
    """Every array has length n (quarter-hours of the simulation year), ordered by idx."""

    n: int
    ts_utc: np.ndarray  # datetime64[us], naive UTC
    date_local: np.ndarray  # datetime64[D]
    qh_of_day: np.ndarray  # local clock quarter-hour 0..95 (repeated October hour appears twice)
    weekday: np.ndarray  # ISO 1 = Monday .. 7 = Sunday
    month: np.ndarray
    is_holiday: np.ndarray
    price_ct: np.ndarray
    heat_space_norm: np.ndarray  # sums to 1 over the year
    heat_water_norm: np.ndarray  # sums to 1 over the year
    cop_space: np.ndarray
    cop_water: np.ndarray
    profiles: dict[str, np.ndarray] = field(default_factory=dict)  # kWh per 1,000 kWh/year
    pv_shape: np.ndarray | None = None  # sums to 1 over the year

    def idx_of_local(self, local: datetime) -> int:
        """Array position of a timezone-aware local datetime (may lie outside [0, n))."""
        utc = local.astimezone(UTC).replace(tzinfo=None)
        delta = np.datetime64(utc, "us") - self.ts_utc[0]
        minutes = delta / np.timedelta64(1, "m")
        return int(minutes // 15)

    def dates(self) -> list[date]:
        """Distinct local dates of the simulation year in order."""
        return [d.astype("datetime64[D]").item() for d in np.unique(self.date_local)]


def _column(df, name: str, dtype=None) -> np.ndarray:
    if df[name].null_count():
        raise ValueError(
            f"column {name} contains nulls; run `ets transform` and check the gold tables"
        )
    arr = df[name].to_numpy()
    return arr.astype(dtype) if dtype is not None else arr


def _opsd_pv_days(parquet: Path) -> dict[str, dict[date, np.ndarray]]:
    """Complete 96-quarter-hour local days per site for the PV channel (DST days have 92/100)."""
    rows = duckdb.sql(
        f"""
        with pv as (
            select site, date_local, kwh,
                   (ts_utc at time zone 'UTC') at time zone 'Europe/Berlin' as local_ts
            from read_parquet('{parquet.as_posix()}') where channel = 'pv'
        ),
        full_days as (
            select site, date_local from pv group by site, date_local
            having count(*) = {QH_PER_DAY}
        )
        select site, date_local, hour(local_ts) * 4 + minute(local_ts) // 15 as qh, kwh
        from pv join full_days using (site, date_local)
        order by site, date_local, qh
        """
    ).fetchall()
    out: dict[str, dict[date, np.ndarray]] = {}
    for site, d, qh, kwh in rows:
        out.setdefault(site, {}).setdefault(d, np.zeros(QH_PER_DAY))[qh] = kwh
    return out


def build_pv_shape(ctx: GoldContext, opsd_parquet: Path = OPSD_SILVER) -> np.ndarray:
    """Normalised PV generation shape for the simulation year (sums to 1).

    Each OPSD site is aligned separately (closest day of year, weather-driven so weekday class is
    ignored), normalised to sum 1, and the sites are averaged (DECISIONS.md #14).
    """
    per_site = _opsd_pv_days(opsd_parquet)
    if not per_site:
        raise ValueError(f"no complete PV days in {opsd_parquet}")
    shapes = [align_to_year(days, ctx, match_daytype=False) for days in per_site.values()]
    mean = np.mean(shapes, axis=0)
    return mean / mean.sum()


def load_gold_context(
    con: duckdb.DuckDBPyConnection, opsd_parquet: Path | None = OPSD_SILVER
) -> GoldContext:
    """Read the four gold tables ordered by idx. The PV shape comes from the OPSD silver parquet
    (the one input that is not a gold table, DECISIONS.md #19); pass None to skip it."""
    t = con.sql("select * from dim_time order by idx").pl()
    p = con.sql("select idx, price_ct_kwh from fact_price_qh order by idx").pl()
    h = con.sql("select * from fact_heat_qh order by idx").pl()
    lp = con.sql("select profile_id, kwh from fact_load_profile_qh order by profile_id, idx").pl()
    n = t.height
    for name, df in (("fact_price_qh", p), ("fact_heat_qh", h)):
        if df.height != n or not (df["idx"].to_numpy() == np.arange(n)).all():
            raise ValueError(f"{name} does not line up with dim_time ({df.height} vs {n} rows)")
    profiles = {
        pid: _column(lp.filter(lp["profile_id"] == pid), "kwh")
        for pid in lp["profile_id"].unique().sort().to_list()
    }
    for pid, arr in profiles.items():
        if len(arr) != n:
            raise ValueError(f"profile {pid} has {len(arr)} rows, expected {n}")
    ctx = GoldContext(
        n=n,
        ts_utc=_column(t, "ts_utc", "datetime64[us]"),
        date_local=_column(t, "date_local", "datetime64[D]"),
        qh_of_day=_column(t, "qh_of_day", np.int64),
        weekday=_column(t, "weekday", np.int64),
        month=_column(t, "month", np.int64),
        is_holiday=_column(t, "is_holiday", bool),
        price_ct=_column(p, "price_ct_kwh", float),
        heat_space_norm=_column(h, "heat_space_norm", float),
        heat_water_norm=_column(h, "heat_water_norm", float),
        cop_space=_column(h, "cop_space", float),
        cop_water=_column(h, "cop_water", float),
        profiles=profiles,
    )
    if opsd_parquet is not None:
        ctx.pv_shape = build_pv_shape(ctx, opsd_parquet)
    return ctx

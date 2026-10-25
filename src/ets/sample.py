"""Cut a small, committed sample out of the real bronze data so CI never depends on live APIs.

The sample is a two-week simulation window plus the slices of every source that the pipeline
needs for it. CI points the pipeline at a copy of it with ETS_DATA_DIR, ETS_SIM_START and
ETS_SIM_END (`ets transform` then builds the whole silver/gold layer from it).
"""

import json
import shutil
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import polars as pl

from ets.config import AppConfig
from ets.settings import BRONZE, DATA_DIR, ROOT, TZ_LOCAL

SAMPLE_DIR = ROOT / "data" / "sample"
DEFAULT_START, DEFAULT_END = date(2026, 4, 20), date(2026, 5, 3)  # 336 negative-price quarter-hours
OPSD_WINDOW = ("2016-05-02", "2016-05-22")  # three weeks in which all OPSD sites have data
_WEEK = timedelta(days=7)
_TZ = ZoneInfo(TZ_LOCAL)


def _utc_bounds(start: date, end: date) -> tuple[datetime, datetime]:
    lo = datetime.combine(start, time(0), tzinfo=_TZ)
    hi = datetime.combine(end + timedelta(days=1), time(0), tzinfo=_TZ)
    return lo, hi


def _slice_csv(
    src: Path, dest: Path, columns: list[str], lo: str, hi: str, *, separator: str
) -> int:
    """Rows with lo <= utc_timestamp < hi (ISO strings compare correctly), selected columns, with
    the original text of every value (so a decimal comma stays a decimal comma)."""
    df = pl.read_csv(
        src, separator=separator, infer_schema=False, columns=["utc_timestamp", *columns]
    )
    df = df.filter((pl.col("utc_timestamp") >= lo) & (pl.col("utc_timestamp") < hi))
    dest.parent.mkdir(parents=True, exist_ok=True)
    df.write_csv(dest, separator=separator)
    return df.height


def make_sample(
    cfg: AppConfig,
    start: date = DEFAULT_START,
    end: date = DEFAULT_END,
    bronze: Path = BRONZE,
    out: Path = SAMPLE_DIR,
) -> dict[str, int]:
    """Write the sample under `out` and return the number of files or rows per source."""
    s = cfg.sources
    lo_dt, hi_dt = _utc_bounds(start, end)
    counts: dict[str, int] = {}
    if out.exists():
        shutil.rmtree(out)

    # SMARD: the raw weekly chunk files that overlap the window, byte for byte.
    price_dir = bronze / "smard" / str(s.smard_price_filter) / s.smard_resolution
    kept = 0
    for f in sorted(price_dir.glob("*.json")):
        chunk_start = datetime.fromtimestamp(int(f.stem) / 1000, tz=lo_dt.tzinfo)
        if chunk_start < hi_dt and chunk_start + _WEEK + timedelta(hours=1) > lo_dt:
            target = out / "bronze" / "smard" / price_dir.relative_to(bronze / "smard") / f.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(f, target)
            kept += 1
    counts["smard_chunks"] = kept

    # OPSD: the configured channels for three weeks.
    counts["opsd_rows"] = _slice_csv(
        bronze / "opsd" / "household_data_15min.csv",
        out / "bronze" / "opsd" / "household_data_15min.csv",
        list(s.opsd_channels.values()),
        *(f"{d}T00:00:00Z" for d in OPSD_WINDOW),
        separator=",",
    )

    # when2heat: the weather season the simulation window maps onto (Oct-Dec -> start year,
    # Jan-Sep -> next year), one day of margin on both sides, only the configured columns.
    def source_day(d: date) -> date:
        y0 = s.when2heat_season_start_year
        return d.replace(year=y0 if d.month >= 10 else y0 + 1)

    w_lo = (source_day(start) - timedelta(days=1)).isoformat()
    w_hi = (source_day(end) + timedelta(days=2)).isoformat()
    counts["when2heat_rows"] = _slice_csv(
        bronze / "when2heat" / "when2heat.csv",
        out / "bronze" / "when2heat" / "when2heat.csv",
        list(s.when2heat_columns.values()),
        f"{w_lo}T00:00:00Z",
        f"{w_hi}T00:00:00Z",
        separator=";",
    )

    # BDEW: the staged long CSV (the workbook itself is not committed).
    staged = DATA_DIR / "staging" / "bdew_profiles_long.csv"
    (out / "staging").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(staged, out / "staging" / "bdew_profiles_long.csv")
    counts["bdew_rows"] = sum(1 for _ in staged.open(encoding="utf-8")) - 1

    (out / "window.json").write_text(
        json.dumps({"start": start.isoformat(), "end": end.isoformat()}), encoding="utf-8"
    )
    (out / "README.md").write_text(_README.format(start=start, end=end), encoding="utf-8")
    return counts


_README = """# CI sample

A small slice of every source so CI can build the whole silver and gold layer without live APIs.
Regenerate with `uv run ets make-sample` (needs the full bronze data from `ets ingest`).

- Simulation window: {start} to {end} (local dates, inclusive); `window.json` holds the dates.
- `bronze/smard/`: the raw weekly price chunks of SMARD (Bundesnetzagentur | SMARD.de, CC BY 4.0).
- `bronze/opsd/`, `bronze/when2heat/`: three weeks / a few days of the Open Power System Data files,
  only the configured columns (CC BY 4.0, https://open-power-system-data.org).
- `staging/bdew_profiles_long.csv`: BDEW standard load profiles H25 and P25 reshaped by
  `ets.profiles.bdew_staging` (source: BDEW, https://www.bdew.de/energie/standardlastprofile-strom/).

Use (as CI does):

    ETS_DATA_DIR=/tmp/ets ETS_SIM_START={start} ETS_SIM_END={end} \\
        uv run ets transform      # after copying bronze/ and staging/ into $ETS_DATA_DIR

Annual figures computed on this sample are meaningless (the fixed fees still count 12 months).
"""

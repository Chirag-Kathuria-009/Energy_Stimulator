"""Open Power System Data household_data 15-minute CSV -> bronze, unchanged."""

from pathlib import Path

from ets.config import AppConfig
from ets.ingest import http, manifest
from ets.settings import BRONZE


def download_household_data(cfg: AppConfig, bronze: Path = BRONZE) -> Path:
    """Download once. Skipped when the file exists and its sha256 matches the manifest."""
    dest = bronze / "opsd" / "household_data_15min.csv"
    if manifest.is_current(dest, bronze):
        return dest
    url = cfg.sources.opsd_household_15min_url
    http.download(url, dest)
    manifest.record("opsd", dest, url, bronze=bronze)
    return dest

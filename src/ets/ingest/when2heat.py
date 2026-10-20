"""Open Power System Data when2heat CSV -> bronze, unchanged.

328 MB, ';' separated, decimal comma (DECISIONS.md #4).
"""

from pathlib import Path

from ets.config import AppConfig
from ets.ingest import http, manifest
from ets.settings import BRONZE


def download_when2heat(cfg: AppConfig, bronze: Path = BRONZE) -> Path:
    """Download once. Skipped when the file exists and its sha256 matches the manifest."""
    dest = bronze / "when2heat" / "when2heat.csv"
    if manifest.is_current(dest, bronze):
        return dest
    url = cfg.sources.when2heat_url
    http.download(url, dest)
    manifest.record("when2heat", dest, url, bronze=bronze)
    return dest

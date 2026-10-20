"""BDEW standard load profiles (H25/P25 workbook) -> bronze.

The plan assumed a manual browser download. The direct URL works (DECISIONS.md #5), so the file is
fetched automatically when it is not already at `bdew_local_path`; a manual download still works.
"""

import shutil
from pathlib import Path

import httpx

from ets.config import AppConfig
from ets.ingest import http, manifest
from ets.settings import BRONZE, ROOT

_MANUAL_HELP = (
    "BDEW file not found at {path} and the automatic download from {url} failed ({err}).\n"
    "Download it in a browser from https://www.bdew.de/energie/standardlastprofile-strom/ "
    "(workbook 'Repraesentative_Profile_BDEW_H25_G25_L25_P25_S25_Veroeffentlichung.xlsx') "
    "and save it as {path}."
)


def register_bdew_file(cfg: AppConfig, bronze: Path = BRONZE, root: Path = ROOT) -> Path:
    src = root / cfg.sources.bdew_local_path
    dest = bronze / "bdew" / src.name
    if manifest.is_current(dest, bronze):
        return dest
    if not src.exists():
        try:
            http.download(cfg.sources.bdew_download_url, src)
        except (httpx.HTTPError, OSError) as e:
            raise FileNotFoundError(
                _MANUAL_HELP.format(path=src, url=cfg.sources.bdew_download_url, err=repr(e))
            ) from e
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    manifest.record(
        "bdew",
        dest,
        cfg.sources.bdew_download_url,
        {"copied_from": src.relative_to(root).as_posix()},
        bronze=bronze,
    )
    return dest

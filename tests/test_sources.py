import httpx
import pytest

from ets.config import load_config
from ets.ingest import bdew, manifest, opsd


def test_bdew_copies_local_file_into_bronze(tmp_path):
    cfg = load_config()
    src = tmp_path / cfg.sources.bdew_local_path
    src.parent.mkdir(parents=True)
    src.write_bytes(b"xlsx-bytes")
    bronze = tmp_path / "bronze"

    dest = bdew.register_bdew_file(cfg, bronze=bronze, root=tmp_path)
    assert dest.read_bytes() == b"xlsx-bytes"
    (entry,) = manifest.entries("bdew", bronze)
    assert entry["extra"]["copied_from"] == cfg.sources.bdew_local_path


def test_bdew_downloads_when_local_file_missing(tmp_path, monkeypatch):
    cfg = load_config()

    def fake_download(url, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"fetched")
        return dest

    monkeypatch.setattr(bdew.http, "download", fake_download)
    dest =bdew.register_bdew_file(cfg, bronze=tmp_path / "bronze", root=tmp_path)
    assert dest.read_bytes() == b"fetched"


def test_bdew_missing_and_download_fails_explains_manual_step(tmp_path, monkeypatch):
    def boom(url, dest):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(bdew.http, "download", boom)
    with pytest.raises(FileNotFoundError, match="save it as"):
        bdew.register_bdew_file(load_config(), bronze=tmp_path / "bronze", root=tmp_path)


def test_opsd_skips_download_when_file_is_current(tmp_path, monkeypatch):
    cfg = load_config()
    calls = []

    def fake_download(url, dest):
        calls.append(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"csv")
        return dest

    monkeypatch.setattr(opsd.http, "download", fake_download)
    opsd.download_household_data(cfg, bronze=tmp_path)
    opsd.download_household_data(cfg, bronze=tmp_path)
    assert len(calls) == 1  # second call: checksum matches the manifest
    (tmp_path / "opsd" / "household_data_15min.csv").write_bytes(b"corrupt")
    opsd.download_household_data(cfg, bronze=tmp_path)
    assert len(calls) == 2  # checksum mismatch: downloaded again

"""SMARD (Bundesnetzagentur) chart_data API -> bronze JSON files, saved unchanged.

URL pattern verified in DECISIONS.md #1: weekly chunks, index lists chunk start timestamps (ms).
"""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ets.config import AppConfig
from ets.ingest import http, manifest
from ets.settings import BRONZE, SIM_START_UTC

WEEK_MS = 7 * 24 * 3600 * 1000


def index_url(base: str, filter_id: int, region: str, resolution: str) -> str:
    return f"{base}/{filter_id}/{region}/index_{resolution}.json"


def chunk_url(base: str, filter_id: int, region: str, resolution: str, ts_ms: int) -> str:
    return f"{base}/{filter_id}/{region}/{filter_id}_{region}_{resolution}_{ts_ms}.json"


def get_index(base: str, filter_id: int, region: str, resolution: str) -> list[int]:
    """Chunk start timestamps (ms), sorted. The key is `timestamps` (verified)."""
    payload = http.get_json(index_url(base, filter_id, region, resolution))
    return sorted(payload["timestamps"])


def fetch_chunk(base: str, filter_id: int, region: str, resolution: str, ts_ms: int) -> bytes:
    """One chunk exactly as the server sent it. Returned as bytes so bronze stays byte-identical."""
    return http.get_bytes(chunk_url(base, filter_id, region, resolution, ts_ms))


def bronze_path(filter_id: int, resolution: str, ts_ms: int, bronze: Path = BRONZE) -> Path:
    return bronze / "smard" / str(filter_id) / resolution / f"{ts_ms}.json"


def save_bronze(
    payload: bytes,
    filter_id: int,
    resolution: str,
    ts_ms: int,
    *,
    url: str,
    extra: dict | None = None,
    bronze: Path = BRONZE,
) -> Path:
    """Write the raw bytes (temp file + rename) and record them in the manifest."""
    path = bronze_path(filter_id, resolution, ts_ms, bronze)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(payload)
    os.replace(tmp, path)
    manifest.record(
        "smard",
        path,
        url,
        {"filter_id": filter_id, "resolution": resolution, "ts_ms": ts_ms, **(extra or {})},
        bronze=bronze,
    )
    return path


def _is_final(path: Path, chunk_end_ms: int, bronze: Path) -> bool:
    """True if the file on disk was downloaded after the chunk's week had ended.

    A chunk fetched while it was still the newest week may hold null future slots, so it must be
    fetched again later even though it is no longer the newest.
    """
    if not manifest.is_current(path, bronze):
        return False
    rel = path.resolve().relative_to(bronze.resolve()).as_posix()
    for e in manifest.entries("smard", bronze):
        if e["path"] == rel:
            fetched = datetime.fromisoformat(e["fetched_at_utc"])
            return fetched.timestamp() * 1000 >= chunk_end_ms
    return False


def fetch_range(
    base: str,
    filter_id: int,
    region: str,
    resolution: str,
    start: datetime,
    end: datetime,
    *,
    force_latest: bool = True,
    bronze: Path = BRONZE,
) -> list[Path]:
    """Download every chunk overlapping [start, end]; idempotent.

    A chunk starting at `ts` covers until the next timestamp in the index (the last one covers a
    week). Chunks already on disk and final are skipped; the newest chunk is always refreshed.
    """
    ts_list = get_index(base, filter_id, region, resolution)
    start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    paths: list[Path] = []
    for i, ts in enumerate(ts_list):
        chunk_end = ts_list[i + 1] if i + 1 < len(ts_list) else ts + WEEK_MS
        if not (ts < end_ms and chunk_end > start_ms):
            continue
        path = bronze_path(filter_id, resolution, ts, bronze)
        newest = i == len(ts_list) - 1
        if not (newest and force_latest) and _is_final(path, chunk_end, bronze):
            paths.append(path)
            continue
        payload = fetch_chunk(base, filter_id, region, resolution, ts)
        url = chunk_url(base, filter_id, region, resolution, ts)
        paths.append(
            save_bronze(
                payload,
                filter_id,
                resolution,
                ts,
                url=url,
                extra={"region": region, "chunk_end_ms": chunk_end},
                bronze=bronze,
            )
        )
    return paths


def ingest_smard(
    cfg: AppConfig, bronze: Path = BRONZE, now: datetime | None = None
) -> tuple[list[Path], list[str]]:
    """Price series (required) from one year before the simulation year until now; context series
    (optional) from the start of the simulation year. Returns (price paths, warnings).

    A failure of the price series raises. A failure of an optional context series is reported
    as a warning because no later phase depends on it yet.
    """
    s = cfg.sources
    now = now or datetime.now(UTC)
    price_start = SIM_START_UTC - timedelta(days=365)
    prices = fetch_range(
        s.smard_base,
        s.smard_price_filter,
        s.smard_region,
        s.smard_resolution,
        price_start,
        now,
        bronze=bronze,
    )
    warnings: list[str] = []
    for filter_id in s.smard_context_filters:
        try:
            fetch_range(
                s.smard_base,
                filter_id,
                s.smard_context_region,
                s.smard_resolution,
                SIM_START_UTC,
                now,
                bronze=bronze,
            )
        except Exception as e:  # optional series; keep ingesting the required sources
            warnings.append(f"optional SMARD series {filter_id} failed: {e!r}")
    return prices, warnings

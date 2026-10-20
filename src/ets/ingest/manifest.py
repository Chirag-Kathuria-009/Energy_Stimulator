"""Bronze manifest: what was fetched, when, from where, with a checksum.

Stored as `data/bronze/_manifest.json`. Paths are relative to the bronze directory (posix style)
so the manifest survives moving the repo. Writes go through a temp file and a rename.
"""

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from ets.config import AppConfig
from ets.settings import BRONZE, SIM_END_UTC, SIM_START_UTC

MANIFEST_NAME = "_manifest.json"
REQUIRED_SOURCES = ("smard", "opsd", "when2heat", "bdew")


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(1 << 20):
            h.update(block)
    return h.hexdigest()


def _manifest_path(bronze: Path) -> Path:
    return bronze / MANIFEST_NAME


def _load(bronze: Path) -> list[dict]:
    p = _manifest_path(bronze)
    if not p.exists():
        return []
    return json.loads(p.read_text(encoding="utf-8"))["entries"]


def _save(bronze: Path, entries: list[dict]) -> None:
    p = _manifest_path(bronze)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps({"entries": entries}, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, p)


def _rel(path: Path, bronze: Path) -> str:
    return path.resolve().relative_to(bronze.resolve()).as_posix()


def record(
    source: str,
    path: Path,
    url: str,
    extra: dict | None = None,
    bronze: Path = BRONZE,
) -> None:
    """Add or replace the entry for `path` (one entry per file)."""
    rel = _rel(path, bronze)
    entry = {
        "source": source,
        "path": rel,
        "url": url,
        "fetched_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "sha256": sha256_of(path),
        "bytes": path.stat().st_size,
        "extra": extra or {},
    }
    entries = [e for e in _load(bronze) if e["path"] != rel]
    entries.append(entry)
    entries.sort(key=lambda e: e["path"])
    _save(bronze, entries)


def entries(source: str, bronze: Path = BRONZE) -> list[dict]:
    return [e for e in _load(bronze) if e["source"] == source]


def is_current(path: Path, bronze: Path = BRONZE) -> bool:
    """True when the file exists and its checksum matches the manifest entry."""
    if not path.exists():
        return False
    rel = _rel(path, bronze)
    return any(e["path"] == rel and e["sha256"] == sha256_of(path) for e in _load(bronze))


def _price_timestamps(files: list[Path]) -> set[int]:
    """Timestamps (ms) with a non-null value in the given SMARD chunk files."""
    out: set[int] = set()
    for f in files:
        for ts, value in json.loads(f.read_text(encoding="utf-8"))["series"]:
            if value is not None:
                out.add(ts)
    return out


def check_coverage(
    cfg: AppConfig,
    bronze: Path = BRONZE,
    start: datetime = SIM_START_UTC,
    end: datetime = SIM_END_UTC,
) -> list[str]:
    """Problems that block Phase 2: a missing source, a missing file, or a gap in the prices.

    `[start, end)` must be fully covered by non-null quarter-hour price values.
    """
    problems: list[str] = []
    for source in REQUIRED_SOURCES:
        es = entries(source, bronze)
        if not es:
            problems.append(f"source '{source}' has no manifest entry")
            continue
        for e in es:
            if not (bronze / e["path"]).exists():
                problems.append(f"{source}: file listed in manifest is missing: {e['path']}")

    price_files = [
        bronze / e["path"]
        for e in entries("smard", bronze)
        if e["extra"].get("filter_id") == cfg.sources.smard_price_filter
        and e["extra"].get("resolution") == cfg.sources.smard_resolution
        and (bronze / e["path"]).exists()
    ]
    if price_files:
        have = _price_timestamps(price_files)
        step = 900_000
        first = int(start.timestamp() * 1000)
        last = int(end.timestamp() * 1000)
        missing = [t for t in range(first, last, step) if t not in have]
        if missing:
            first_gap = datetime.fromtimestamp(missing[0] / 1000, UTC).isoformat()
            problems.append(
                f"SMARD prices miss {len(missing)} of {(last - first) // step} quarter-hours "
                f"in {start.date()}..{end.date()} (first gap {first_gap})"
            )
    elif entries("smard", bronze):
        problems.append("SMARD has entries but none for the price series")
    return problems

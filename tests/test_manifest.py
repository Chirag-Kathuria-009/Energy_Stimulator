import json
from datetime import UTC, datetime, timedelta

from ets.config import load_config
from ets.ingest import manifest, smard

START = datetime(2025, 10, 1, tzinfo=UTC)
END = START + timedelta(hours=1)  # 4 quarter-hours: enough to test coverage logic


def _write(path, text="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _price_chunk(tmp_path, values):
    series = [[int(START.timestamp() * 1000) + 900_000 * i, v] for i, v in enumerate(values)]
    payload = json.dumps({"series": series}).encode()
    return smard.save_bronze(payload, 4169, "quarterhour", 1, url="u", bronze=tmp_path)


def _other_sources(tmp_path):
    for source in ("opsd", "when2heat", "bdew"):
        manifest.record(source, _write(tmp_path / source / "f.dat"), "u", bronze=tmp_path)


def test_record_twice_keeps_one_entry(tmp_path):
    f = _write(tmp_path / "opsd" / "a.csv", "one")
    manifest.record("opsd", f, "u1", bronze=tmp_path)
    f.write_text("two!")
    manifest.record("opsd", f, "u2", {"k": 1}, bronze=tmp_path)
    (entry,) = manifest.entries("opsd", tmp_path)
    assert entry["url"] == "u2" and entry["bytes"] == 4 and entry["extra"] == {"k": 1}
    assert entry["path"] == "opsd/a.csv"
    assert not list(tmp_path.glob("*.tmp"))


def test_is_current_detects_changed_file(tmp_path):
    f = _write(tmp_path / "opsd" / "a.csv", "one")
    assert not manifest.is_current(f, tmp_path)  # not recorded yet
    manifest.record("opsd", f, "u", bronze=tmp_path)
    assert manifest.is_current(f, tmp_path)
    f.write_text("changed")
    assert not manifest.is_current(f, tmp_path)


def test_check_coverage_ok(tmp_path):
    _other_sources(tmp_path)
    _price_chunk(tmp_path, [1.0, 2.0, 3.0, 4.0])
    assert manifest.check_coverage(load_config(), tmp_path, START, END) == []


def test_check_coverage_reports_gap(tmp_path):
    _other_sources(tmp_path)
    _price_chunk(tmp_path, [1.0, None, 3.0, 4.0])  # a null slot counts as missing
    (problem,) = manifest.check_coverage(load_config(), tmp_path, START, END)
    assert "miss 1 of 4 quarter-hours" in problem and "2025-10-01T00:15:00" in problem


def test_check_coverage_reports_missing_sources_and_files(tmp_path):
    problems = manifest.check_coverage(load_config(), tmp_path, START, END)
    assert set(problems) == {
        f"source '{s}' has no manifest entry" for s in manifest.REQUIRED_SOURCES
    }

    _other_sources(tmp_path)
    _price_chunk(tmp_path, [1.0, 2.0, 3.0, 4.0])
    (tmp_path / "when2heat" / "f.dat").unlink()
    (problem,) = manifest.check_coverage(load_config(), tmp_path, START, END)
    assert "when2heat" in problem and "missing" in problem

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ets.ingest import manifest, smard

FIXTURE = Path(__file__).parent / "fixtures" / "smard_chunk_sample.json"
BASE = "https://example.test/chart_data"
T0 = int(datetime(2020, 1, 6, tzinfo=UTC).timestamp() * 1000)  # a Monday long in the past
INDEX = [T0, T0 + smard.WEEK_MS, T0 + 2 * smard.WEEK_MS]


def test_urls():
    assert smard.index_url(BASE, 4169, "DE-LU", "quarterhour") == (
        f"{BASE}/4169/DE-LU/index_quarterhour.json"
    )
    assert smard.chunk_url(BASE, 4169, "DE-LU", "quarterhour", 123) == (
        f"{BASE}/4169/DE-LU/4169_DE-LU_quarterhour_123.json"
    )


def test_fixture_is_parseable_and_has_a_null():
    series = json.loads(FIXTURE.read_bytes())["series"]
    assert series[0] == [1791151200000, 161.27]
    assert series[4][1] is None  # future slot in the newest chunk
    assert series[5][1] == -12.5  # negative prices are legitimate


def test_save_bronze_writes_identical_bytes(tmp_path):
    payload = FIXTURE.read_bytes()
    path = smard.save_bronze(payload, 4169, "quarterhour", 1791151200000, url="u", bronze=tmp_path)
    assert path == tmp_path / "smard" / "4169" / "quarterhour" / "1791151200000.json"
    assert path.read_bytes() == payload
    (entry,) = manifest.entries("smard", tmp_path)
    assert entry["sha256"] == manifest.sha256_of(FIXTURE)
    assert entry["extra"]["ts_ms"] == 1791151200000
    assert not list(tmp_path.rglob("*.part"))


@pytest.fixture
def fake_http(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(smard.http, "get_json", lambda url: {"timestamps": INDEX[::-1]})

    def fake_get_bytes(url):
        calls.append(url)
        return FIXTURE.read_bytes()

    monkeypatch.setattr(smard.http, "get_bytes", fake_get_bytes)
    return calls


def _range(tmp_path, start, end):
    return smard.fetch_range(
        BASE, 4169, "DE-LU", "quarterhour",
        datetime.fromtimestamp(start / 1000, UTC), datetime.fromtimestamp(end / 1000, UTC),
        bronze=tmp_path,
    )  # fmt: skip


def test_fetch_range_skips_final_chunks_but_refreshes_newest(tmp_path, fake_http):
    paths = _range(tmp_path, T0, INDEX[-1] + 1000)
    assert len(paths) == 3 and len(fake_http) == 3

    fake_http.clear()
    _range(tmp_path, T0, INDEX[-1] + 1000)
    # chunks 0 and 1 were fetched after their week ended: skipped. Newest is refreshed.
    assert len(fake_http) == 1 and fake_http[0].endswith(f"_{INDEX[-1]}.json")


def test_fetch_range_refetches_chunk_that_was_fetched_before_its_week_ended(tmp_path, fake_http):
    _range(tmp_path, T0, INDEX[-1] + 1000)
    # Pretend chunk 1 was downloaded while it was still the newest week (before it had ended).
    mpath = tmp_path / manifest.MANIFEST_NAME
    data = json.loads(mpath.read_text())
    for e in data["entries"]:
        if e["extra"]["ts_ms"] == INDEX[1]:
            e["fetched_at_utc"] = datetime.fromtimestamp(INDEX[1] / 1000 + 3600, UTC).isoformat()
    mpath.write_text(json.dumps(data))

    fake_http.clear()
    _range(tmp_path, T0, INDEX[-1] + 1000)
    assert sorted(u.rsplit("_", 1)[1] for u in fake_http) == sorted(
        [f"{INDEX[1]}.json", f"{INDEX[-1]}.json"]
    )


def test_fetch_range_downloads_only_overlapping_chunks(tmp_path, fake_http):
    paths = _range(tmp_path, INDEX[1] + 1000, INDEX[1] + 2000)
    assert [p.stem for p in paths] == [str(INDEX[1])]
    assert len(fake_http) == 1

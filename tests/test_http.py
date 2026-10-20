import httpx
import pytest
from tenacity import wait_none

from ets.ingest import http

# Same retry policy, no waiting between attempts.
fast_get = http.get_bytes.retry_with(wait=wait_none())
fast_download = http.download.retry_with(wait=wait_none())


def _transport(statuses, body=b"payload"):
    seen = []

    def handler(request):
        seen.append(request)
        status = statuses[min(len(seen) - 1, len(statuses) - 1)]
        return httpx.Response(status, content=body if status == 200 else b"")

    return httpx.MockTransport(handler), seen


def test_retries_5xx_then_succeeds():
    transport, seen = _transport([503, 503, 200])
    assert fast_get("https://x.test/a", transport=transport) == b"payload"
    assert len(seen) == 3


def test_does_not_retry_4xx():
    transport, seen = _transport([404])
    with pytest.raises(httpx.HTTPStatusError):
        fast_get("https://x.test/a", transport=transport)
    assert len(seen) == 1


def test_gives_up_after_five_attempts():
    transport, seen = _transport([500])
    with pytest.raises(httpx.HTTPStatusError):
        fast_get("https://x.test/a", transport=transport)
    assert len(seen) == 5


def test_download_renames_part_file_on_success(tmp_path):
    transport, _ = _transport([200], body=b"x" * 3_000_000)
    dest = fast_download("https://x.test/big", tmp_path / "d" / "big.csv", transport=transport)
    assert dest.read_bytes() == b"x" * 3_000_000
    assert not list(tmp_path.rglob("*.part"))


def test_failed_download_leaves_no_file_that_looks_complete(tmp_path):
    transport, _ = _transport([500])
    with pytest.raises(httpx.HTTPStatusError):
        fast_download("https://x.test/big", tmp_path / "big.csv", transport=transport)
    assert list(tmp_path.iterdir()) == []

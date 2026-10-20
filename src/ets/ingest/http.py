"""Shared HTTP helper: retries on transient failures, never leaves a half-written file."""

from pathlib import Path

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

USER_AGENT = "energy-tariff-simulator/0.1 (portfolio project; python-httpx)"
TIMEOUT_S = 30.0


def client(**kwargs) -> httpx.Client:
    """30 s timeout and an identifying user agent. Extra kwargs (e.g. transport) go to httpx."""
    return httpx.Client(
        timeout=TIMEOUT_S,
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
        **kwargs,
    )


def _transient(exc: BaseException) -> bool:
    """Retry timeouts, connection errors and 5xx. A 4xx is a real answer, not worth retrying."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return isinstance(exc, httpx.TimeoutException | httpx.TransportError)


_retry = retry(
    retry=retry_if_exception(_transient),
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=1, max=30),
    reraise=True,
)


@_retry
def get_bytes(url: str, **client_kwargs) -> bytes:
    """GET and return the raw body, unchanged."""
    with client(**client_kwargs) as c:
        r = c.get(url)
        r.raise_for_status()
        return r.content


def get_json(url: str, **client_kwargs) -> dict:
    import json

    return json.loads(get_bytes(url, **client_kwargs))


@_retry
def download(url: str, dest: Path, **client_kwargs) -> Path:
    """Stream to `<dest>.part`, then rename, so a crash never leaves a file that looks complete."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    try:
        with client(**client_kwargs) as c, c.stream("GET", url) as r:
            r.raise_for_status()
            with part.open("wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)
        part.replace(dest)
    finally:
        part.unlink(missing_ok=True)
    return dest

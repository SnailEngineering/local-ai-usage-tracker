"""Tiny stdlib HTTP helper. No dependencies is a feature here -- the collector
runs unattended from launchd and should never break because a venv drifted."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "ai-usage-tracker/0.1 (+local)"

RETRY_STATUS = {429, 500, 502, 503, 504, 529}


class HttpError(RuntimeError):
    def __init__(self, status: int, body: str, url: str):
        super().__init__(f"HTTP {status} for {url}: {body[:400]}")
        self.status = status
        self.body = body


def get_json(url: str, params: dict, headers: dict,
             *, retries: int = 4, timeout: int = 60) -> dict:
    """GET with repeated-key array encoding and exponential backoff.

    Both providers expect repeated `group_by[]=a&group_by[]=b` for list params,
    which urlencode gives us via doseq once the keys are pre-expanded.
    """
    pairs: list[tuple[str, str]] = []
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            for v in value:
                pairs.append((key, str(v)))
        else:
            pairs.append((key, str(value)))

    full = f"{url}?{urllib.parse.urlencode(pairs)}"
    req = urllib.request.Request(full, headers={"User-Agent": USER_AGENT, **headers})

    delay = 2.0
    last: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            if e.code not in RETRY_STATUS or attempt == retries - 1:
                raise HttpError(e.code, body, full) from None
            last = e
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == retries - 1:
                raise
            last = e
        time.sleep(delay)
        delay *= 2

    raise RuntimeError(f"unreachable: {last}")

"""Shared HTTP session with retries and a polite rate limiter."""
from __future__ import annotations

import logging
import threading
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)

USER_AGENT = "licence-tracker/1.0 (regulatory register monitor; github actions)"


def make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=4,
        backoff_factor=2,
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=("GET", "HEAD"),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_maxsize=16)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers["User-Agent"] = USER_AGENT
    return s


_ROBOTS = {}


def robots_allowed(session: requests.Session, url: str, agent: str = USER_AGENT) -> bool:
    """True unless the site's robots.txt disallows `url` (missing/unreadable
    robots.txt counts as allowed). Cached per host."""
    import urllib.robotparser
    from urllib.parse import urlparse
    p = urlparse(url)
    root = f"{p.scheme}://{p.netloc}"
    rp = _ROBOTS.get(root)
    if rp is None:
        rp = urllib.robotparser.RobotFileParser()
        try:
            r = session.get(root + "/robots.txt", timeout=30)
            rp.parse(r.text.splitlines() if r.status_code == 200 else [])
        except Exception:
            rp.parse([])
        _ROBOTS[root] = rp
    return rp.can_fetch(agent, url)


class RateLimiter:
    """Keeps requests at or below `per_second`."""

    def __init__(self, per_second: float):
        self.interval = 1.0 / per_second
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self):
        """Thread-safe: each caller reserves the next free slot."""
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next)
            self._next = slot + self.interval
        delay = slot - time.monotonic()
        if delay > 0:
            time.sleep(delay)

"""Shared HTTP session with retries and a polite rate limiter."""
from __future__ import annotations

import logging
import threading
import time

import re

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)

# The usual way a well-behaved bot names itself (like "Mozilla/5.0 (compatible; Googlebot/2.1; +url)"):
# it still says plainly that this is the licence-tracker bot and where it lives. Several regulators'
# sites answer "404 Not Found" to requests whose User-Agent doesn't start with "Mozilla/5.0".
USER_AGENT = "Mozilla/5.0 (compatible; licence-tracker/1.0; +https://github.com/TheShaz77/licence-tracker)"
ROBOTS_AGENT = "licence-tracker/1.0"     # the name looked up in robots.txt


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
    s.headers["Accept-Language"] = "en-GB,en;q=0.9"
    return s


_ROBOTS = {}
BLOCKED = {}   # host -> robots.txt text, for sites whose robots.txt refused a download (saved to data/debug)
_TEXT = {}


def _groups(text: str) -> list:
    """robots.txt as [(user-agents, [(allow?, pattern), ...])]."""
    groups, agents, rules, last = [], [], [], None
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        field, value = (x.strip() for x in line.split(":", 1))
        field = field.lower()
        if field == "user-agent":
            if last == "rule":
                groups.append((agents, rules))
                agents, rules = [], []
            agents.append(value.lower())
            last = "agent"
        elif field in ("allow", "disallow"):
            if agents:
                rules.append((field == "allow", value))
            last = "rule"
    if agents:
        groups.append((agents, rules))
    return groups


def _pattern_len_if_match(pattern: str, path: str) -> int:
    """Length of the pattern if it matches the path (RFC 9309: * wildcard, $ end anchor), else -1."""
    if pattern == "":
        return -1
    rx = "".join(".*" if ch == "*" else ("$" if ch == "$" and i == len(pattern) - 1 else re.escape(ch))
                 for i, ch in enumerate(pattern))
    return len(pattern) if re.match(rx, path) else -1


def rules_allow(text: str, url: str, agent: str = ROBOTS_AGENT) -> bool:
    """Decide like Google and RFC 9309: the group for the most specific matching
    user-agent (else *), then the longest matching rule wins; Allow wins ties."""
    from urllib.parse import urlparse
    p = urlparse(url)
    path = (p.path or "/") + (("?" + p.query) if p.query else "")
    token = agent.split("/")[0].lower()
    groups = _groups(text or "")
    chosen = [r for a, r in groups if any(x != "*" and x in token for x in a)]
    if not chosen:
        chosen = [r for a, r in groups if "*" in a]
    rules = [rule for g in chosen for rule in g]
    best_len, allowed = -1, True
    for allow, pattern in rules:
        n = _pattern_len_if_match(pattern, path)
        if n > best_len or (n == best_len and n >= 0 and allow):
            best_len, allowed = n, allow
    return allowed


def robots_allowed(session: requests.Session, url: str, agent: str = ROBOTS_AGENT) -> bool:
    """True unless the site's robots.txt disallows `url` (missing/unreadable
    robots.txt counts as allowed). Cached per host. Implements wildcards and
    longest-match like search engines do (Python's built-in parser does not)."""
    from urllib.parse import urlparse
    p = urlparse(url)
    root = f"{p.scheme}://{p.netloc}"
    if root not in _TEXT:
        try:
            r = session.get(root + "/robots.txt", timeout=30)
            _TEXT[root] = r.text if r.status_code == 200 else ""
        except Exception:
            _TEXT[root] = ""
    ok = rules_allow(_TEXT[root], url, agent)
    if not ok:
        BLOCKED[p.netloc] = _TEXT[root]
    return ok


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

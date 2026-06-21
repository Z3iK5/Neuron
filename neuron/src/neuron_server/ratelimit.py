# SPDX-License-Identifier: Apache-2.0
"""In-process request rate limiting (token bucket).

A small, dependency-free limiter for the abuse-prone endpoints:

- password login — both per **account** (brute-force one login) and per **client
  IP** (one host spraying many accounts);
- account **registration** — per client IP (sign-up spam);
- message sending — per **sender** (spam).

Account/sender-keyed limits work behind a proxy with no extra config. The IP-keyed
limits use the proxy-aware client IP (see ``neuron_server.proxy.client_ip``), so a
reverse-proxy deployment must set ``trusted_proxies`` — otherwise every request
shares the proxy's single bucket.

Per-process: each worker enforces its own buckets. That bounds abuse on every
worker; a strict global limit across workers would need a shared store (Redis) and
is a later concern. ``build_rate_limiters`` reads the limits from settings.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from neuron_server.config import NeuronServerSettings
from neuron_server.errors import limit_exceeded

# Above this many tracked keys, drop idle (fully-refilled) buckets so an attacker
# varying the key (e.g. random login usernames) can't grow memory without bound.
_MAX_KEYS = 10_000


class RateLimiter:
    """A token-bucket limiter shared across keys.

    Each key gets a bucket that refills at ``rate_hz`` tokens/second up to
    ``burst`` tokens. :meth:`consume` takes one token if available (returns
    ``None``) or, if empty, returns the seconds until a token is free.
    """

    def __init__(
        self, rate_hz: float, burst: int, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._rate = rate_hz
        self._burst = max(1, burst)
        self._clock = clock
        self._buckets: dict[str, tuple[float, float]] = {}  # key -> (tokens, last_ts)

    def consume(self, key: str) -> float | None:
        now = self._clock()
        if len(self._buckets) > _MAX_KEYS:
            self._prune(now)
        tokens, last = self._buckets.get(key, (float(self._burst), now))
        tokens = min(float(self._burst), tokens + (now - last) * self._rate)
        if tokens >= 1.0:
            self._buckets[key] = (tokens - 1.0, now)
            return None
        self._buckets[key] = (tokens, now)
        # Time until one more token accrues (rate is validated > 0 in settings).
        return (1.0 - tokens) / self._rate

    def _prune(self, now: float) -> None:
        """Bound memory: drop idle (fully-refilled) buckets, then hard-cap by LRU."""
        idle = [
            key
            for key, (tokens, last) in self._buckets.items()
            if min(float(self._burst), tokens + (now - last) * self._rate) >= self._burst
        ]
        for key in idle:
            del self._buckets[key]
        # An attacker rotating the key (e.g. spoofed/rotated client IPs) keeps every
        # bucket partially drained, so the idle sweep above frees nothing. Hard-cap
        # the dict by evicting the least-recently-touched buckets. Evict down to a
        # low-water mark, not just back to the cap, so this O(N log N) sort amortizes
        # over ~_MAX_KEYS/10 inserts instead of firing on every consume during a
        # flood. (Evicting a still-drained victim bucket resets its limit, but the
        # independent per-IP limiter still throttles the source.)
        if len(self._buckets) > _MAX_KEYS:
            target = _MAX_KEYS * 9 // 10
            stale = sorted(self._buckets, key=lambda k: self._buckets[k][1])
            for key in stale[: len(self._buckets) - target]:
                del self._buckets[key]


@dataclass
class RateLimiters:
    """The server's configured limiters; ``check_*`` raises 429 when exceeded."""

    enabled: bool
    login: RateLimiter
    login_ip: RateLimiter
    registration: RateLimiter
    message: RateLimiter

    def _check(self, limiter: RateLimiter, key: str) -> None:
        if not self.enabled:
            return
        retry_after_s = limiter.consume(key)
        if retry_after_s is not None:
            # Round up to at least 1ms so clients always back off a little.
            raise limit_exceeded(max(1, int(retry_after_s * 1000)))

    def check_login(self, account: str) -> None:
        self._check(self.login, account)

    def check_login_ip(self, client_ip: str) -> None:
        self._check(self.login_ip, client_ip)

    def check_registration(self, client_ip: str) -> None:
        self._check(self.registration, client_ip)

    def check_message(self, user_id: str) -> None:
        self._check(self.message, user_id)


def build_rate_limiters(settings: NeuronServerSettings) -> RateLimiters:
    """Build the configured limiters from ``settings``."""
    return RateLimiters(
        enabled=settings.rate_limit_enabled,
        login=RateLimiter(settings.rate_limit_login_hz, settings.rate_limit_login_burst),
        login_ip=RateLimiter(
            settings.rate_limit_login_ip_hz, settings.rate_limit_login_ip_burst
        ),
        registration=RateLimiter(
            settings.rate_limit_registration_hz, settings.rate_limit_registration_burst
        ),
        message=RateLimiter(settings.rate_limit_message_hz, settings.rate_limit_message_burst),
    )

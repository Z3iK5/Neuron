# SPDX-License-Identifier: Apache-2.0
"""Tests for request rate limiting: the token bucket, the holder, and the
login endpoint returning a spec-shaped 429."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from neuron_server.app import create_app
from neuron_server.config import NeuronServerSettings
from neuron_server.errors import MatrixError
from neuron_server.ratelimit import _MAX_KEYS, RateLimiter, build_rate_limiters

_REG = "/_matrix/client/v3/register"
_LOGIN = "/_matrix/client/v3/login"


def test_token_bucket_allows_burst_then_limits_then_refills() -> None:
    now = [1000.0]
    limiter = RateLimiter(rate_hz=1.0, burst=3, clock=lambda: now[0])

    assert limiter.consume("k") is None  # 3 immediate
    assert limiter.consume("k") is None
    assert limiter.consume("k") is None
    retry = limiter.consume("k")  # 4th denied
    assert retry == pytest.approx(1.0)  # one token in 1/rate seconds

    now[0] += 1.0  # one token refills
    assert limiter.consume("k") is None
    assert limiter.consume("k") is not None  # empty again

    # Keys are independent.
    assert limiter.consume("other") is None


def test_rate_limiters_raise_429_and_respect_disabled() -> None:
    limiters = build_rate_limiters(
        NeuronServerSettings(rate_limit_login_burst=1, rate_limit_login_hz=0.001)
    )
    limiters.check_login("alice")  # consumes the single token
    with pytest.raises(MatrixError) as exc:
        limiters.check_login("alice")
    assert exc.value.status_code == 429
    assert exc.value.errcode == "M_LIMIT_EXCEEDED"
    assert isinstance(exc.value.extra.get("retry_after_ms"), int)
    assert exc.value.headers.get("Retry-After")
    # A different account has its own bucket.
    limiters.check_login("bob")

    disabled = build_rate_limiters(
        NeuronServerSettings(rate_limit_enabled=False, rate_limit_login_burst=1)
    )
    for _ in range(5):
        disabled.check_login("alice")  # never raises


def test_login_endpoint_is_rate_limited(tmp_path: Path) -> None:
    settings = NeuronServerSettings(
        name="neuron.local",
        database_url=f"sqlite:///{tmp_path / 'hs.db'}",
        rate_limit_login_burst=3,
        rate_limit_login_hz=0.001,  # effectively no refill during the test
    )
    with TestClient(create_app(settings)) as client:
        session = client.post(_REG, json={"username": "alice", "password": "pw-123456"}).json()[
            "session"
        ]
        client.post(
            _REG,
            json={
                "username": "alice",
                "password": "pw-123456",
                "auth": {"type": "m.login.dummy", "session": session},
            },
        )

        attempt = {
            "type": "m.login.password",
            "identifier": {"type": "m.id.user", "user": "alice"},
            "password": "wrong-password",
        }
        last = None
        for _ in range(6):  # burst is 3, so later attempts are limited
            last = client.post(_LOGIN, json=attempt)

        assert last is not None
        assert last.status_code == 429
        body = last.json()
        assert body["errcode"] == "M_LIMIT_EXCEEDED"
        assert isinstance(body["retry_after_ms"], int)
        assert last.headers.get("Retry-After")


def test_login_rate_limit_key_is_normalized_across_identifier_forms(tmp_path: Path) -> None:
    """'alice' and '@alice:server' are the same account — alternating the
    identifier form must not grant a second per-account bucket."""
    settings = NeuronServerSettings(
        name="neuron.local",
        database_url=f"sqlite:///{tmp_path / 'hs.db'}",
        rate_limit_login_burst=3,
        rate_limit_login_hz=0.001,  # effectively no refill during the test
    )
    with TestClient(create_app(settings)) as client:
        def _try(user: str):  # type: ignore[no-untyped-def]
            return client.post(
                _LOGIN,
                json={
                    "type": "m.login.password",
                    "identifier": {"type": "m.id.user", "user": user},
                    "password": "wrong-password",
                },
            )

        # Alternate the localpart and full-MXID forms of the same account. With
        # burst 3, the 4th attempt must be limited even though only 2 of each
        # form were sent — both forms charge the one normalized bucket.
        forms = ["alice", "@alice:neuron.local", "alice", "@alice:neuron.local"]
        statuses = [_try(form).status_code for form in forms]
        assert statuses[:3] == [403, 403, 403]  # wrong password, not limited
        assert statuses[3] == 429


def test_bucket_dict_is_hard_capped_under_key_rotation() -> None:
    now = [1000.0]
    # No refill during the test, so every bucket stays partially drained — the
    # idle-only prune would free nothing; the LRU hard-cap must bound the dict.
    limiter = RateLimiter(rate_hz=0.001, burst=5, clock=lambda: now[0])
    for i in range(_MAX_KEYS + 2000):
        limiter.consume(f"ip-{i}")
    assert len(limiter._buckets) <= _MAX_KEYS + 1


def test_ip_keyed_limiters_raise_and_respect_disabled() -> None:
    limiters = build_rate_limiters(
        NeuronServerSettings(
            rate_limit_registration_burst=1,
            rate_limit_registration_hz=0.001,
            rate_limit_login_ip_burst=1,
            rate_limit_login_ip_hz=0.001,
        )
    )
    limiters.check_registration("1.2.3.4")  # single token
    with pytest.raises(MatrixError) as exc:
        limiters.check_registration("1.2.3.4")
    assert exc.value.status_code == 429
    limiters.check_registration("5.6.7.8")  # a different IP has its own bucket

    limiters.check_login_ip("1.2.3.4")
    with pytest.raises(MatrixError):
        limiters.check_login_ip("1.2.3.4")

    disabled = build_rate_limiters(
        NeuronServerSettings(
            rate_limit_enabled=False,
            rate_limit_registration_burst=1,
            rate_limit_login_ip_burst=1,
        )
    )
    for _ in range(5):
        disabled.check_registration("1.2.3.4")  # never raises
        disabled.check_login_ip("1.2.3.4")


def test_registration_is_rate_limited_at_the_challenge(tmp_path: Path) -> None:
    settings = NeuronServerSettings(
        name="neuron.local",
        database_url=f"sqlite:///{tmp_path / 'hs.db'}",
        rate_limit_registration_burst=2,
        rate_limit_registration_hz=0.001,  # no refill during the test
    )
    with TestClient(create_app(settings)) as client:
        def _challenge(user: str):  # type: ignore[no-untyped-def]
            return client.post(_REG, json={"username": user, "password": "pw-123456"})

        # The IP limit is charged at the UIA challenge — the unauthenticated step
        # that persists a uia_sessions row — so a flood can't bloat that table.
        assert _challenge("alice").status_code == 401  # challenge issued
        assert _challenge("bob").status_code == 401
        denied = _challenge("carol")
        assert denied.status_code == 429
        assert denied.json()["errcode"] == "M_LIMIT_EXCEEDED"


def test_get_started_rate_limit_renders_branded_html(tmp_path: Path) -> None:
    settings = NeuronServerSettings(
        name="neuron.local",
        database_url=f"sqlite:///{tmp_path / 'hs.db'}",
        registration_enabled=True,
        rate_limit_registration_burst=1,
        rate_limit_registration_hz=0.001,
    )
    with TestClient(create_app(settings)) as client:
        first = client.post(
            "/get-started", data={"username": "alice", "password": "s3cret-password"}
        )
        assert first.status_code == 200  # the only token is consumed
        # The next sign-up is rate-limited; the HTML form must render the branded
        # page, not the raw JSON Matrix error from the global handler.
        denied = client.post(
            "/get-started",
            data={"username": "bob", "password": "s3cret-password"},
            follow_redirects=False,
        )
        assert denied.status_code == 429
        assert "text/html" in denied.headers["content-type"]
        assert '"errcode"' not in denied.text


def test_login_is_rate_limited_by_ip_across_accounts(tmp_path: Path) -> None:
    settings = NeuronServerSettings(
        name="neuron.local",
        database_url=f"sqlite:///{tmp_path / 'hs.db'}",
        rate_limit_login_ip_burst=2,
        rate_limit_login_ip_hz=0.001,
    )
    with TestClient(create_app(settings)) as client:
        def _try(user: str):  # type: ignore[no-untyped-def]
            return client.post(
                _LOGIN,
                json={
                    "type": "m.login.password",
                    "identifier": {"type": "m.id.user", "user": user},
                    "password": "x",
                },
            )

        # Distinct accounts each time, so the per-account bucket never trips — but
        # the shared per-IP bucket (burst 2) does on the third attempt.
        assert _try("a").status_code != 429  # 401 (unknown user), not rate-limited
        assert _try("b").status_code != 429
        assert _try("c").status_code == 429

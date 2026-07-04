# SPDX-License-Identifier: Apache-2.0
"""Remote user profiles over federation (HS-7).

Fetches a remote user's displayname/avatar_url from their homeserver via
``GET /_matrix/federation/v1/query/profile`` when one of our clients asks for
the profile of a user on another server.
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlencode

from neuron_server.errors import MatrixError
from neuron_server.federation.client import FederationClient
from neuron_server.federation.validation import domain_of

# How long a fetched remote profile is served from memory before re-fetching.
_DEFAULT_TTL_S = 300.0
# Soft cap on cache entries; expired rows are pruned when it is exceeded.
_MAX_CACHE_ENTRIES = 1024


class RemoteProfileFetcher:
    """Fetches remote users' profiles, with a short-TTL in-process cache.

    The cache is **per process**: with multiple workers each keeps its own copy,
    so a profile change on the remote server may be visible on one worker before
    another for up to the TTL. That is acceptable for display data — the cache
    only exists so a burst of profile reads (e.g. rendering a member list)
    doesn't hammer the remote server.
    """

    def __init__(self, client: FederationClient, *, ttl_s: float = _DEFAULT_TTL_S) -> None:
        self._client = client
        self._ttl_s = ttl_s
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}

    async def fetch(self, user_id: str) -> dict[str, Any]:
        """The remote user's profile (``displayname``/``avatar_url``, when set).

        Raises ``M_NOT_FOUND`` when the remote server is unreachable, errors, or
        doesn't know the user — per spec that is how a missing profile reads.
        """
        now = time.monotonic()
        cached = self._cache.get(user_id)
        if cached is not None and now < cached[0]:
            return dict(cached[1])

        destination = domain_of(user_id)
        path = "/_matrix/federation/v1/query/profile?" + urlencode({"user_id": user_id})
        try:
            answer = await self._client.get_json(destination, path)
        except Exception as exc:
            raise MatrixError(
                404, "M_NOT_FOUND", "Remote profile could not be fetched"
            ) from exc

        profile = {k: v for k, v in answer.items() if k in ("displayname", "avatar_url")}
        if len(self._cache) >= _MAX_CACHE_ENTRIES:
            self._cache = {k: v for k, v in self._cache.items() if now < v[0]}
        self._cache[user_id] = (now + self._ttl_s, profile)
        return dict(profile)

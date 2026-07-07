# SPDX-License-Identifier: Apache-2.0
"""Matrix specification constants advertised by ``/_matrix/client/versions``.

The Client-Server API defines a discovery endpoint that lists the spec versions a
server is compatible with, so clients know which features and endpoints they may
use (Matrix CS API: "GET /_matrix/client/versions").

These values declare the spec revisions ``neuron_server`` *targets* for
compatibility. Endpoint coverage is implemented incrementally: advertising a
version here reflects the intended client-facing contract, not a guarantee that
every endpoint introduced in that revision is already live.
"""

from __future__ import annotations

# CS API spec versions we target. Ordered oldest -> newest.
SUPPORTED_SPEC_VERSIONS: tuple[str, ...] = (
    "v1.1",
    "v1.2",
    "v1.3",
    "v1.4",
    "v1.5",
    "v1.6",
    "v1.7",
    "v1.8",
    "v1.9",
    "v1.10",
    "v1.11",
)

# Unstable feature flags (MSC opt-ins) we expose. Config-dependent flags (e.g.
# MSC3861 delegated auth, only advertised when OIDC is enabled) are added at
# request time in the /versions handler; the ones here are always on.
UNSTABLE_FEATURES: dict[str, bool] = {
    # Native Simplified Sliding Sync — the /sync variant Element X uses.
    "org.matrix.msc3575": True,
    "org.matrix.simplified_msc3575": True,
    "org.matrix.msc4186": True,
}

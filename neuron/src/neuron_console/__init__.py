# SPDX-License-Identifier: Apache-2.0
"""neuron_console — a web admin console over the homeserver Admin API.

Phase 1 is **read-only**: it lets an operator log in and browse users, rooms,
and content reports. Write/destructive actions and MAS/OIDC login arrive in
Phase 2 (see ``PLAN.md``).

Design notes:

- The **server-admin token** is configured server-side and is used only by the
  backend to call the homeserver. It is **never** sent to the browser.
- Operators authenticate with a separate console password; a signed session
  cookie keeps them logged in.
- Pages are server-rendered (Jinja2) so the console works without any
  client-side build step.
"""

from neuron_console.app import create_app

__all__ = ["create_app"]

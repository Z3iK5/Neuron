# SPDX-License-Identifier: Apache-2.0
"""``neuron-server doctor`` — a preflight / health check for a homeserver.

Validates the things that quietly break a deployment: the configured identity and
URLs, the database (reachable, schema state, identity match), the signing key, the
media store, and — unless ``--offline`` — whether the server is actually listening,
serves client discovery at its public URL, and is reachable + verifiable over
federation.

Each check yields a :class:`CheckResult` with one of three states:

- **ok**   — fine.
- **warn** — works, but you probably want to fix it before production (advisory).
- **fail** — broken; the server will not work correctly until it is fixed.

The network checks are deliberately forgiving (a server that is simply not running
yet is a *warning*, not a failure) so the command is useful both before first start
and as an ongoing health probe.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import urllib.parse
from dataclasses import dataclass
from enum import StrEnum

import httpx

from neuron_server.config import NeuronServerSettings
from neuron_server.crypto.signing import SigningKey, parse_signing_key
from neuron_server.federation.client import FederationClient, OpenClient
from neuron_server.federation.discovery import pick_base_url
from neuron_server.keys.resolver import parse_and_verify_key_document
from neuron_server.storage.database import Database, connect_database
from neuron_server.storage.metadata import get_metadata
from neuron_server.storage.migrations import MIGRATIONS

_NET_TIMEOUT = 4.0
_LOCAL_NAMES = {"localhost", "127.0.0.1", "::1", "neuron.local"}
_ALL_INTERFACES = {"0.0.0.0", "::"}


class Status(StrEnum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True)
class CheckResult:
    """The outcome of a single doctor check."""

    name: str
    status: Status
    detail: str


def _backend_label(database_url: str) -> str:
    scheme = database_url.split(":", 1)[0].lower()
    if scheme.startswith("sqlite"):
        return "SQLite"
    if scheme.startswith("postgres"):
        return "PostgreSQL"
    return scheme or "unknown"


def _host_is_local(host: str) -> bool:
    if host in _LOCAL_NAMES:
        return True
    if host.endswith(".local"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


# --- individual checks (synchronous, config-only) --------------------------


def _check_server_name(settings: NeuronServerSettings) -> CheckResult:
    name = settings.name.strip()
    if not name:
        return CheckResult("server name", Status.FAIL, "NEURON_SERVER_NAME is not set")
    if "/" in name or " " in name:
        return CheckResult("server name", Status.FAIL, f"{name!r} is not a valid server name")
    if _host_is_local(name) or "." not in name:
        return CheckResult(
            "server name",
            Status.WARN,
            f"{name!r} is fine for local development, but other servers cannot "
            "federate with a non-public name",
        )
    return CheckResult("server name", Status.OK, name)


def _check_public_base_url(settings: NeuronServerSettings) -> CheckResult:
    url = settings.public_base_url.strip()
    parsed = urllib.parse.urlparse(url)
    if not parsed.scheme or not parsed.hostname:
        return CheckResult(
            "public base URL", Status.FAIL, f"{url!r} is not a valid absolute URL"
        )
    if parsed.scheme != "https":
        if _host_is_local(parsed.hostname):
            return CheckResult(
                "public base URL", Status.WARN, f"{url} (http + local — dev only)"
            )
        return CheckResult(
            "public base URL",
            Status.WARN,
            f"{url} uses http; clients and federation require https in production",
        )
    return CheckResult("public base URL", Status.OK, url)


def _check_bind(settings: NeuronServerSettings) -> CheckResult:
    where = f"{settings.bind_host}:{settings.bind_port}"
    if settings.bind_host in _ALL_INTERFACES:
        return CheckResult(
            "bind address",
            Status.OK,
            f"{where} (all interfaces — put a TLS-terminating reverse proxy in front)",
        )
    return CheckResult("bind address", Status.OK, where)


def _check_media_store(settings: NeuronServerSettings) -> CheckResult:
    path = settings.media_store_path
    if os.path.isdir(path):
        if os.access(path, os.W_OK):
            return CheckResult("media store", Status.OK, f"{path} (writable)")
        return CheckResult("media store", Status.FAIL, f"{path} exists but is not writable")
    if os.path.exists(path):
        return CheckResult("media store", Status.FAIL, f"{path} exists but is not a directory")
    parent = os.path.dirname(os.path.abspath(path))
    if os.access(parent, os.W_OK):
        return CheckResult("media store", Status.OK, f"{path} (will be created)")
    return CheckResult("media store", Status.FAIL, f"cannot create {path}: {parent} not writable")


def _check_registration(settings: NeuronServerSettings) -> CheckResult:
    if settings.registration_enabled:
        return CheckResult(
            "registration",
            Status.WARN,
            "open — anyone can create an account; disable it and hand out invite "
            "links from the console for a private server",
        )
    return CheckResult("registration", Status.OK, "closed (invite links / admin-created only)")


def _check_admin_users(settings: NeuronServerSettings) -> CheckResult:
    admins = settings.admin_user_ids()
    if not admins:
        return CheckResult(
            "admin users",
            Status.WARN,
            "none configured — set NEURON_SERVER_ADMIN_USERS to use the console / Admin API",
        )
    return CheckResult("admin users", Status.OK, ", ".join(sorted(admins)))


# --- database + signing key (need a connection) ----------------------------


async def _applied_schema(db: Database) -> set[int]:
    try:
        rows = await db.fetchall("SELECT version FROM schema_migrations")
    except Exception:
        return set()  # table absent -> database not yet initialized
    return {int(r[0]) for r in rows}


def _schema_result(applied: set[int], backend: str) -> CheckResult:
    latest = max(m.version for m in MIGRATIONS)
    if not applied:
        return CheckResult(
            "database", Status.OK, f"{backend}: reachable, not yet initialized"
        )
    current = max(applied)
    if current < latest:
        pending = latest - current
        return CheckResult(
            "database",
            Status.WARN,
            f"{backend}: schema v{current}, {pending} migration(s) pending "
            "(applied automatically on next start)",
        )
    return CheckResult("database", Status.OK, f"{backend}: schema v{current} (up to date)")


async def _identity_result(db: Database, settings: NeuronServerSettings) -> CheckResult | None:
    try:
        stored = await get_metadata(db, "server_name")
    except Exception:
        return None  # uninitialized — nothing recorded yet
    if stored is None:
        return None
    if stored != settings.name:
        return CheckResult(
            "server identity",
            Status.FAIL,
            f"database belongs to server_name={stored!r} but config is {settings.name!r}; "
            "the server will refuse to start",
        )
    return CheckResult("server identity", Status.OK, f"database matches {stored}")


async def _signing_key_result(
    db: Database | None, settings: NeuronServerSettings
) -> tuple[CheckResult, SigningKey | None]:
    if settings.signing_key_path:
        path = settings.signing_key_path
        if not os.path.exists(path):
            return (
                CheckResult("signing key", Status.OK, f"will be generated at {path} on first run"),
                None,
            )
        try:
            with open(path, encoding="utf-8") as handle:
                key = parse_signing_key(handle.read())
        except Exception as exc:
            return CheckResult("signing key", Status.FAIL, f"{path} is unreadable: {exc}"), None
        return CheckResult("signing key", Status.OK, f"{path} (key id {key.key_id})"), key

    if db is None:
        return CheckResult("signing key", Status.WARN, "skipped (database unavailable)"), None
    try:
        stored = await get_metadata(db, "signing_key")
    except Exception:
        stored = None
    if not stored:
        return (
            CheckResult(
                "signing key",
                Status.OK,
                "will be generated and stored in the database on first run",
            ),
            None,
        )
    try:
        key = parse_signing_key(stored)
    except Exception as exc:
        return CheckResult("signing key", Status.FAIL, f"stored key is invalid: {exc}"), None
    return CheckResult("signing key", Status.OK, f"stored in database (key id {key.key_id})"), key


async def _database_and_key_checks(
    settings: NeuronServerSettings,
) -> tuple[list[CheckResult], SigningKey | None]:
    backend = _backend_label(settings.database_url)
    db: Database | None = None
    try:
        db = connect_database(settings.database_url)
        await db.connect()
    except Exception as exc:
        results = [CheckResult("database", Status.FAIL, f"cannot connect to {backend}: {exc}")]
        key_result, key = await _signing_key_result(None, settings)
        results.append(key_result)
        return results, key

    try:
        results = [_schema_result(await _applied_schema(db), backend)]
        identity = await _identity_result(db, settings)
        if identity is not None:
            results.append(identity)
        key_result, key = await _signing_key_result(db, settings)
        results.append(key_result)
        return results, key
    finally:
        await db.disconnect()


# --- network checks (skipped with --offline) -------------------------------


async def _check_listening(settings: NeuronServerSettings) -> CheckResult:
    host = settings.bind_host
    if host in _ALL_INTERFACES:
        host = "127.0.0.1"
    try:
        fut = asyncio.open_connection(host, settings.bind_port)
        _, writer = await asyncio.wait_for(fut, timeout=_NET_TIMEOUT)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return CheckResult(
            "listening",
            Status.OK,
            f"something is accepting connections on {host}:{settings.bind_port}",
        )
    except (OSError, TimeoutError):
        return CheckResult(
            "listening",
            Status.WARN,
            f"nothing is listening on {host}:{settings.bind_port} "
            "(start it with `neuron-server`)",
        )


async def _check_client_discovery(
    settings: NeuronServerSettings, http_client: httpx.AsyncClient | None
) -> CheckResult:
    owns = http_client is None
    client = http_client or httpx.AsyncClient(
        base_url=settings.public_base_url, timeout=_NET_TIMEOUT
    )
    try:
        versions = await client.get("/_matrix/client/versions")
        versions.raise_for_status()
        wk = await client.get("/.well-known/matrix/client")
        wk.raise_for_status()
        advertised = wk.json().get("m.homeserver", {}).get("base_url")
    except Exception as exc:
        return CheckResult(
            "client discovery",
            Status.WARN,
            f"could not reach {settings.public_base_url}: {exc}",
        )
    finally:
        if owns:
            await client.aclose()

    if advertised != settings.public_base_url:
        return CheckResult(
            "client discovery",
            Status.WARN,
            f".well-known advertises {advertised!r}, expected {settings.public_base_url!r}",
        )
    return CheckResult(
        "client discovery", Status.OK, f"clients auto-discover {settings.public_base_url}"
    )


def _check_federation_routing(settings: NeuronServerSettings) -> CheckResult:
    base = pick_base_url(settings.name, None)
    return CheckResult(
        "federation routing",
        Status.OK,
        f"other servers reach {settings.name} at {base} "
        "(add a /.well-known/matrix/server delegation to change this)",
    )


async def _check_federation_self(
    settings: NeuronServerSettings,
    signing_key: SigningKey | None,
    fed_open_client: OpenClient | None,
) -> CheckResult:
    if signing_key is None:
        return CheckResult(
            "federation reachability",
            Status.WARN,
            "skipped — server not initialized yet (run it once to create its key)",
        )
    client = FederationClient(
        settings.name, signing_key, open_client=fed_open_client, timeout=_NET_TIMEOUT
    )
    base = pick_base_url(settings.name, None)
    try:
        doc = await client.get_json(settings.name, "/_matrix/key/v2/server", sign=False)
    except Exception as exc:
        return CheckResult(
            "federation reachability",
            Status.WARN,
            f"could not fetch keys over federation at {base}: {exc} "
            "(federation needs this server reachable over https)",
        )
    keys = parse_and_verify_key_document(doc, settings.name)
    if keys is None:
        return CheckResult(
            "federation reachability",
            Status.FAIL,
            "the published key document failed verification (bad signature or server_name)",
        )
    if signing_key.key_id not in keys:
        return CheckResult(
            "federation reachability",
            Status.WARN,
            f"reachable, but the answering server publishes {sorted(keys)}, "
            f"not this config's key {signing_key.key_id}",
        )
    return CheckResult(
        "federation reachability",
        Status.OK,
        f"reachable and self-signed correctly (key id {signing_key.key_id})",
    )


# --- orchestration ----------------------------------------------------------


async def run_checks(
    settings: NeuronServerSettings,
    *,
    offline: bool = False,
    http_client: httpx.AsyncClient | None = None,
    fed_open_client: OpenClient | None = None,
) -> list[CheckResult]:
    """Run every doctor check and return the results in display order."""
    results = [
        _check_server_name(settings),
        _check_public_base_url(settings),
        _check_bind(settings),
        _check_media_store(settings),
        _check_registration(settings),
        _check_admin_users(settings),
    ]
    db_results, signing_key = await _database_and_key_checks(settings)
    results.extend(db_results)

    if not offline:
        results.append(await _check_listening(settings))
        results.append(await _check_client_discovery(settings, http_client))
        results.append(_check_federation_routing(settings))
        results.append(await _check_federation_self(settings, signing_key, fed_open_client))

    return results


_SYMBOL = {Status.OK: "✓", Status.WARN: "!", Status.FAIL: "✗"}
_COLOR = {Status.OK: "\033[32m", Status.WARN: "\033[33m", Status.FAIL: "\033[31m"}
_RESET = "\033[0m"


def format_report(results: list[CheckResult], *, color: bool) -> str:
    """Render check results as an aligned, optionally-coloured report."""
    width = max((len(r.name) for r in results), default=0)
    lines = []
    for r in results:
        symbol = _SYMBOL[r.status]
        if color:
            symbol = f"{_COLOR[r.status]}{symbol}{_RESET}"
        lines.append(f" {symbol}  {r.name.ljust(width)}   {r.detail}")
    n_fail = sum(1 for r in results if r.status is Status.FAIL)
    n_warn = sum(1 for r in results if r.status is Status.WARN)
    n_ok = sum(1 for r in results if r.status is Status.OK)
    lines.append("")
    lines.append(f"{n_ok} ok · {n_warn} warning(s) · {n_fail} failure(s)")
    return "\n".join(lines)


def exit_code(results: list[CheckResult], *, strict: bool) -> int:
    """0 if healthy; 1 on any failure (or any warning when ``strict``)."""
    if any(r.status is Status.FAIL for r in results):
        return 1
    if strict and any(r.status is Status.WARN for r in results):
        return 1
    return 0


async def doctor_main(settings: NeuronServerSettings, *, offline: bool, strict: bool) -> int:
    """Run the checks, print the report, and return a process exit code."""
    import sys

    results = await run_checks(settings, offline=offline)
    print(format_report(results, color=sys.stdout.isatty()))
    return exit_code(results, strict=strict)

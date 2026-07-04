# SPDX-License-Identifier: Apache-2.0
"""Authentication & account domain service.

Orchestrates registration, login, logout, token lookup and device management on
top of the storage layer. It raises :class:`MatrixError` directly so the HTTP
layer can return spec-correct error bodies without an extra translation step.

Behaviour follows the Matrix Client-Server API (registration with the
``m.login.dummy`` UIA stage; ``m.login.password`` login; ``account/whoami``;
device endpoints).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from neuron_server.auth import ids
from neuron_server.auth.passwords import hash_password, verify_password
from neuron_server.auth.uia import UiaSessionStore
from neuron_server.clock import now_ms
from neuron_server.errors import MatrixError
from neuron_server.storage import accounts
from neuron_server.storage import admin as admin_store
from neuron_server.storage.database import Database


@dataclass(frozen=True)
class Authenticated:
    """The identity behind a valid access token."""

    user_id: str
    device_id: str


@dataclass(frozen=True)
class LoginResult:
    """A freshly-issued login (from register or login)."""

    user_id: str
    device_id: str
    access_token: str


class AuthService:
    """Account/authentication operations for one server."""

    def __init__(
        self,
        db: Database,
        server_name: str,
        registration_enabled: bool,
        *,
        first_user_admin: bool = False,
        uia_session_ttl_s: float = 3600.0,
    ) -> None:
        self._db = db
        self._server_name = server_name
        self._registration_enabled = registration_enabled
        self._first_user_admin = first_user_admin
        self._uia = UiaSessionStore(db, ttl_ms=int(uia_session_ttl_s * 1000))

    @property
    def registration_enabled(self) -> bool:
        return self._registration_enabled

    def _user_id(self, localpart: str) -> str:
        return f"@{localpart}:{self._server_name}"

    # --- UIA (registration uses the m.login.dummy stage) -------------------

    async def begin_uia(self) -> str:
        """Open a UIA session and return its id (for the 401 challenge body)."""
        return await self._uia.create()

    async def uia_satisfied(self, auth: Any) -> bool:
        """Return True if ``auth`` completes the dummy stage for a known session."""
        return (
            isinstance(auth, dict)
            and auth.get("type") == "m.login.dummy"
            and isinstance(auth.get("session"), str)
            and await self._uia.exists(auth["session"])
        )

    async def uia_password_submitted(self, auth: Any) -> bool:
        """True if ``auth`` is an m.login.password submission for an open session.

        This only checks the *shape* (stage type + known session); the password
        itself is verified separately by :meth:`verify_uia_password` so the HTTP
        layer can charge the login rate limiter between the two steps.
        """
        return (
            isinstance(auth, dict)
            and auth.get("type") == "m.login.password"
            and isinstance(auth.get("session"), str)
            and await self._uia.exists(auth["session"])
        )

    async def verify_uia_password(self, auth: dict[str, Any], user_id: str) -> None:
        """Verify the m.login.password UIA stage against ``user_id``'s password.

        The identifier (if supplied) must resolve to the authenticated user —
        UIA re-authenticates the *current* account, so a cross-user identifier
        is rejected rather than letting one user's session vouch for another.
        Raises M_FORBIDDEN on mismatch or wrong password; the UIA session stays
        open so the client may retry with the same session.
        """
        identifier = auth.get("identifier")
        supplied: Any = None
        if isinstance(identifier, dict) and identifier.get("type") == "m.id.user":
            supplied = identifier.get("user")
        elif "user" in auth:  # deprecated top-level field
            supplied = auth.get("user")
        if supplied is not None:
            if not isinstance(supplied, str) or not supplied:
                raise MatrixError(400, "M_INVALID_PARAM", "Invalid user identifier")
            supplied_id = supplied if supplied.startswith("@") else self._user_id(supplied)
            if supplied_id != user_id:
                raise MatrixError(
                    403, "M_FORBIDDEN", "Cannot authenticate as a different user"
                )

        password = auth.get("password")
        if not isinstance(password, str) or not password:
            raise MatrixError(400, "M_MISSING_PARAM", "Missing password")
        row = await accounts.get_user(self._db, user_id)
        if (
            row is None
            or row.password_hash is None
            or not verify_password(password, row.password_hash)
        ):
            raise MatrixError(403, "M_FORBIDDEN", "Invalid password")

    async def complete_uia(self, auth: Any) -> None:
        session = auth.get("session") if isinstance(auth, dict) else None
        if isinstance(session, str):
            await self._uia.complete(session)

    async def sweep_uia(self) -> None:
        """Remove expired UIA sessions (driven by a periodic sweeper)."""
        await self._uia.sweep_expired()

    # --- registration ------------------------------------------------------

    async def is_username_available(self, localpart: str) -> bool:
        """Return True if ``localpart`` is valid and not taken (else raise/return)."""
        if not ids.is_valid_localpart(localpart):
            raise MatrixError(400, "M_INVALID_USERNAME", "Invalid user name")
        return not await accounts.user_exists(self._db, self._user_id(localpart))

    async def register(
        self,
        *,
        localpart: str | None,
        password: str | None,
        device_id: str | None,
        initial_device_display_name: str | None,
        inhibit_login: bool,
    ) -> LoginResult | dict[str, Any]:
        """Create a new local account. Returns a login unless ``inhibit_login``."""
        if not password:
            raise MatrixError(400, "M_MISSING_PARAM", "Missing password")

        localpart = localpart or ids.generate_localpart()
        if not ids.is_valid_localpart(localpart):
            raise MatrixError(400, "M_INVALID_USERNAME", "Invalid user name")

        user_id = self._user_id(localpart)
        if not ids.is_valid_user_id(user_id):
            raise MatrixError(400, "M_INVALID_USERNAME", "User ID is too long")
        if await accounts.user_exists(self._db, user_id):
            raise MatrixError(400, "M_USER_IN_USE", "Desired user ID is already taken.")

        password_hash = hash_password(password)
        created_ts = now_ms()
        new_device_id = device_id or ids.generate_device_id()
        token = ids.generate_access_token()

        async with self._db.transaction():
            # The very first account may be made a server admin (the desktop
            # first-run flow uses this so the user who signs up owns the server).
            is_admin = self._first_user_admin and not await accounts.any_users(self._db)
            await accounts.create_user(self._db, user_id, password_hash, is_admin, created_ts)
            if not inhibit_login:
                await accounts.create_device(
                    self._db, user_id, new_device_id, initial_device_display_name, created_ts
                )
                await accounts.create_access_token(
                    self._db, token, user_id, new_device_id, created_ts
                )

        if inhibit_login:
            return {"user_id": user_id}
        return LoginResult(user_id=user_id, device_id=new_device_id, access_token=token)

    # --- login -------------------------------------------------------------

    async def login(
        self,
        *,
        user: str,
        password: str,
        device_id: str | None,
        initial_device_display_name: str | None,
    ) -> LoginResult:
        """Authenticate a password login and issue a new access token."""
        user_id = user if user.startswith("@") else self._user_id(user)
        row = await accounts.get_user(self._db, user_id)
        if (
            row is None
            or row.password_hash is None
            or not verify_password(password, row.password_hash)
        ):
            raise MatrixError(403, "M_FORBIDDEN", "Invalid username or password")
        if row.deactivated:
            raise MatrixError(403, "M_USER_DEACTIVATED", "This account has been deactivated")

        created_ts = now_ms()
        token = ids.generate_access_token()

        reuse = False
        if device_id:
            reuse = await accounts.device_exists(self._db, user_id, device_id)
        chosen_device = device_id if device_id else ids.generate_device_id()

        async with self._db.transaction():
            if reuse:
                # Reusing a device: invalidate its old tokens and (optionally) rename it.
                await accounts.delete_tokens_for_device(self._db, user_id, chosen_device)
                if initial_device_display_name is not None:
                    await accounts.set_device_display_name(
                        self._db, user_id, chosen_device, initial_device_display_name
                    )
            else:
                await accounts.create_device(
                    self._db, user_id, chosen_device, initial_device_display_name, created_ts
                )
            await accounts.create_access_token(self._db, token, user_id, chosen_device, created_ts)

        return LoginResult(user_id=user_id, device_id=chosen_device, access_token=token)

    # --- tokens / logout ---------------------------------------------------

    async def lookup_token(self, token: str) -> Authenticated | None:
        """Resolve an access token to its identity, or ``None`` if unknown."""
        row = await accounts.get_token(self._db, token)
        if row is None:
            return None
        return Authenticated(user_id=row[0], device_id=row[1])

    async def logout(self, auth: Authenticated) -> None:
        """Invalidate this token and delete its device (per the spec)."""
        async with self._db.transaction():
            await accounts.delete_tokens_for_device(self._db, auth.user_id, auth.device_id)
            await accounts.delete_device(self._db, auth.user_id, auth.device_id)

    async def logout_all(self, user_id: str) -> None:
        """Invalidate all of a user's tokens and delete all their devices."""
        async with self._db.transaction():
            await accounts.delete_tokens_for_user(self._db, user_id)
            await accounts.delete_all_devices(self._db, user_id)

    # --- self-serve account management --------------------------------------

    async def change_password(
        self, user_id: str, new_password: str, *, logout_devices: bool, keep_device_id: str
    ) -> None:
        """Set a new password; optionally revoke every *other* session.

        With ``logout_devices`` (the spec default) all of the user's devices and
        access tokens are deleted except the device the request came from, so
        the caller's session survives its own password change.
        """
        async with self._db.transaction():
            await admin_store.set_user_password(self._db, user_id, hash_password(new_password))
            if logout_devices:
                for device in await accounts.list_devices(self._db, user_id):
                    if device.device_id == keep_device_id:
                        continue
                    await accounts.delete_tokens_for_device(self._db, user_id, device.device_id)
                    await accounts.delete_device(self._db, user_id, device.device_id)

    async def deactivate_account(self, user_id: str) -> None:
        """Deactivate the account and revoke every session (mirrors admin deactivate,
        additionally deleting the device rows since no session may remain)."""
        async with self._db.transaction():
            await admin_store.set_user_deactivated(self._db, user_id, True)
            await accounts.delete_tokens_for_user(self._db, user_id)
            await accounts.delete_all_devices(self._db, user_id)

    # --- device management -------------------------------------------------

    async def list_devices(self, user_id: str) -> list[dict[str, Any]]:
        rows = await accounts.list_devices(self._db, user_id)
        return [{"device_id": r.device_id, "display_name": r.display_name} for r in rows]

    async def get_device(self, user_id: str, device_id: str) -> dict[str, Any]:
        row = await accounts.get_device(self._db, user_id, device_id)
        if row is None:
            raise MatrixError(404, "M_NOT_FOUND", "Unknown device")
        return {"device_id": row.device_id, "display_name": row.display_name}

    async def update_device(
        self, user_id: str, device_id: str, display_name: str | None
    ) -> None:
        if not await accounts.device_exists(self._db, user_id, device_id):
            raise MatrixError(404, "M_NOT_FOUND", "Unknown device")
        await accounts.set_device_display_name(self._db, user_id, device_id, display_name)

    async def delete_device(self, user_id: str, device_id: str) -> None:
        async with self._db.transaction():
            await accounts.delete_tokens_for_device(self._db, user_id, device_id)
            await accounts.delete_device(self._db, user_id, device_id)

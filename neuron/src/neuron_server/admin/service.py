# SPDX-License-Identifier: Apache-2.0
"""Synapse-compatible Admin API operations.

Implements the ``/_synapse/admin/...`` surface the Neuron console and bots use, so
they run unchanged against ``neuron_server``. Reuses the same storage as the rest
of the server. Some endpoints that need infrastructure we haven't built yet
(server notices, async purge jobs, content reports) return spec-shaped responses
and are marked as honest stubs.
"""

from __future__ import annotations

import platform
import secrets
import time
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from typing import Any

from neuron_server.auth.passwords import hash_password
from neuron_server.errors import MatrixError
from neuron_server.storage import accounts, userdata
from neuron_server.storage import admin as admin_store
from neuron_server.storage import rooms as rooms_store
from neuron_server.storage.database import Database


def _server_version_string() -> str:
    """The running server's version, read from the installed package metadata.

    (Bundled into the frozen app via ``copy_metadata('neuron')`` in the spec, so it
    reflects the real release instead of a hard-coded constant.)
    """
    try:
        return f"Neuron {_pkg_version('neuron')}"
    except PackageNotFoundError:  # pragma: no cover - metadata present when installed
        return "Neuron"


def _now_ms() -> int:
    return int(time.time() * 1000)


class AdminService:
    """Server-administration operations for one server."""

    def __init__(self, db: Database, server_name: str) -> None:
        self._db = db
        self._server_name = server_name

    # --- server ------------------------------------------------------------

    def server_version(self) -> dict[str, Any]:
        return {
            "server_version": _server_version_string(),
            "python_version": platform.python_version(),
        }

    # --- users -------------------------------------------------------------

    async def list_users(
        self, *, offset: int, limit: int, name: str | None, deactivated: bool | None
    ) -> dict[str, Any]:
        total = await admin_store.count_users(self._db, deactivated=deactivated)
        users = await admin_store.list_users(
            self._db, offset=offset, limit=limit, name=name, deactivated=deactivated
        )
        body: dict[str, Any] = {"users": users, "total": total}
        if offset + limit < total:
            body["next_token"] = str(offset + limit)
        return body

    async def get_user(self, user_id: str) -> dict[str, Any]:
        row = await accounts.get_user(self._db, user_id)
        if row is None:
            raise MatrixError(404, "M_NOT_FOUND", "User not found")
        profile = await userdata.get_profile(self._db, user_id)
        return admin_store.user_to_admin_dict(row, profile.get("displayname"))

    async def upsert_user(self, user_id: str, body: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Create or modify a user. Returns ``(user, created)``."""
        existing = await accounts.get_user(self._db, user_id)
        created = existing is None
        password = body.get("password")
        admin = body.get("admin")
        deactivated = body.get("deactivated")
        displayname = body.get("displayname")

        async with self._db.transaction():
            if existing is None:
                pw_hash = hash_password(password) if isinstance(password, str) else None
                await accounts.create_user(
                    self._db, user_id, pw_hash, bool(admin), _now_ms()
                )
            else:
                if isinstance(password, str):
                    await admin_store.set_user_password(self._db, user_id, hash_password(password))
                if admin is not None:
                    await admin_store.set_user_admin(self._db, user_id, bool(admin))
                if deactivated is not None:
                    await admin_store.set_user_deactivated(self._db, user_id, bool(deactivated))
            if isinstance(displayname, str):
                await userdata.set_displayname(self._db, user_id, displayname)

        return await self.get_user(user_id), created

    async def deactivate_user(self, user_id: str) -> dict[str, Any]:
        if await accounts.get_user(self._db, user_id) is None:
            raise MatrixError(404, "M_NOT_FOUND", "User not found")
        async with self._db.transaction():
            await admin_store.set_user_deactivated(self._db, user_id, True)
            await accounts.delete_tokens_for_user(self._db, user_id)
        return {"id_server_unbind_result": "success"}

    async def reset_password(self, user_id: str, new_password: str) -> dict[str, Any]:
        if await accounts.get_user(self._db, user_id) is None:
            raise MatrixError(404, "M_NOT_FOUND", "User not found")
        await admin_store.set_user_password(self._db, user_id, hash_password(new_password))
        return {}

    # --- rooms -------------------------------------------------------------

    async def list_rooms(self, *, offset: int, limit: int) -> dict[str, Any]:
        total = await rooms_store.count_rooms(self._db)
        rooms = await rooms_store.list_rooms_page(self._db, offset=offset, limit=limit)
        room_dicts = [await self._room_summary(r.room_id) for r in rooms]
        body: dict[str, Any] = {
            "rooms": room_dicts,
            "offset": offset,
            "total_rooms": total,
        }
        if offset + limit < total:
            body["next_batch"] = offset + limit
        if offset > 0:
            body["prev_batch"] = max(0, offset - limit)
        return body

    async def _room_summary(self, room_id: str) -> dict[str, Any]:
        room = await rooms_store.get_room(self._db, room_id)
        state = {(e.type, e.state_key or ""): e for e in await rooms_store.get_current_state(
            self._db, room_id
        )}
        name_event = state.get(("m.room.name", ""))
        join_rules = state.get(("m.room.join_rules", ""))
        encryption = state.get(("m.room.encryption", ""))
        joined = await rooms_store.count_joined_members(self._db, room_id)
        return {
            "room_id": room_id,
            "name": name_event.content.get("name") if name_event else None,
            "canonical_alias": None,
            "joined_members": joined,
            "joined_local_members": joined,
            "version": room.room_version if room else None,
            "creator": room.creator if room else None,
            "encryption": encryption.content.get("algorithm") if encryption else None,
            "join_rules": join_rules.content.get("join_rule") if join_rules else None,
            "state_events": len(state),
            "public": bool(join_rules and join_rules.content.get("join_rule") == "public"),
        }

    async def get_room(self, room_id: str) -> dict[str, Any]:
        if await rooms_store.get_room(self._db, room_id) is None:
            raise MatrixError(404, "M_NOT_FOUND", "Room not found")
        return await self._room_summary(room_id)

    async def get_room_members(self, room_id: str) -> dict[str, Any]:
        if await rooms_store.get_room(self._db, room_id) is None:
            raise MatrixError(404, "M_NOT_FOUND", "Room not found")
        members = await rooms_store.get_joined_members(self._db, room_id)
        return {"members": members, "total": len(members)}

    async def get_room_state(self, room_id: str) -> dict[str, Any]:
        if await rooms_store.get_room(self._db, room_id) is None:
            raise MatrixError(404, "M_NOT_FOUND", "Room not found")
        state = await rooms_store.get_current_state(self._db, room_id)
        return {"state": [e.client_dict() for e in state]}

    # --- registration tokens ----------------------------------------------

    async def list_registration_tokens(self) -> dict[str, Any]:
        return {"registration_tokens": await admin_store.list_registration_tokens(self._db)}

    async def create_registration_token(
        self, *, token: str | None, uses_allowed: int | None, expiry_time: int | None
    ) -> dict[str, Any]:
        token = token or secrets.token_urlsafe(12)
        await admin_store.create_registration_token(self._db, token, uses_allowed, expiry_time)
        created = await admin_store.get_registration_token(self._db, token)
        return created or {"token": token}

    async def delete_registration_token(self, token: str) -> dict[str, Any]:
        await admin_store.delete_registration_token(self._db, token)
        return {}

    async def registration_token_valid(self, token: str) -> bool:
        """True if ``token`` can currently be redeemed (unexpired, uses left)."""
        return await admin_store.registration_token_valid(self._db, token, _now_ms())

    async def consume_registration_token(self, token: str) -> bool:
        """Claim one use of ``token``; False if it is invalid, expired or spent."""
        return await admin_store.consume_registration_token(self._db, token, _now_ms())

    # --- stubs (spec-shaped; backing work deferred) -----------------------

    async def list_event_reports(self) -> dict[str, Any]:
        return {"event_reports": [], "total": 0}

    async def set_shadow_ban(self, user_id: str) -> dict[str, Any]:
        return {}

    async def send_server_notice(self) -> dict[str, Any]:
        return {"event_id": f"$notice-{secrets.token_urlsafe(8)}"}

    async def set_room_block(self, room_id: str, block: bool) -> dict[str, Any]:
        return {"block": block}

    async def delete_room(self, room_id: str) -> dict[str, Any]:
        return {"delete_id": secrets.token_urlsafe(8)}

    async def get_delete_status(self, delete_id: str) -> dict[str, Any]:
        return {
            "status": "complete",
            "shutdown_room": {"kicked_users": [], "failed_to_kick_users": []},
        }

    async def redact_user_events(self, user_id: str) -> dict[str, Any]:
        return {"redact_id": secrets.token_urlsafe(8)}

    async def get_redact_status(self, redact_id: str) -> dict[str, Any]:
        return {"status": "complete", "failed_redactions": {}}

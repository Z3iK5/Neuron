# SPDX-License-Identifier: Apache-2.0
"""Client-Server API: server-side key backup (``/room_keys``).

Element's "Secure Backup": clients upload their encrypted megolm session keys
under a backup version so message history is recoverable with the recovery key
after losing all devices. The server never decrypts anything.

Writes must target the *current* version; a stale version gets 403
``M_WRONG_ROOM_KEYS_VERSION`` with ``current_version``, which is how clients
learn to rotate to the new backup. Reads may target any live version.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from neuron_server.api.deps import json_body, require_user
from neuron_server.auth.service import Authenticated
from neuron_server.errors import MatrixError
from neuron_server.storage import keybackup
from neuron_server.storage.database import Database
from neuron_server.storage.keybackup import BackupVersion

router = APIRouter(prefix="/_matrix/client")


def get_db(request: Request) -> Database:
    db: Database = request.app.state.db
    return db


def _parse_version(version: str) -> int | None:
    """Versions are served as strings but stored as integers; anything that isn't
    a positive integer can't name an existing backup."""
    try:
        parsed = int(version)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


async def _get_version_or_404(db: Database, user_id: str, version: str) -> BackupVersion:
    parsed = _parse_version(version)
    info = None if parsed is None else await keybackup.get_version(db, user_id, parsed)
    if info is None:
        raise MatrixError(404, "M_NOT_FOUND", "Unknown backup version")
    return info


def _require_version_param(request: Request) -> str:
    version = request.query_params.get("version")
    if not version:
        raise MatrixError(400, "M_MISSING_PARAM", "Missing version query parameter")
    return version


async def _current_version_for_write(
    db: Database, user_id: str, version: str
) -> BackupVersion:
    """Resolve a ``?version=`` for a write: it must be the current version."""
    current = await keybackup.get_version(db, user_id)
    if current is None:
        raise MatrixError(404, "M_NOT_FOUND", "No backup version exists")
    if _parse_version(version) != current.version:
        raise MatrixError(
            403,
            "M_WRONG_ROOM_KEYS_VERSION",
            "Wrong backup version.",
            extra={"current_version": str(current.version)},
        )
    return current


def _version_info(info: BackupVersion, count: int) -> dict[str, Any]:
    return {
        "algorithm": info.algorithm,
        "auth_data": info.auth_data,
        "count": count,
        "etag": str(info.etag),
        "version": str(info.version),
    }


async def _count_etag(db: Database, user_id: str, version: int) -> dict[str, Any]:
    """The ``{count, etag}`` body for key writes — re-read after the change."""
    info = await keybackup.get_version(db, user_id, version)
    etag = info.etag if info is not None else 0
    return {
        "count": await keybackup.count_sessions(db, user_id, version),
        "etag": str(etag),
    }


def _validate_sessions(rooms: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Validate the ``{room_id: {sessions: {session_id: KeyBackupData}}}`` shape."""
    for room_id, room in rooms.items():
        if not isinstance(room, dict) or not isinstance(room.get("sessions"), dict):
            raise MatrixError(400, "M_BAD_JSON", f"Malformed sessions for room {room_id}")
        for session_id, data in room["sessions"].items():
            if not isinstance(data, dict):
                raise MatrixError(400, "M_BAD_JSON", f"Malformed key data for {session_id}")
            fmi, fc = data.get("first_message_index"), data.get("forwarded_count")
            if (
                not isinstance(fmi, int)
                or not isinstance(fc, int)
                or isinstance(fmi, bool)
                or isinstance(fc, bool)
                or not isinstance(data.get("is_verified"), bool)
                or not isinstance(data.get("session_data"), dict)
            ):
                raise MatrixError(400, "M_BAD_JSON", f"Malformed key data for {session_id}")
    return rooms


# --- backup versions ---------------------------------------------------------


@router.post("/v3/room_keys/version")
async def create_backup_version(
    request: Request,
    who: Authenticated = Depends(require_user),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    body = await json_body(request)
    algorithm, auth_data = body.get("algorithm"), body.get("auth_data")
    if not isinstance(algorithm, str) or not algorithm:
        raise MatrixError(400, "M_MISSING_PARAM", "Missing algorithm")
    if not isinstance(auth_data, dict):
        raise MatrixError(400, "M_MISSING_PARAM", "Missing auth_data")
    version = await keybackup.create_version(db, who.user_id, algorithm, auth_data)
    return {"version": str(version)}


@router.get("/v3/room_keys/version")
async def get_current_backup_version(
    who: Authenticated = Depends(require_user),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    info = await keybackup.get_version(db, who.user_id)
    if info is None:
        raise MatrixError(404, "M_NOT_FOUND", "No current backup version")
    count = await keybackup.count_sessions(db, who.user_id, info.version)
    return _version_info(info, count)


@router.get("/v3/room_keys/version/{version}")
async def get_backup_version(
    version: str,
    who: Authenticated = Depends(require_user),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    info = await _get_version_or_404(db, who.user_id, version)
    count = await keybackup.count_sessions(db, who.user_id, info.version)
    return _version_info(info, count)


@router.put("/v3/room_keys/version/{version}")
async def update_backup_version(
    version: str,
    request: Request,
    who: Authenticated = Depends(require_user),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    body = await json_body(request)
    info = await _get_version_or_404(db, who.user_id, version)
    auth_data = body.get("auth_data")
    if not isinstance(auth_data, dict):
        raise MatrixError(400, "M_MISSING_PARAM", "Missing auth_data")
    if body.get("algorithm") != info.algorithm:
        raise MatrixError(400, "M_INVALID_PARAM", "Algorithm does not match the backup")
    body_version = body.get("version")
    if body_version is not None and body_version != str(info.version):
        raise MatrixError(400, "M_INVALID_PARAM", "Version in body does not match path")
    await keybackup.update_version_auth_data(db, who.user_id, info.version, auth_data)
    return {}


@router.delete("/v3/room_keys/version/{version}")
async def delete_backup_version(
    version: str,
    who: Authenticated = Depends(require_user),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    info = await _get_version_or_404(db, who.user_id, version)
    await keybackup.delete_version(db, who.user_id, info.version)
    return {}


# --- keys --------------------------------------------------------------------


@router.put("/v3/room_keys/keys")
async def put_all_room_keys(
    request: Request,
    who: Authenticated = Depends(require_user),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    version = _require_version_param(request)
    body = await json_body(request)
    rooms = body.get("rooms")
    if not isinstance(rooms, dict):
        raise MatrixError(400, "M_BAD_JSON", "Missing rooms object")
    current = await _current_version_for_write(db, who.user_id, version)
    await keybackup.put_keys(db, who.user_id, current.version, _validate_sessions(rooms))
    return await _count_etag(db, who.user_id, current.version)


@router.put("/v3/room_keys/keys/{room_id}")
async def put_room_keys(
    room_id: str,
    request: Request,
    who: Authenticated = Depends(require_user),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    version = _require_version_param(request)
    body = await json_body(request)
    current = await _current_version_for_write(db, who.user_id, version)
    rooms = _validate_sessions({room_id: body})
    await keybackup.put_keys(db, who.user_id, current.version, rooms)
    return await _count_etag(db, who.user_id, current.version)


@router.put("/v3/room_keys/keys/{room_id}/{session_id}")
async def put_session_key(
    room_id: str,
    session_id: str,
    request: Request,
    who: Authenticated = Depends(require_user),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    version = _require_version_param(request)
    body = await json_body(request)
    current = await _current_version_for_write(db, who.user_id, version)
    rooms = _validate_sessions({room_id: {"sessions": {session_id: body}}})
    await keybackup.put_keys(db, who.user_id, current.version, rooms)
    return await _count_etag(db, who.user_id, current.version)


@router.get("/v3/room_keys/keys")
async def get_all_room_keys(
    request: Request,
    who: Authenticated = Depends(require_user),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    version = _require_version_param(request)
    info = await _get_version_or_404(db, who.user_id, version)
    return {"rooms": await keybackup.get_keys(db, who.user_id, info.version)}


@router.get("/v3/room_keys/keys/{room_id}")
async def get_room_keys(
    room_id: str,
    request: Request,
    who: Authenticated = Depends(require_user),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    version = _require_version_param(request)
    info = await _get_version_or_404(db, who.user_id, version)
    rooms = await keybackup.get_keys(db, who.user_id, info.version, room_id)
    if room_id not in rooms:
        raise MatrixError(404, "M_NOT_FOUND", "No backed-up keys for this room")
    return rooms[room_id]


@router.get("/v3/room_keys/keys/{room_id}/{session_id}")
async def get_session_key(
    room_id: str,
    session_id: str,
    request: Request,
    who: Authenticated = Depends(require_user),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    version = _require_version_param(request)
    info = await _get_version_or_404(db, who.user_id, version)
    rooms = await keybackup.get_keys(db, who.user_id, info.version, room_id, session_id)
    sessions = rooms.get(room_id, {}).get("sessions", {})
    if session_id not in sessions:
        raise MatrixError(404, "M_NOT_FOUND", "No backed-up key for this session")
    data: dict[str, Any] = sessions[session_id]
    return data


@router.delete("/v3/room_keys/keys")
async def delete_all_room_keys(
    request: Request,
    who: Authenticated = Depends(require_user),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    version = _require_version_param(request)
    current = await _current_version_for_write(db, who.user_id, version)
    await keybackup.delete_keys(db, who.user_id, current.version)
    return await _count_etag(db, who.user_id, current.version)


@router.delete("/v3/room_keys/keys/{room_id}")
async def delete_room_keys(
    room_id: str,
    request: Request,
    who: Authenticated = Depends(require_user),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    version = _require_version_param(request)
    current = await _current_version_for_write(db, who.user_id, version)
    await keybackup.delete_keys(db, who.user_id, current.version, room_id)
    return await _count_etag(db, who.user_id, current.version)


@router.delete("/v3/room_keys/keys/{room_id}/{session_id}")
async def delete_session_key(
    room_id: str,
    session_id: str,
    request: Request,
    who: Authenticated = Depends(require_user),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    version = _require_version_param(request)
    current = await _current_version_for_write(db, who.user_id, version)
    await keybackup.delete_keys(db, who.user_id, current.version, room_id, session_id)
    return await _count_etag(db, who.user_id, current.version)

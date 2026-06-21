# SPDX-License-Identifier: Apache-2.0
"""Client-Server API: identity & authentication endpoints (HS-1).

Implements registration, login, logout, ``account/whoami`` and device management
from the Matrix Client-Server API. Request bodies are parsed leniently (the spec
allows extra fields) and errors use the spec's ``M_*`` error bodies.

HS-1 simplification: device update/delete are authenticated by access token only
(the spec also gates device deletion behind UIA; that is added with the broader
UIA work in a later phase).
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Request
from starlette.responses import JSONResponse

from neuron_server.api.deps import get_auth, require_user
from neuron_server.auth.service import Authenticated, AuthService, LoginResult
from neuron_server.errors import MatrixError

router = APIRouter(prefix="/_matrix/client")


async def _json_body(request: Request) -> dict[str, Any]:
    """Parse the JSON request body, or raise the spec's M_NOT_JSON error."""
    raw = await request.body()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise MatrixError(400, "M_NOT_JSON", "Request body is not valid JSON") from exc
    if not isinstance(data, dict):
        raise MatrixError(400, "M_BAD_JSON", "Request body must be a JSON object")
    return data


def _login_payload(result: LoginResult) -> dict[str, Any]:
    return {
        "user_id": result.user_id,
        "access_token": result.access_token,
        "device_id": result.device_id,
    }


# --- registration ----------------------------------------------------------


@router.post("/v3/register")
async def register(request: Request, auth: AuthService = Depends(get_auth)) -> Any:
    if request.query_params.get("kind", "user") == "guest":
        raise MatrixError(403, "M_FORBIDDEN", "Guest registration is not supported")
    if not auth.registration_enabled:
        raise MatrixError(403, "M_FORBIDDEN", "Registration is disabled on this server")

    body = await _json_body(request)
    auth_data = body.get("auth")
    if not auth.uia_satisfied(auth_data):
        # Challenge: ask the client to complete the dummy stage and retry.
        session = auth.begin_uia()
        return JSONResponse(
            status_code=401,
            content={
                "session": session,
                "flows": [{"stages": ["m.login.dummy"]}],
                "params": {},
                "completed": [],
            },
        )

    result = await auth.register(
        localpart=body.get("username"),
        password=body.get("password"),
        device_id=body.get("device_id"),
        initial_device_display_name=body.get("initial_device_display_name"),
        inhibit_login=bool(body.get("inhibit_login", False)),
    )
    auth.complete_uia(auth_data)

    if isinstance(result, LoginResult):
        return _login_payload(result)
    return result  # inhibit_login -> {"user_id": ...}


@router.get("/v3/register/available")
async def register_available(
    request: Request, auth: AuthService = Depends(get_auth)
) -> dict[str, Any]:
    username = request.query_params.get("username")
    if not username:
        raise MatrixError(400, "M_MISSING_PARAM", "Missing 'username' query parameter")
    if not await auth.is_username_available(username):
        raise MatrixError(400, "M_USER_IN_USE", "Desired user ID is already taken.")
    return {"available": True}


# --- login -----------------------------------------------------------------


@router.get("/v3/login")
async def login_flows() -> dict[str, Any]:
    return {"flows": [{"type": "m.login.password"}]}


def _extract_login_user(body: dict[str, Any]) -> str | None:
    identifier = body.get("identifier")
    if isinstance(identifier, dict) and identifier.get("type") == "m.id.user":
        user = identifier.get("user")
        return user if isinstance(user, str) else None
    legacy = body.get("user")  # deprecated top-level field
    return legacy if isinstance(legacy, str) else None


@router.post("/v3/login")
async def login(request: Request, auth: AuthService = Depends(get_auth)) -> dict[str, Any]:
    body = await _json_body(request)
    if body.get("type") != "m.login.password":
        raise MatrixError(400, "M_UNKNOWN", "Unsupported login type")
    user = _extract_login_user(body)
    if not user:
        raise MatrixError(400, "M_INVALID_PARAM", "Missing or invalid user identifier")
    password = body.get("password")
    if not isinstance(password, str) or not password:
        raise MatrixError(400, "M_MISSING_PARAM", "Missing password")

    # Throttle login attempts per account (brute-force defence) before doing the
    # expensive password verification.
    request.app.state.rate_limiters.check_login(user)

    result = await auth.login(
        user=user,
        password=password,
        device_id=body.get("device_id"),
        initial_device_display_name=body.get("initial_device_display_name"),
    )
    return _login_payload(result)


# --- logout / whoami -------------------------------------------------------


@router.post("/v3/logout")
async def logout(
    who: Authenticated = Depends(require_user), auth: AuthService = Depends(get_auth)
) -> dict[str, Any]:
    await auth.logout(who)
    return {}


@router.post("/v3/logout/all")
async def logout_all(
    who: Authenticated = Depends(require_user), auth: AuthService = Depends(get_auth)
) -> dict[str, Any]:
    await auth.logout_all(who.user_id)
    return {}


@router.get("/v3/account/whoami")
async def whoami(who: Authenticated = Depends(require_user)) -> dict[str, Any]:
    return {"user_id": who.user_id, "device_id": who.device_id, "is_guest": False}


# --- devices ---------------------------------------------------------------


@router.get("/v3/devices")
async def get_devices(
    who: Authenticated = Depends(require_user), auth: AuthService = Depends(get_auth)
) -> dict[str, Any]:
    return {"devices": await auth.list_devices(who.user_id)}


@router.get("/v3/devices/{device_id}")
async def get_device(
    device_id: str,
    who: Authenticated = Depends(require_user),
    auth: AuthService = Depends(get_auth),
) -> dict[str, Any]:
    return await auth.get_device(who.user_id, device_id)


@router.put("/v3/devices/{device_id}")
async def update_device(
    device_id: str,
    request: Request,
    who: Authenticated = Depends(require_user),
    auth: AuthService = Depends(get_auth),
) -> dict[str, Any]:
    body = await _json_body(request)
    await auth.update_device(who.user_id, device_id, body.get("display_name"))
    return {}


@router.delete("/v3/devices/{device_id}")
async def delete_device(
    device_id: str,
    who: Authenticated = Depends(require_user),
    auth: AuthService = Depends(get_auth),
) -> dict[str, Any]:
    await auth.delete_device(who.user_id, device_id)
    return {}

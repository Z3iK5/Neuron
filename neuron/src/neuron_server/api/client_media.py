# SPDX-License-Identifier: Apache-2.0
"""Client-Server API: media repository (HS-4).

Authenticated upload, plus authenticated download/thumbnail (the newer
``/_matrix/client/v1/media/...`` endpoints) with the legacy ``/_matrix/media/v3``
paths served too (also requiring auth here). Only local media is served; remote
(federated) media is part of HS-7.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from starlette.responses import Response

from neuron_server.api.deps import require_user
from neuron_server.auth.service import Authenticated
from neuron_server.media.service import MediaContent, MediaService

router = APIRouter(prefix="/_matrix")


def get_media(request: Request) -> MediaService:
    service: MediaService = request.app.state.media
    return service


def _media_response(content: MediaContent) -> Response:
    disposition = MediaService.disposition_type(content.content_type)
    if content.upload_name:
        disposition += f"; filename*=utf-8''{quote(content.upload_name)}"
    return Response(
        content=content.data,
        media_type=content.content_type,
        headers={"Content-Disposition": disposition},
    )


# --- config ----------------------------------------------------------------


@router.get("/media/v3/config")
@router.get("/client/v1/media/config")
async def media_config(
    who: Authenticated = Depends(require_user),
    media: MediaService = Depends(get_media),
) -> dict[str, Any]:
    return media.config()


# --- upload ----------------------------------------------------------------


@router.post("/media/v3/upload")
async def upload(
    request: Request,
    who: Authenticated = Depends(require_user),
    media: MediaService = Depends(get_media),
) -> dict[str, Any]:
    data = await request.body()
    content_type = request.headers.get("Content-Type", "application/octet-stream")
    upload_name = request.query_params.get("filename")
    content_uri = await media.upload(who.user_id, data, content_type, upload_name)
    return {"content_uri": content_uri}


# --- download --------------------------------------------------------------


@router.get("/client/v1/media/download/{server_name}/{media_id}")
@router.get("/client/v1/media/download/{server_name}/{media_id}/{file_name}")
@router.get("/media/v3/download/{server_name}/{media_id}")
@router.get("/media/v3/download/{server_name}/{media_id}/{file_name}")
async def download(
    server_name: str,
    media_id: str,
    who: Authenticated = Depends(require_user),
    media: MediaService = Depends(get_media),
) -> Response:
    content = await media.download(server_name, media_id)
    return _media_response(content)


# --- thumbnail -------------------------------------------------------------


@router.get("/client/v1/media/thumbnail/{server_name}/{media_id}")
@router.get("/media/v3/thumbnail/{server_name}/{media_id}")
async def thumbnail(
    server_name: str,
    media_id: str,
    request: Request,
    who: Authenticated = Depends(require_user),
    media: MediaService = Depends(get_media),
) -> Response:
    try:
        width = int(request.query_params.get("width", "96"))
        height = int(request.query_params.get("height", "96"))
    except ValueError:
        width, height = 96, 96
    method = request.query_params.get("method", "scale")
    content = await media.thumbnail(server_name, media_id, width, height, method)
    return _media_response(content)

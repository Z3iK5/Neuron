# SPDX-License-Identifier: Apache-2.0
"""Federation media API — serving our media to other homeservers (spec v1.11).

Authenticated media (MSC3916): a remote server fetches media hosted here over
signed federation, and we answer with a ``multipart/mixed`` body — a JSON metadata
part followed by the raw bytes. Only media whose ``server_name`` is us is served;
anything else (or unknown) is ``M_NOT_FOUND``. Requests are X-Matrix signed like
every other federation route.
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Request
from starlette.responses import Response

from neuron_server.federation.request import authenticate_request
from neuron_server.media.multipart import build_multipart
from neuron_server.media.service import MediaContent, MediaService

router = APIRouter(prefix="/_matrix/federation/v1/media")


def _multipart_response(content: MediaContent) -> Response:
    disposition = MediaService.disposition_type(content.content_type)
    if content.upload_name:
        disposition += f"; filename*=utf-8''{quote(content.upload_name)}"
    boundary, body = build_multipart({}, content.content_type, content.data, disposition)
    return Response(content=body, media_type=f'multipart/mixed; boundary="{boundary}"')


@router.get("/download/{media_id}")
async def federation_download(media_id: str, request: Request) -> Response:
    await authenticate_request(request)
    media: MediaService = request.app.state.media
    # Serving our own name only returns local media (or M_NOT_FOUND); it never
    # recurses back out over federation.
    content = await media.download(request.app.state.settings.name, media_id)
    return _multipart_response(content)


@router.get("/thumbnail/{media_id}")
async def federation_thumbnail(media_id: str, request: Request) -> Response:
    await authenticate_request(request)
    media: MediaService = request.app.state.media
    try:
        width = int(request.query_params.get("width", "96"))
        height = int(request.query_params.get("height", "96"))
    except ValueError:
        width, height = 96, 96
    method = request.query_params.get("method", "scale")
    content = await media.thumbnail(
        request.app.state.settings.name, media_id, width, height, method
    )
    return _multipart_response(content)

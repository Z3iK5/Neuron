# SPDX-License-Identifier: Apache-2.0
"""Image thumbnailing for the media repository (via Pillow).

``make_thumbnail`` returns the resized image bytes + content type, or ``None`` if
the data is not an image we can process (the caller then falls back to the
original). ``method`` is ``scale`` (fit within the box, preserve aspect ratio) or
``crop`` (cover the box and centre-crop).
"""

from __future__ import annotations

from io import BytesIO

_MAX_DIMENSION = 2048


def make_thumbnail(
    data: bytes, width: int, height: int, method: str
) -> tuple[bytes, str] | None:
    """Return ``(png_bytes, "image/png")`` for a thumbnail, or ``None`` if not an image."""
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return None

    width = max(1, min(width, _MAX_DIMENSION))
    height = max(1, min(height, _MAX_DIMENSION))

    try:
        image: Image.Image = Image.open(BytesIO(data))
        image.load()
    except Exception:
        return None

    if image.mode not in ("RGB", "RGBA"):
        image = image.convert("RGBA" if "A" in image.getbands() else "RGB")

    if method == "crop":
        thumb = ImageOps.fit(image, (width, height))
    else:  # "scale" (default)
        thumb = image.copy()
        thumb.thumbnail((width, height))

    out = BytesIO()
    thumb.save(out, format="PNG")
    return out.getvalue(), "image/png"

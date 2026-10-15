# -*- coding: utf-8 -*-
"""Normalize oversized item photo uploads before they reach storage.

The form layer already proves that Pillow can decode an upload. This module
adds the two guards that a campus marketplace actually needs: reject files
which are unreasonable to decode, and avoid storing a phone-original photo when
all read paths only need a web-sized image.

The function deliberately returns the original upload when it is already small.
Re-encoding a small image would trade visible quality for no meaningful
bandwidth saving.
"""

from io import BytesIO
from uuid import uuid4

from django.core.files.base import ContentFile
from PIL import Image, ImageOps, UnidentifiedImageError

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_UPLOAD_PIXELS = 20_000_000
OPTIMIZE_THRESHOLD_BYTES = 1536 * 1024
MAX_EDGE_PIXELS = 1600
WEBP_QUALITY_LADDER = (85, 75, 65)


class ItemImageLimitError(ValueError):
    """The upload exceeds a declared hard limit."""


def _as_rgb_or_rgba(image):
    """Put uncommon Pillow modes into a mode WebP can encode without loss."""
    if image.mode in ('RGB', 'RGBA'):
        return image
    if 'A' in image.getbands() or image.mode in ('LA', 'PA'):
        return image.convert('RGBA')
    return image.convert('RGB')


def normalize_item_image(uploaded):
    """Return a storable upload, downscaling only when a threshold is crossed.

    Hard size and pixel limits are checked before pixel data is loaded. Existing
    bytes are returned untouched when both the dimensions and file size are
    already web-friendly.
    """
    if uploaded is None:
        return uploaded
    if uploaded.size is None or uploaded.size > MAX_UPLOAD_BYTES:
        raise ItemImageLimitError('single-file-too-large')

    uploaded.file.seek(0)
    try:
        with Image.open(uploaded.file) as source:
            width, height = source.size
            if width * height > MAX_UPLOAD_PIXELS:
                raise ItemImageLimitError('too-many-pixels')
            if (
                width <= MAX_EDGE_PIXELS
                and height <= MAX_EDGE_PIXELS
                and uploaded.size <= OPTIMIZE_THRESHOLD_BYTES
            ):
                return uploaded

            # `Image.open` is lazy; `exif_transpose` is the first operation that
            # needs the full raster, and it happens only after the hard limits.
            oriented = ImageOps.exif_transpose(source)
            oriented.thumbnail(
                (MAX_EDGE_PIXELS, MAX_EDGE_PIXELS),
                Image.Resampling.LANCZOS,
                reducing_gap=3.0,
            )
            normalized = _as_rgb_or_rgba(oriented)
    except ItemImageLimitError:
        raise
    except UnidentifiedImageError as exc:
        raise ValueError('undecodable-image') from exc

    output = None
    for quality in WEBP_QUALITY_LADDER:
        output = BytesIO()
        normalized.save(
            output, format='WEBP', quality=quality, method=4,
        )
        if output.tell() <= OPTIMIZE_THRESHOLD_BYTES:
            break

    output.seek(0)
    result = ContentFile(output.read(), name=f'{uuid4().hex}.webp')
    result.content_type = 'image/webp'
    return result

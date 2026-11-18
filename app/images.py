"""Uploaded images: check, clean and resize.

Each upload is decoded with Pillow (so only real JPEG, PNG or WebP images get through), turned
the right way up from its EXIF orientation, and saved again as JPEG at two widths: 1200 px for
pages and 400 px for cards. Re-encoding drops all metadata, including any GPS location.
"""

import base64
import binascii
import io
import uuid
from dataclasses import dataclass

from PIL import Image, ImageOps, UnidentifiedImageError

from app.storage import PUBLIC_PREFIX, get_storage

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_PIXELS = 40_000_000  # refuse "decompression bombs" (tiny files that unpack to huge images)
WIDTHS = (1200, 400)
ALLOWED = {"JPEG", "PNG", "WEBP"}
IMAGE_MSG = "Upload a JPEG, PNG or WebP image of up to 5 MB."


class ImageError(ValueError):
    pass


@dataclass(frozen=True)
class StoredImage:
    key: str  # base key: files are f"{key}-1200.jpg" and f"{key}-400.jpg"
    width: int  # of the 1200 version
    height: int


def decode_data_url(value: object) -> bytes:
    """The bytes of a data URL (data:image/...;base64,...) of at most 5 MB."""
    if not isinstance(value, str) or not value.startswith("data:image/") or ";base64," not in value[:40]:
        raise ImageError(IMAGE_MSG)
    encoded = value.split(";base64,", 1)[1]
    if len(encoded) > MAX_UPLOAD_BYTES * 4 // 3 + 4:
        raise ImageError(IMAGE_MSG)
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise ImageError(IMAGE_MSG) from None
    if len(data) > MAX_UPLOAD_BYTES:
        raise ImageError(IMAGE_MSG)
    return data


def _open(data: bytes) -> Image.Image:
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        img = Image.open(io.BytesIO(data))
        if img.format not in ALLOWED:
            raise ImageError(IMAGE_MSG)
        img.load()
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, SyntaxError):
        raise ImageError(IMAGE_MSG) from None
    if min(img.size) < 200:
        raise ImageError("That image is too small. Use one at least 200 pixels on each side.")
    img = ImageOps.exif_transpose(img)
    if img.mode != "RGB":  # e.g. a PNG with transparency: put it on white
        background = Image.new("RGB", img.size, "white")
        background.paste(img, mask=img.convert("RGBA").getchannel("A"))
        img = background
    return img


def _jpeg(img: Image.Image, width: int) -> tuple[bytes, tuple[int, int]]:
    copy = img.copy()
    if copy.width > width:
        copy = copy.resize((width, round(copy.height * width / copy.width)), Image.LANCZOS)
    out = io.BytesIO()
    copy.save(out, "JPEG", quality=82, optimize=True, progressive=True)
    return out.getvalue(), copy.size


def store(data: bytes, folder: str) -> StoredImage:
    """Check, resize and store an image under public/<folder>/. Raises ImageError."""
    img = _open(data)
    key = f"{PUBLIC_PREFIX}{folder}/{uuid.uuid4().hex}"
    storage = get_storage()
    size = img.size
    for width in WIDTHS:
        jpeg, dims = _jpeg(img, width)
        storage.put(f"{key}-{width}.jpg", jpeg, "image/jpeg")
        if width == WIDTHS[0]:
            size = dims
    return StoredImage(key, size[0], size[1])


def delete(key: str | None) -> None:
    if not key:
        return
    storage = get_storage()
    for width in WIDTHS:
        storage.delete(f"{key}-{width}.jpg")


def urls(key: str | None) -> dict | None:
    from app.storage import public_url

    if not key:
        return None
    return {"large": public_url(f"{key}-1200.jpg"), "small": public_url(f"{key}-400.jpg")}

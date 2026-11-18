import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

import pillow_heif
from PIL import Image

THUMB_EDGE = 480
PREVIEW_EDGE = 2048
THUMB_QUALITY = 80
PREVIEW_QUALITY = 86
EXIF_IFD_POINTER = 0x8769
EXIF_DATE_TIME_ORIGINAL = 36867
EXIF_DATE_TIME_DIGITIZED = 36868
IFD0_DATE_TIME = 306
EXIF_ORIENTATION = 0x0112
ROTATED_ORIENTATIONS = (5, 6, 7, 8)
UPRIGHT_TURNS = {
    2: Image.Transpose.FLIP_LEFT_RIGHT,
    3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE,
    6: Image.Transpose.ROTATE_270,
    7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_90,
}
EXIF_DATE_PATTERN = re.compile(r"(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2}):(\d{2})")
EARLIEST_YEAR = 1826
LATEST_YEAR = 2999
BACKGROUND = (0, 0, 0)

pillow_heif.register_heif_opener()
log = logging.getLogger("photos.imaging")


class ImageTooLarge(Exception):
    pass


@dataclass(frozen=True)
class ImageFacts:
    width: int | None
    height: int | None
    taken_at: str | None
    has_preview: bool


def _wall_clock(raw) -> str | None:
    if not isinstance(raw, str):
        return None
    match = EXIF_DATE_PATTERN.match(raw.strip())
    if not match:
        return None
    year, month, day, hour, minute, second = (int(part) for part in match.groups())
    if not (EARLIEST_YEAR <= year <= LATEST_YEAR and 1 <= month <= 12 and 1 <= day <= 31
            and hour <= 23 and minute <= 59 and second <= 59):
        return None
    return f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:{second:02d}"


def taken_at_from_exif(image: Image.Image) -> str | None:
    try:
        exif = image.getexif()
        detail = exif.get_ifd(EXIF_IFD_POINTER)
    except Exception as error:
        log.info("unreadable EXIF dates: %s", error)
        return None
    for raw in (detail.get(EXIF_DATE_TIME_ORIGINAL), detail.get(EXIF_DATE_TIME_DIGITIZED), exif.get(IFD0_DATE_TIME)):
        found = _wall_clock(raw)
        if found:
            return found
    return None


def _flatten(image: Image.Image) -> Image.Image:
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        flat = Image.new("RGB", rgba.size, BACKGROUND)
        flat.paste(rgba, mask=rgba.getchannel("A"))
        return flat
    return image.convert("RGB")


def _save_jpeg(image: Image.Image, target: Path, edge: int, quality: int) -> None:
    copy = image.copy()
    copy.thumbnail((edge, edge), Image.Resampling.LANCZOS)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    partial = target.with_name(f".{target.name}.partial")
    copy.save(partial, "JPEG", quality=quality, optimize=True, progressive=True)
    os.replace(partial, target)


def write_renditions(image: Image.Image, thumb: Path, preview: Path) -> None:
    _save_jpeg(image, preview, PREVIEW_EDGE, PREVIEW_QUALITY)
    _save_jpeg(image, thumb, THUMB_EDGE, THUMB_QUALITY)


def _remove_renditions(*targets: Path) -> None:
    for target in targets:
        target.unlink(missing_ok=True)
        target.with_name(f".{target.name}.partial").unlink(missing_ok=True)


def _refuse_oversized(image: Image.Image, max_pixels: int) -> None:
    width, height = image.size
    if width * height > max_pixels:
        raise ImageTooLarge(f"the image is {width} x {height} pixels, more than the {max_pixels // 1_000_000} "
                            "megapixels this library accepts")


def _orientation(image: Image.Image) -> int | None:
    try:
        return image.getexif().get(EXIF_ORIENTATION)
    except Exception as error:
        log.info("unreadable EXIF orientation: %s", error)
        return None


def _upright_size(size: tuple[int, int], orientation: int | None) -> tuple[int, int]:
    width, height = size
    if orientation in ROTATED_ORIENTATIONS:
        return height, width
    return width, height


def _shrink_in_place(image: Image.Image) -> None:
    image.draft("RGB", (PREVIEW_EDGE, PREVIEW_EDGE))
    image.thumbnail((PREVIEW_EDGE, PREVIEW_EDGE), Image.Resampling.LANCZOS)


def _turned_upright(image: Image.Image, orientation: int | None) -> Image.Image:
    turn = UPRIGHT_TURNS.get(orientation)
    return image if turn is None else image.transpose(turn)


def _render(source: Path, thumb: Path, preview: Path, max_pixels: int) -> ImageFacts:
    with Image.open(source) as image:
        _refuse_oversized(image, max_pixels)
        taken_at = taken_at_from_exif(image)
        orientation = _orientation(image)
        width, height = _upright_size(image.size, orientation)
        _shrink_in_place(image)
        write_renditions(_flatten(_turned_upright(image, orientation)), thumb, preview)
        return ImageFacts(width, height, taken_at, True)


def process_image(source: Path, thumb: Path, preview: Path, max_pixels: int) -> ImageFacts:
    try:
        return _render(source, thumb, preview, max_pixels)
    except ImageTooLarge:
        raise
    except Image.DecompressionBombError as error:
        raise ImageTooLarge(f"the image has too many pixels to open safely ({error})") from error
    except Exception as error:
        _remove_renditions(thumb, preview)
        log.warning("no preview for %s: %s: %s", source.name, type(error).__name__, error)
        return ImageFacts(None, None, None, False)


def thumbnail_from_poster(poster: Path, thumb: Path) -> None:
    with Image.open(poster) as image:
        _save_jpeg(_flatten(image), thumb, THUMB_EDGE, THUMB_QUALITY)

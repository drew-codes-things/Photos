import io

import pytest
from PIL import Image

from conftest import jpeg_bytes, png_bytes
from photos import sniff


def ftyp(major: bytes, *compatible: bytes) -> bytes:
    body = b"ftyp" + major + b"\x00\x00\x00\x00" + b"".join(compatible)
    return (len(body) + 4).to_bytes(4, "big") + body + b"\x00" * 32


def image_bytes(file_format: str) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8)).save(buffer, file_format)
    return buffer.getvalue()


@pytest.mark.parametrize("data, expected", [
    (jpeg_bytes(), sniff.JPEG),
    (png_bytes(), sniff.PNG),
    (image_bytes("GIF"), sniff.GIF),
    (image_bytes("WEBP"), sniff.WEBP),
    (ftyp(b"heic", b"mif1", b"heic"), sniff.HEIC),
    (ftyp(b"mif1", b"mif1", b"heic"), sniff.HEIC),
    (ftyp(b"mif1", b"mif1", b"avif"), sniff.AVIF),
    (ftyp(b"avif", b"mif1"), sniff.AVIF),
    (ftyp(b"qt  ", b"qt  "), sniff.MOV),
    (ftyp(b"isom", b"isom", b"iso2", b"avc1", b"mp41"), sniff.MP4),
    (ftyp(b"mp42", b"mp42", b"isom"), sniff.MP4),
    (ftyp(b"3gp5", b"3gp5"), sniff.THREE_GP),
    (b"\x1a\x45\xdf\xa3\x9f\x42\x86\x81\x01\x42\x82\x84webm" + b"\x00" * 40, sniff.WEBM),
    (b"\x1a\x45\xdf\xa3\x9f\x42\x86\x81\x01\x42\x82\x88matroska" + b"\x00" * 40, sniff.MATROSKA),
])
def test_known_photo_and_video_types_are_recognised_by_content(data, expected):
    assert sniff.detect(data[:sniff.HEADER_BYTES]) == expected


@pytest.mark.parametrize("data", [
    b"<!doctype html><script>alert(1)</script>",
    b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>',
    b"<?xml version='1.0'?><svg/>",
    b"PK\x03\x04" + b"\x00" * 60,
    b"MZ\x90\x00" + b"\x00" * 60,
    b"%PDF-1.7\n",
    ftyp(b"zzzz", b"yyyy"),
    b"",
])
def test_anything_else_is_refused(data):
    assert sniff.detect(data[:sniff.HEADER_BYTES]) is None


def test_a_jpeg_named_like_a_page_is_still_a_jpeg():
    assert sniff.detect(jpeg_bytes()[:sniff.HEADER_BYTES]).extension == "jpg"

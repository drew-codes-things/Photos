import io
import struct
import zlib
from pathlib import Path

import pytest
from PIL import Image

from conftest import jpeg_bytes, png_bytes
from photos.imaging import ImageTooLarge, process_image

GENEROUS_PIXELS = 10 ** 12


def run(tmp_path, data: bytes, max_pixels=50_000_000):
    source = tmp_path / "source.bin"
    source.write_bytes(data)
    thumb, preview = tmp_path / "out" / "thumb.jpg", tmp_path / "out" / "preview.jpg"
    return process_image(source, thumb, preview, max_pixels), thumb, preview


def png_header_claiming(width: int, height: int) -> bytes:
    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(b"\x00" * 64)) + chunk(b"IEND", b"")


def test_the_taken_date_comes_from_exif_and_both_renditions_are_jpegs(tmp_path):
    facts, thumb, preview = run(tmp_path, jpeg_bytes(size=(3000, 2000), taken="2021:07:04 18:30:05"))
    assert facts.taken_at == "2021-07-04T18:30:05"
    assert (facts.width, facts.height, facts.has_preview) == (3000, 2000, True)
    with Image.open(thumb) as small, Image.open(preview) as large:
        assert small.format == large.format == "JPEG"
        assert max(small.size) == 480
        assert max(large.size) == 2048


def test_a_photo_without_exif_has_no_taken_date(tmp_path):
    facts, _, _ = run(tmp_path, jpeg_bytes())
    assert facts.taken_at is None
    assert facts.has_preview


@pytest.mark.parametrize("raw", ["0000:00:00 00:00:00", "    :  :     :  :  ", "2021:13:40 25:61:61", "garbage"])
def test_blank_or_impossible_exif_dates_are_ignored(tmp_path, raw):
    facts, _, _ = run(tmp_path, jpeg_bytes(taken=raw))
    assert facts.taken_at is None


def test_a_sideways_phone_photo_is_turned_upright(tmp_path):
    facts, thumb, _ = run(tmp_path, jpeg_bytes(size=(80, 40), orientation=6))
    assert (facts.width, facts.height) == (40, 80)
    with Image.open(thumb) as small:
        assert small.width < small.height


def test_a_see_through_png_is_flattened(tmp_path):
    facts, thumb, _ = run(tmp_path, png_bytes())
    assert facts.has_preview
    with Image.open(thumb) as small:
        assert small.mode == "RGB"


def test_a_heic_from_an_iphone_gets_a_preview(tmp_path):
    buffer = io.BytesIO()
    Image.new("RGB", (120, 90), (0, 0, 200)).save(buffer, "HEIF")
    facts, thumb, _ = run(tmp_path, buffer.getvalue())
    assert facts.has_preview
    assert (facts.width, facts.height) == (120, 90)
    assert thumb.is_file()


def test_a_broken_image_is_kept_without_a_preview(tmp_path):
    facts, thumb, preview = run(tmp_path, b"\xff\xd8\xff\xe0" + b"\x00" * 200)
    assert not facts.has_preview
    assert not thumb.exists() and not preview.exists()


def test_more_pixels_than_the_setting_allows_is_refused(tmp_path):
    with pytest.raises(ImageTooLarge):
        run(tmp_path, jpeg_bytes(size=(100, 100)), max_pixels=9_999)


def test_an_image_bomb_header_is_refused_before_decoding(tmp_path):
    bomb = png_header_claiming(100_000, 100_000)
    with pytest.raises(ImageTooLarge):
        run(tmp_path, bomb)
    with pytest.raises(ImageTooLarge):
        run(tmp_path, bomb, max_pixels=GENEROUS_PIXELS)


DAMAGED = sorted((Path(__file__).parent / "data" / "damaged").glob("*.bin"))
SMALL_ENOUGH_BYTES_PER_PIXEL = 12


def leftovers(folder):
    return sorted(path.name for path in folder.rglob("*") if path.is_file())


def test_big_images_are_shrunk_before_they_are_copied(tmp_path):
    from memory_probe import bytes_per_pixel
    assert bytes_per_pixel(tmp_path, "PNG") < SMALL_ENOUGH_BYTES_PER_PIXEL


@pytest.mark.parametrize("sample", DAMAGED, ids=[path.name for path in DAMAGED])
def test_damaged_images_never_escape_and_leave_no_half_written_files(tmp_path, sample):
    facts, thumb, preview = run(tmp_path, sample.read_bytes())
    if facts.has_preview:
        assert thumb.is_file() and preview.is_file()
    else:
        assert not thumb.exists() and not preview.exists()
    assert not list(tmp_path.rglob("*.partial"))


def test_a_failure_while_writing_the_thumbnail_removes_the_preview_too(tmp_path, monkeypatch):
    from photos import imaging
    real_save = imaging._save_jpeg

    def fail_on_thumb(image, target, edge, quality):
        if target.name == "thumb.jpg":
            raise OSError("disk went away")
        return real_save(image, target, edge, quality)

    monkeypatch.setattr(imaging, "_save_jpeg", fail_on_thumb)
    facts, _, _ = run(tmp_path, jpeg_bytes())
    assert not facts.has_preview
    assert leftovers(tmp_path / "out") == []


def test_a_sideways_phone_photo_stays_upright_after_shrinking(tmp_path):
    facts, _, preview = run(tmp_path, jpeg_bytes(size=(4000, 2000), orientation=6))
    assert (facts.width, facts.height) == (2000, 4000)
    with Image.open(preview) as large:
        assert large.size == (1024, 2048)


def test_a_palette_gif_gets_a_colour_preview(tmp_path):
    buffer = io.BytesIO()
    Image.new("P", (3000, 2000), 3).save(buffer, "GIF")
    facts, thumb, _ = run(tmp_path, buffer.getvalue())
    assert facts.has_preview
    with Image.open(thumb) as small:
        assert small.mode == "RGB" and max(small.size) == 480

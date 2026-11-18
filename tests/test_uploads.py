import io
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import GIGABYTE, MAX_FILE_BYTES, MIN_FREE_BYTES, jpeg_bytes, png_bytes
from photos.uploads import MAX_CHUNK_BYTES, STALE_UPLOAD_SECONDS

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")


def stored_originals(storage):
    return sorted(path.name for path in storage.originals.rglob("*") if path.is_file())


def start(owner, data=b"x" * 10, name="IMG.JPG", fingerprint="fp-1", file_time=None):
    return owner.post("/api/uploads", json={"name": name, "size": len(data), "fingerprint": fingerprint,
                                            "fileTime": file_time})


def test_a_photo_uploaded_in_chunks_lands_in_the_library_with_its_exif_date(owner, storage):
    data = jpeg_bytes(size=(900, 600), taken="2020:02:29 07:15:00")
    finished = owner.upload(data, name="IMG_0420.JPG", chunk=777)
    assert finished.status_code == 200
    body = finished.get_json()
    assert body["duplicate"] is False
    item = body["item"]
    assert item["takenAt"] == "2020-02-29T07:15:00"
    assert item["takenSource"] == "photo"
    assert item["name"] == "IMG_0420.JPG"
    assert (item["kind"], item["format"], item["size"]) == ("image", "jpeg", len(data))
    assert item["hasPreview"] is True
    assert len(stored_originals(storage)) == 1
    assert not list(storage.incoming.iterdir())


def test_without_exif_the_file_time_from_the_phone_is_used(owner):
    item = owner.upload(png_bytes(), name="screen.png", file_time="2019-12-24T20:00:01").get_json()["item"]
    assert (item["takenAt"], item["takenSource"]) == ("2019-12-24T20:00:01", "file")


def test_without_exif_or_file_time_the_upload_time_is_used(owner, clock):
    from datetime import datetime
    item = owner.upload(png_bytes(), name="screen.png").get_json()["item"]
    assert item["takenSource"] == "upload"
    assert item["takenAt"] == datetime.fromtimestamp(clock()).replace(microsecond=0).isoformat()


def test_a_dropped_connection_resumes_from_what_the_server_already_has(owner):
    data = jpeg_bytes(size=(300, 300))
    started = start(owner, data, fingerprint="phone|IMG_1.JPG|123").get_json()
    assert owner.put_chunk(started["id"], 0, data[:500]).status_code == 200
    again = start(owner, data, fingerprint="phone|IMG_1.JPG|123").get_json()
    assert again["id"] == started["id"]
    assert (again["resumed"], again["received"]) == (True, 500)
    assert owner.get(f"/api/uploads/{started['id']}").get_json()["received"] == 500
    owner.put_chunk(started["id"], 500, data[500:])
    assert owner.post(f"/api/uploads/{started['id']}/finish").status_code == 200


def test_a_chunk_sent_twice_after_a_lost_reply_is_refused_with_the_right_offset(owner):
    data = jpeg_bytes()
    upload_id = start(owner, data).get_json()["id"]
    owner.put_chunk(upload_id, 0, data[:100])
    repeat = owner.put_chunk(upload_id, 0, data[:100])
    assert repeat.status_code == 409
    assert repeat.get_json()["received"] == 100


def test_finishing_before_every_byte_arrived_is_refused(owner):
    data = jpeg_bytes()
    upload_id = start(owner, data).get_json()["id"]
    owner.put_chunk(upload_id, 0, data[:10])
    refused = owner.post(f"/api/uploads/{upload_id}/finish")
    assert refused.status_code == 409
    assert refused.get_json()["received"] == 10


def test_the_same_photo_uploaded_twice_is_stored_once(owner, storage, library):
    data = jpeg_bytes(taken="2022:01:01 10:00:00")
    first = owner.upload(data, name="a.jpg", fingerprint="one").get_json()
    second = owner.upload(data, name="copy of a.jpg", fingerprint="two").get_json()
    assert second["duplicate"] is True
    assert second["item"]["id"] == first["item"]["id"]
    assert library.totals()["count"] == 1
    assert len(stored_originals(storage)) == 1
    assert not list(storage.incoming.iterdir())


def test_uploading_a_photo_that_is_in_the_bin_brings_it_back(owner, library):
    data = jpeg_bytes()
    item_id = owner.upload(data, fingerprint="one").get_json()["item"]["id"]
    owner.post("/api/media/bulk", json={"action": "trash", "ids": [item_id]})
    again = owner.upload(data, fingerprint="two").get_json()
    assert (again["duplicate"], again["restored"]) == (True, True)
    assert library.get(item_id)["trashedAt"] is None


@pytest.mark.parametrize("data, name", [
    (b"<html><script>alert(1)</script></html>", "photo.jpg"),
    (b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"></svg>', "pic.svg"),
    (b"PK\x03\x04" + b"\x00" * 100, "../../evil.zip"),
])
def test_files_that_are_not_photos_or_videos_are_refused_and_removed(owner, storage, data, name):
    refused = owner.upload(data, name=name)
    assert refused.status_code == 415
    assert stored_originals(storage) == []
    assert not list(storage.incoming.iterdir())


def test_path_parts_in_a_file_name_are_dropped(owner, storage):
    item = owner.upload(jpeg_bytes(), name="../../etc/cron.d/IMG_1.JPG").get_json()["item"]
    assert item["name"] == "IMG_1.JPG"
    assert all(path.resolve().is_relative_to(storage.root.resolve()) for path in storage.root.rglob("*"))


def test_files_over_the_size_limit_are_refused_up_front(owner):
    refused = owner.post("/api/uploads", json={"name": "big.mov", "size": MAX_FILE_BYTES + 1, "fingerprint": "f"})
    assert refused.status_code == 413


@pytest.mark.parametrize("payload", [
    {"name": "a.jpg", "size": 0, "fingerprint": "f"},
    {"name": "a.jpg", "size": -5, "fingerprint": "f"},
    {"name": "a.jpg", "size": "10", "fingerprint": "f"},
    {"name": "a.jpg", "size": True, "fingerprint": "f"},
    {"name": 5, "size": 10, "fingerprint": "f"},
    {"name": "a.jpg", "size": 10},
    {"name": "a.jpg", "size": 10, "fingerprint": "x" * 601},
    {"name": "a.jpg", "size": 10, "fingerprint": "f", "fileTime": "yesterday"},
    {"name": "a.jpg", "size": 10, "fingerprint": "f", "fileTime": "2024-02-30T10:00:00"},
])
def test_bad_upload_requests_are_refused(owner, payload):
    assert owner.post("/api/uploads", json=payload).status_code == 400


def test_a_chunk_that_runs_past_the_end_is_refused(owner):
    upload_id = start(owner, b"x" * 10).get_json()["id"]
    assert owner.put_chunk(upload_id, 0, b"y" * 11).status_code == 400


def test_a_chunk_over_the_chunk_limit_is_refused(owner):
    upload_id = owner.post("/api/uploads", json={"name": "v.mp4", "size": MAX_CHUNK_BYTES * 2,
                                                 "fingerprint": "f"}).get_json()["id"]
    assert owner.put_chunk(upload_id, 0, b"y" * (MAX_CHUNK_BYTES + 1)).status_code == 413


@pytest.mark.parametrize("offset", ["", "-1", "abc", "1e3"])
def test_a_chunk_without_a_usable_offset_is_refused(owner, offset):
    upload_id = start(owner).get_json()["id"]
    headers = {**owner.write_headers, "Upload-Offset": offset}
    assert owner.client.put(f"/api/uploads/{upload_id}", data=b"x", headers=headers).status_code == 400


def test_a_chunk_without_a_length_is_refused(uploads, owner):
    from photos.errors import PhotoError
    upload_id = start(owner).get_json()["id"]
    with pytest.raises(PhotoError) as raised:
        uploads.append(upload_id, 0, None, io.BytesIO(b"x"))
    assert raised.value.status == 411


@pytest.mark.parametrize("upload_id", ["..%2f..%2fphotos.db", "zz", "0" * 31, "A" * 32])
def test_upload_ids_that_are_not_ours_reach_nothing(owner, upload_id):
    assert owner.get(f"/api/uploads/{upload_id}").status_code == 404


def test_uploads_stop_when_the_disk_would_drop_below_the_free_space_floor(owner, disk):
    disk.free = MIN_FREE_BYTES + 5
    refused = start(owner, b"x" * 10)
    assert refused.status_code == 507
    assert "not enough disk space" in refused.get_json()["error"]
    space = owner.get("/api/storage").get_json()
    assert space["uploadsAllowed"] is False
    assert space["minFreeBytes"] == MIN_FREE_BYTES


def test_the_floor_counts_uploads_already_under_way(owner, disk):
    disk.free = MIN_FREE_BYTES + 15
    assert start(owner, b"x" * 10, fingerprint="one").status_code == 201
    assert start(owner, b"x" * 10, fingerprint="two").status_code == 507


def test_a_chunk_is_refused_if_the_disk_filled_up_meanwhile(owner, disk):
    data = jpeg_bytes()
    upload_id = start(owner, data).get_json()["id"]
    disk.free = MIN_FREE_BYTES
    assert owner.put_chunk(upload_id, 0, data[:100]).status_code == 507


def test_storage_shows_used_and_free_space(owner, disk):
    owner.upload(jpeg_bytes())
    space = owner.get("/api/storage").get_json()
    assert space["freeBytes"] == disk.free
    assert space["totalBytes"] == disk.total
    assert space["roomBytes"] == disk.free - MIN_FREE_BYTES
    assert space["count"] == 1 and space["libraryBytes"] > 0
    assert space["uploadsAllowed"] is True
    assert disk.free > GIGABYTE


def test_an_upload_can_be_cancelled(owner, storage):
    upload_id = start(owner).get_json()["id"]
    assert owner.delete(f"/api/uploads/{upload_id}").status_code == 204
    assert owner.get(f"/api/uploads/{upload_id}").status_code == 404
    assert not list(storage.incoming.iterdir())


def test_uploads_left_unfinished_for_days_are_cleared(owner, uploads, clock, storage):
    upload_id = start(owner).get_json()["id"]
    clock.advance(STALE_UPLOAD_SECONDS + 1)
    assert uploads.purge_stale() >= 1
    assert owner.get(f"/api/uploads/{upload_id}").status_code == 404
    assert not list(storage.incoming.iterdir())


def test_a_huge_image_is_refused_and_not_kept(owner, storage, uploads):
    uploads.max_pixels = 1_000
    refused = owner.upload(jpeg_bytes(size=(100, 100)))
    assert refused.status_code == 413
    assert stored_originals(storage) == []


@needs_ffmpeg
def test_a_video_gets_a_poster_and_plays_with_range_requests(owner, tmp_path):
    clip = tmp_path / "clip.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=2:size=160x120:rate=10",
                    "-pix_fmt", "yuv420p", "-metadata", "creation_time=2023-03-04T10:11:12.000000Z", str(clip)],
                   check=True, capture_output=True, timeout=60)
    item = owner.upload(clip.read_bytes(), name="clip.mp4", chunk=4096).get_json()["item"]
    assert (item["kind"], item["mime"], item["hasPreview"]) == ("video", "video/mp4", True)
    assert item["takenSource"] == "video"
    assert owner.get(f"/media/{item['id']}/thumb").mimetype == "image/jpeg"
    partial = owner.get(f"/media/{item['id']}/play", headers={"Range": "bytes=0-99"})
    assert partial.status_code == 206
    assert len(partial.data) == 100
    assert partial.headers["Content-Type"] == "video/mp4"


def test_a_video_without_ffmpeg_shows_no_preview_but_still_plays(owner, uploads):
    from photos.video import VideoTools
    uploads.tools = VideoTools(ffprobe=None, ffmpeg=None)
    data = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 200
    item = owner.upload(data, name="old.mp4", file_time="2015-06-01T09:00:00").get_json()["item"]
    assert (item["kind"], item["hasPreview"], item["takenSource"]) == ("video", False, "file")
    assert owner.get(f"/media/{item['id']}/thumb").status_code == 404
    assert owner.get(f"/media/{item['id']}/play").status_code == 200


DAMAGED = sorted((Path(__file__).parent / "data" / "damaged").glob("*.bin"))


def test_finishing_twice_after_a_lost_reply_answers_the_same_result(owner):
    data = jpeg_bytes(color=(1, 2, 3))
    upload_id = start(owner, data).get_json()["id"]
    owner.put_chunk(upload_id, 0, data)
    first = owner.post(f"/api/uploads/{upload_id}/finish")
    second = owner.post(f"/api/uploads/{upload_id}/finish")
    assert first.status_code == second.status_code == 200
    assert second.get_json()["item"]["id"] == first.get_json()["item"]["id"]
    assert second.get_json()["duplicate"] is False


def test_a_finished_upload_holds_no_disk_room(owner, uploads):
    data = jpeg_bytes()
    owner.upload(data)
    assert uploads.outstanding_bytes() == 0


def test_an_upload_that_crashed_the_server_while_ingesting_is_refused_not_retried(owner, library, storage):
    data = jpeg_bytes()
    upload_id = start(owner, data).get_json()["id"]
    owner.put_chunk(upload_id, 0, data)
    with library.database.connect() as connection:
        connection.execute("UPDATE uploads SET state = 'ingesting' WHERE id = ?", (upload_id,))
    refused = owner.post(f"/api/uploads/{upload_id}/finish")
    assert refused.status_code == 422
    assert "stopped" in refused.get_json()["error"]
    assert owner.post(f"/api/uploads/{upload_id}/finish").status_code == 422
    assert not list(storage.incoming.iterdir())
    assert stored_originals(storage) == []


def test_the_same_fingerprint_started_four_times_at_once_gives_one_upload(owner, storage):
    import threading
    answers = []

    def start_one():
        answers.append(start(owner, b"x" * 5000, fingerprint="race"))

    threads = [threading.Thread(target=start_one) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(answer.status_code for answer in answers) == [201] * 4
    assert len({answer.get_json()["id"] for answer in answers}) == 1
    assert len(list(storage.incoming.iterdir())) == 1


def test_refused_chunks_for_unknown_ids_leave_no_locks_behind(uploads):
    from photos.errors import PhotoError
    for number in range(200):
        with pytest.raises(PhotoError):
            uploads.append(f"not-an-id-{number}", 0, 10, io.BytesIO(b"0123456789"))
        with pytest.raises(PhotoError):
            uploads.finish(f"{number:032x}")
    assert uploads.upload_locks == {}


def test_storage_shows_room_held_by_uploads_still_arriving(owner, disk):
    disk.free = MIN_FREE_BYTES + MAX_FILE_BYTES + MAX_FILE_BYTES // 2
    held = {"name": "big.mov", "size": MAX_FILE_BYTES, "fingerprint": "held"}
    assert owner.post("/api/uploads", json=held).status_code == 201
    space = owner.get("/api/storage").get_json()
    assert space["reservedBytes"] == MAX_FILE_BYTES
    assert space["roomBytes"] == MAX_FILE_BYTES // 2
    refused = owner.post("/api/uploads", json={"name": "next.mov", "size": MAX_FILE_BYTES, "fingerprint": "next"})
    assert refused.status_code == 507
    assert "held for uploads still arriving" in refused.get_json()["error"]


def test_an_upload_that_never_sent_a_byte_lets_go_of_its_room_after_an_hour(owner, uploads, clock, disk):
    disk.free = MIN_FREE_BYTES + 3 * MAX_FILE_BYTES
    owner.post("/api/uploads", json={"name": "big.mov", "size": MAX_FILE_BYTES, "fingerprint": "abandoned"})
    clock.advance(30 * 60)
    uploads.purge_stale()
    assert uploads.outstanding_bytes() == MAX_FILE_BYTES
    clock.advance(31 * 60)
    uploads.purge_stale()
    assert uploads.outstanding_bytes() == 0


def test_an_upload_with_bytes_keeps_its_room_for_days(owner, uploads, clock):
    data = jpeg_bytes()
    upload_id = start(owner, data).get_json()["id"]
    owner.put_chunk(upload_id, 0, data[:100])
    clock.advance(2 * 24 * 60 * 60)
    uploads.purge_stale()
    assert owner.get(f"/api/uploads/{upload_id}").status_code == 200


@pytest.mark.parametrize("offset", ["²", "¹", "1³"])
def test_offsets_written_with_non_ascii_digits_are_refused(owner, offset):
    upload_id = start(owner).get_json()["id"]
    headers = {**owner.write_headers, "Upload-Offset": offset}
    assert owner.client.put(f"/api/uploads/{upload_id}", data=b"x", headers=headers).status_code == 400


@pytest.mark.parametrize("sample", DAMAGED, ids=[path.name for path in DAMAGED])
def test_damaged_photos_are_kept_without_a_preview_instead_of_failing(owner, sample, storage):
    finished = owner.upload(sample.read_bytes(), name=sample.stem + ".img")
    assert finished.status_code == 200, finished.get_json()
    assert len(stored_originals(storage)) == 1

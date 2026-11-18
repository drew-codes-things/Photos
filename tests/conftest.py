import io
from collections import namedtuple

import pytest
from PIL import Image

START_TIME = 1_800_000_000.0
OWNER = "demo@example.com"
SECRET = b"k" * 32
GIGABYTE = 1024 ** 3
MIN_FREE_BYTES = 15 * GIGABYTE
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_PIXELS = 50_000_000
DiskUsage = namedtuple("DiskUsage", "total used free")


class FakeClock:
    def __init__(self, start: float = START_TIME):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeDisk:
    def __init__(self, free: int = 100 * GIGABYTE, total: int = 400 * GIGABYTE):
        self.free = free
        self.total = total

    def __call__(self, path) -> DiskUsage:
        return DiskUsage(self.total, self.total - self.free, self.free)


def jpeg_bytes(size=(64, 48), color=(200, 30, 30), taken=None, orientation=None) -> bytes:
    image = Image.new("RGB", size, color)
    exif = Image.Exif()
    if taken:
        exif.get_ifd(0x8769)[36867] = taken
    if orientation:
        exif[0x0112] = orientation
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", exif=exif.tobytes())
    return buffer.getvalue()


def png_bytes(size=(40, 30), mode="RGBA") -> bytes:
    buffer = io.BytesIO()
    Image.new(mode, size, (10, 200, 10, 128) if mode == "RGBA" else (10, 200, 10)).save(buffer, "PNG")
    return buffer.getvalue()


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def disk():
    return FakeDisk()


@pytest.fixture
def storage(tmp_path, disk):
    from photos.storage import Storage
    return Storage(tmp_path / "data", MIN_FREE_BYTES, disk_usage=disk)


@pytest.fixture
def library(tmp_path, storage, clock):
    from photos.db import Database
    from photos.library import Library
    return Library(Database(tmp_path / "data" / "photos.db"), storage, clock=clock)


@pytest.fixture
def video_tools():
    from photos.video import VideoTools
    return VideoTools.find()


@pytest.fixture
def uploads(library, clock, video_tools):
    from photos.uploads import Uploads
    return Uploads(library, MAX_FILE_BYTES, MAX_PIXELS, video_tools, clock=clock)


@pytest.fixture
def app(library, uploads):
    from photos.auth import ProxyAuth
    from photos.web import create_app
    auth = ProxyAuth(trusted_proxies=("127.0.0.1", "::1"), user_header="X-Forwarded-User")
    flask_app = create_app(library, uploads, auth, SECRET)
    flask_app.testing = True
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


class SignedInClient:
    def __init__(self, client):
        self.client = client
        self.headers = {"X-Forwarded-User": OWNER}
        session = client.get("/api/session", headers=self.headers).get_json()
        self.write_headers = {**self.headers, "X-CSRF-Token": session["csrfToken"]}

    def get(self, path, headers=None, **kwargs):
        return self.client.get(path, headers={**self.headers, **(headers or {})}, **kwargs)

    def post(self, path, json=None, **kwargs):
        return self.client.post(path, json=json, headers=self.write_headers, **kwargs)

    def patch(self, path, json=None, **kwargs):
        return self.client.patch(path, json=json, headers=self.write_headers, **kwargs)

    def delete(self, path, **kwargs):
        return self.client.delete(path, headers=self.write_headers, **kwargs)

    def put_chunk(self, upload_id, offset, data):
        headers = {**self.write_headers, "Upload-Offset": str(offset), "Content-Type": "application/octet-stream"}
        return self.client.put(f"/api/uploads/{upload_id}", data=data, headers=headers)

    def upload(self, data: bytes, name="IMG_0001.JPG", file_time=None, chunk=1000, fingerprint=None):
        started = self.post("/api/uploads", json={"name": name, "size": len(data), "fileTime": file_time,
                                                  "fingerprint": fingerprint or f"{name}|{len(data)}|{hash(data)}"})
        assert started.status_code == 201, started.get_json()
        upload_id = started.get_json()["id"]
        for offset in range(started.get_json()["received"], len(data), chunk):
            sent = self.put_chunk(upload_id, offset, data[offset:offset + chunk])
            assert sent.status_code == 200, sent.get_json()
        return self.post(f"/api/uploads/{upload_id}/finish")


@pytest.fixture
def owner(client):
    return SignedInClient(client)

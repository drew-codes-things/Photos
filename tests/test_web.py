import pytest

from conftest import OWNER, SECRET, jpeg_bytes


@pytest.fixture
def photo(owner):
    return owner.upload(jpeg_bytes(taken="2020:01:01 12:00:00"), name="Café au lait.JPG").get_json()["item"]


def test_the_page_is_named_starfire_photos(owner):
    page = owner.get("/").get_data(as_text=True)
    assert "<title>Starfire Photos</title>" in page
    assert 'aria-label="Starfire Photos home"' in page
    assert 'Starfire <span class="brand-accent">Photos</span>' in page


def test_health_answers_without_signing_in(client):
    assert client.get("/api/health").get_json() == {"ok": True}


@pytest.mark.parametrize("path", ["/api/media", "/api/storage", "/api/albums", "/media/1/original", "/"])
def test_everything_else_needs_the_proxy_user(client, path):
    assert client.get(path).status_code == 401


def test_the_user_header_counts_only_from_a_trusted_proxy(client):
    response = client.get("/api/media", headers={"X-Forwarded-User": OWNER}, environ_base={"REMOTE_ADDR": "10.0.0.9"})
    assert response.status_code == 401


def test_writes_need_the_csrf_token(owner):
    response = owner.client.post("/api/albums", json={"name": "x"}, headers=owner.headers)
    assert response.status_code == 403


def test_writes_from_another_site_are_refused(owner):
    headers = {**owner.write_headers, "Origin": "https://evil.example"}
    assert owner.client.post("/api/albums", json={"name": "x"}, headers=headers).status_code == 403


def test_chunks_from_another_site_are_refused(owner):
    upload_id = owner.post("/api/uploads", json={"name": "a.jpg", "size": 5, "fingerprint": "f"}).get_json()["id"]
    headers = {**owner.write_headers, "Origin": "https://evil.example", "Upload-Offset": "0"}
    assert owner.client.put(f"/api/uploads/{upload_id}", data=b"12345", headers=headers).status_code == 403


def test_pages_carry_safety_headers(owner):
    response = owner.get("/")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert "script-src 'self'" in response.headers["Content-Security-Policy"]


def test_the_original_downloads_as_an_attachment_with_its_own_name(owner, photo):
    response = owner.get(f"/media/{photo['id']}/original")
    assert response.status_code == 200
    disposition = response.headers["Content-Disposition"]
    assert disposition.startswith("attachment;")
    assert "filename*=UTF-8''Caf%C3%A9%20au%20lait.JPG" in disposition
    assert response.headers["Content-Type"] == "image/jpeg"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "sandbox" in response.headers["Content-Security-Policy"]
    assert response.data == jpeg_bytes(taken="2020:01:01 12:00:00")


def test_a_name_without_an_extension_gets_one_on_download(owner):
    item = owner.upload(jpeg_bytes(color=(9, 9, 9)), name="no extension").get_json()["item"]
    disposition = owner.get(f"/media/{item['id']}/original").headers["Content-Disposition"]
    assert "no%20extension.jpg" in disposition or "no extension.jpg" in disposition


def test_thumbnails_and_previews_are_our_own_jpegs_and_cache_well(owner, photo):
    for kind in ("thumb", "preview"):
        response = owner.get(f"/media/{photo['id']}/{kind}?v={photo['version']}")
        assert response.status_code == 200
        assert response.headers["Content-Type"] == "image/jpeg"
        assert response.headers["Content-Disposition"].startswith("inline;")
        assert "immutable" in response.headers["Cache-Control"]
        assert response.data[:3] == b"\xff\xd8\xff"


def test_play_is_only_for_videos(owner, photo):
    assert owner.get(f"/media/{photo['id']}/play").status_code == 404


@pytest.mark.parametrize("path", [
    "/media/999/original", "/media/999/thumb", "/media/abc/original", f"/media/{2 ** 63}/original",
    "/static/../photos.db", "/static/..%2f..%2fdata%2fphotos.db", "/static/%2e%2e/web.py", "/api/media/999",
])
def test_paths_outside_the_library_reach_nothing(owner, path):
    assert owner.get(path).status_code in (400, 404)


def test_a_missing_original_file_is_a_404_not_a_crash(owner, photo, storage):
    for path in storage.originals.rglob("*.jpg"):
        path.unlink()
    assert owner.get(f"/media/{photo['id']}/original").status_code == 404


def test_session_tells_the_page_how_to_upload(owner):
    session = owner.get("/api/session").get_json()
    assert session["chunkBytes"] == 4 * 1024 * 1024
    assert session["binDays"] == 30
    assert session["user"] == OWNER


def test_a_huge_request_is_refused(owner):
    upload_id = owner.post("/api/uploads", json={"name": "v.mp4", "size": 60 * 1024 * 1024,
                                                 "fingerprint": "f"}).get_json()["id"]
    response = owner.put_chunk(upload_id, 0, b"x" * (9 * 1024 * 1024))
    assert response.status_code == 413
    assert response.get_json()["error"]


def test_deep_json_is_refused_cleanly(owner):
    body = "[" * 100_000 + "]" * 100_000
    headers = {**owner.write_headers, "Content-Type": "application/json"}
    response = owner.client.post("/api/albums", data=body, headers=headers)
    assert response.status_code == 400


def test_items_carry_a_version_and_only_matching_versioned_urls_cache_forever(owner, photo):
    version = photo["version"]
    assert len(version) == 16
    versioned = owner.get(f"/media/{photo['id']}/thumb?v={version}")
    assert "immutable" in versioned.headers["Cache-Control"]
    for query in ("", "?v=0000000000000000", "?v=wrong"):
        response = owner.get(f"/media/{photo['id']}/thumb{query}")
        assert response.status_code == 200
        assert "immutable" not in response.headers["Cache-Control"]
        assert "no-cache" in response.headers["Cache-Control"]


@pytest.mark.parametrize("name, expected", [
    ("holiday.html", "holiday.html.jpg"),
    ("party.desktop", "party.desktop.jpg"),
    ("setup.exe", "setup.exe.jpg"),
    ("IMG_1.JPEG", "IMG_1.JPEG"),
    ("IMG_2.jpg", "IMG_2.jpg"),
    ("snow.svg", "snow.svg.jpg"),
])
def test_downloads_always_end_in_the_real_type(owner, name, expected):
    item = owner.upload(jpeg_bytes(color=(len(name), 1, 1)), name=name, fingerprint=name).get_json()["item"]
    disposition = owner.get(f"/media/{item['id']}/original").headers["Content-Disposition"]
    assert f"filename={expected}" in disposition or f"filename=\"{expected}\"" in disposition


def test_download_name_helper_keeps_matching_extensions():
    from photos.sniff import HEIC, MOV
    from photos.web import download_name
    assert download_name("IMG_1.HEIF", HEIC) == "IMG_1.HEIF"
    assert download_name("clip.qt", MOV) == "clip.qt"
    assert download_name("clip.mp4", MOV) == "clip.mp4.mov"
    assert download_name("noext", HEIC) == "noext.heic"


@pytest.fixture
def guarded_client(library, uploads):
    from photos.auth import ProxyAuth
    from photos.web import create_app
    auth = ProxyAuth(("127.0.0.1",), "X-Forwarded-User", ("admin",), allowed_hosts=("photos.example.com",))
    flask_app = create_app(library, uploads, auth, SECRET)
    flask_app.testing = True
    return flask_app.test_client()


def test_proxy_mode_refuses_a_host_name_it_was_not_given(guarded_client):
    user = {"X-Forwarded-User": "admin"}
    assert guarded_client.get("/api/session", headers={**user, "Host": "attacker.example:18099"}).status_code == 421
    assert guarded_client.get("/api/session", headers={**user, "Host": "photos.example.com"}).status_code == 200
    assert guarded_client.get("/api/session", headers={**user, "Host": "PHOTOS.example.com:80"}).status_code == 200


def test_health_answers_on_any_host_for_container_checks(guarded_client):
    assert guarded_client.get("/api/health", headers={"Host": "127.0.0.1:8080"}).status_code == 200

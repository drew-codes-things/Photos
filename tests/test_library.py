import pytest

from conftest import jpeg_bytes
from photos.library import BIN_SECONDS


def add(owner, taken, name="IMG.JPG", color=(1, 2, 3)):
    data = jpeg_bytes(color=color, taken=taken)
    return owner.upload(data, name=name, fingerprint=f"{name}|{taken}|{color}").get_json()["item"]["id"]


@pytest.fixture
def three(owner):
    return {
        "old": add(owner, "2019:05:01 09:00:00", "beach.jpg", (10, 0, 0)),
        "mid": add(owner, "2021:12:25 08:00:00", "christmas_tree.jpg", (20, 0, 0)),
        "new": add(owner, "2023:08:15 17:45:00", "100%_holiday.jpg", (30, 0, 0)),
    }


def ids(response):
    return [item["id"] for item in response.get_json()["items"]]


def test_the_timeline_is_newest_taken_first(owner, three):
    assert ids(owner.get("/api/media")) == [three["new"], three["mid"], three["old"]]


def test_the_timeline_pages_with_a_cursor(owner, three):
    first = owner.get("/api/media?limit=2").get_json()
    assert [item["id"] for item in first["items"]] == [three["new"], three["mid"]]
    assert first["total"] == 3
    rest = owner.get(f"/api/media?limit=2&cursor={first['nextCursor']}").get_json()
    assert [item["id"] for item in rest["items"]] == [three["old"]]
    assert rest["nextCursor"] is None


@pytest.mark.parametrize("cursor", ["nope", "2020-01-01T00:00:00", "2020-01-01T00:00:00~x", "'; DROP TABLE media;--"])
def test_a_made_up_cursor_is_refused(owner, three, cursor):
    assert owner.get("/api/media", query_string={"cursor": cursor}).status_code == 400


def test_search_by_file_name_ignores_case(owner, three):
    assert ids(owner.get("/api/media?q=CHRISTMAS")) == [three["mid"]]


def test_search_treats_like_wildcards_as_plain_text(owner, three):
    assert ids(owner.get("/api/media", query_string={"q": "%"})) == [three["new"]]
    assert ids(owner.get("/api/media", query_string={"q": "h_l"})) == []
    assert ids(owner.get("/api/media", query_string={"q": "s_tree"})) == [three["mid"]]


def test_search_by_date_range_includes_both_end_days(owner, three):
    assert ids(owner.get("/api/media?from=2021-12-25&to=2023-08-15")) == [three["new"], three["mid"]]
    assert ids(owner.get("/api/media?to=2019-05-01")) == [three["old"]]
    assert ids(owner.get("/api/media?from=2024-01-01")) == []


@pytest.mark.parametrize("query", ["from=2021-02-30", "from=21-1-1", "to=tomorrow", "from=2023-01-01&to=2022-01-01",
                                   "view=everything", "view=album", "q=" + "x" * 201])
def test_bad_searches_are_refused(owner, three, query):
    assert owner.get(f"/api/media?{query}").status_code == 400


def test_favourites(owner, three):
    owner.post("/api/media/bulk", json={"action": "favourite", "ids": [three["old"], three["new"]]})
    assert ids(owner.get("/api/media?view=favourites")) == [three["new"], three["old"]]
    owner.post("/api/media/bulk", json={"action": "unfavourite", "ids": [three["new"]]})
    assert ids(owner.get("/api/media?view=favourites")) == [three["old"]]


def test_the_bin_hides_items_and_restore_brings_them_back(owner, three):
    owner.post("/api/media/bulk", json={"action": "trash", "ids": [three["mid"]]})
    assert ids(owner.get("/api/media")) == [three["new"], three["old"]]
    binned = owner.get("/api/media?view=bin").get_json()["items"]
    assert [item["id"] for item in binned] == [three["mid"]]
    assert binned[0]["purgeAt"] == binned[0]["trashedAt"] + BIN_SECONDS
    owner.post("/api/media/bulk", json={"action": "restore", "ids": [three["mid"]]})
    assert ids(owner.get("/api/media?view=bin")) == []


def test_delete_forever_only_touches_items_in_the_bin(owner, three, storage):
    refused = owner.post("/api/media/bulk", json={"action": "delete", "ids": [three["old"]]}).get_json()
    assert refused["changed"] == 0
    owner.post("/api/media/bulk", json={"action": "trash", "ids": [three["old"]]})
    deleted = owner.post("/api/media/bulk", json={"action": "delete", "ids": [three["old"]]}).get_json()
    assert deleted["changed"] == 1
    assert owner.get(f"/api/media/{three['old']}").status_code == 404
    assert len([path for path in storage.originals.rglob("*") if path.is_file()]) == 2
    assert len([path for path in storage.renditions.rglob("*") if path.is_file()]) == 4


def test_the_bin_empties_itself_after_thirty_days(owner, three, library, clock, storage):
    owner.post("/api/media/bulk", json={"action": "trash", "ids": [three["old"]]})
    clock.advance(BIN_SECONDS - 60)
    assert library.purge_expired() == 0
    clock.advance(120)
    assert library.purge_expired() == 1
    assert owner.get(f"/api/media/{three['old']}").status_code == 404
    assert len([path for path in storage.originals.rglob("*") if path.is_file()]) == 2


def test_listing_runs_the_bin_clean_up(owner, three, clock):
    owner.post("/api/media/bulk", json={"action": "trash", "ids": [three["old"]]})
    clock.advance(BIN_SECONDS + 1)
    assert ids(owner.get("/api/media?view=bin")) == []


def test_empty_bin_now(owner, three):
    owner.post("/api/media/bulk", json={"action": "trash", "ids": [three["old"], three["mid"]]})
    assert owner.post("/api/bin/empty").get_json() == {"removed": 2}
    assert owner.get("/api/storage").get_json()["count"] == 1


@pytest.mark.parametrize("payload", [
    {"action": "explode", "ids": [1]},
    {"action": "trash", "ids": []},
    {"action": "trash", "ids": "1"},
    {"action": "trash", "ids": [0]},
    {"action": "trash", "ids": [True]},
    {"action": "trash", "ids": [2 ** 63]},
    {"action": "trash", "ids": list(range(1, 1002))},
])
def test_bad_bulk_requests_are_refused(owner, payload):
    assert owner.post("/api/media/bulk", json=payload).status_code == 400


def test_albums_hold_photos_and_show_a_cover_and_count(owner, three):
    album = owner.post("/api/albums", json={"name": "  Summer  "}).get_json()
    assert album["name"] == "Summer" and album["count"] == 0
    added = owner.post(f"/api/albums/{album['id']}/add", json={"ids": [three["old"], three["new"], 99999]}).get_json()
    assert added["added"] == 2
    assert added["album"]["count"] == 2
    assert added["album"]["coverId"] == three["new"]
    assert ids(owner.get(f"/api/media?view=album&album={album['id']}")) == [three["new"], three["old"]]
    again = owner.post(f"/api/albums/{album['id']}/add", json={"ids": [three["old"]]}).get_json()
    assert again["added"] == 0
    removed = owner.post(f"/api/albums/{album['id']}/remove", json={"ids": [three["new"]]}).get_json()
    assert removed["removed"] == 1 and removed["album"]["coverId"] == three["old"]
    item = owner.get(f"/api/media/{three['old']}").get_json()
    assert item["albums"] == [{"id": album["id"], "name": "Summer"}]


def test_binned_photos_drop_out_of_albums_until_restored(owner, three):
    album_id = owner.post("/api/albums", json={"name": "Trip"}).get_json()["id"]
    owner.post(f"/api/albums/{album_id}/add", json={"ids": [three["old"]]})
    owner.post("/api/media/bulk", json={"action": "trash", "ids": [three["old"]]})
    assert owner.get(f"/api/albums/{album_id}").get_json()["count"] == 0
    owner.post("/api/media/bulk", json={"action": "restore", "ids": [three["old"]]})
    assert owner.get(f"/api/albums/{album_id}").get_json()["count"] == 1


def test_renaming_and_deleting_an_album_keeps_its_photos(owner, three):
    album_id = owner.post("/api/albums", json={"name": "Trip"}).get_json()["id"]
    owner.post(f"/api/albums/{album_id}/add", json={"ids": [three["old"]]})
    assert owner.patch(f"/api/albums/{album_id}", json={"name": "Trip 2019"}).get_json()["name"] == "Trip 2019"
    assert owner.delete(f"/api/albums/{album_id}").status_code == 204
    assert owner.get(f"/api/albums/{album_id}").status_code == 404
    assert owner.get(f"/api/media/{three['old']}").status_code == 200


def test_albums_are_listed_by_name(owner):
    for name in ("zoo", "Apples", "middle"):
        owner.post("/api/albums", json={"name": name})
    assert [album["name"] for album in owner.get("/api/albums").get_json()["albums"]] == ["Apples", "middle", "zoo"]


@pytest.mark.parametrize("name", ["", "   ", "\x00\x01", 5, None, "x" * 121])
def test_bad_album_names_are_refused(owner, name):
    assert owner.post("/api/albums", json={"name": name}).status_code == 400


def test_missing_albums_are_404(owner):
    assert owner.post("/api/albums/42/add", json={"ids": [1]}).status_code == 404
    assert owner.get("/api/media?view=album&album=42").status_code == 404
    assert owner.get("/api/media?view=album&album=abc").status_code == 404
    assert owner.patch("/api/albums/42", json={"name": "x"}).status_code == 404
    assert owner.delete("/api/albums/42").status_code == 404


OLD_MEDIA_TABLE = """
CREATE TABLE media (
    id INTEGER PRIMARY KEY,
    sha256 TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('image', 'video')),
    format TEXT NOT NULL,
    mime TEXT NOT NULL,
    extension TEXT NOT NULL,
    original_name TEXT NOT NULL,
    size INTEGER NOT NULL,
    width INTEGER,
    height INTEGER,
    duration REAL,
    taken_at TEXT NOT NULL,
    taken_source TEXT NOT NULL CHECK (taken_source IN ('photo', 'video', 'file', 'upload')),
    has_preview INTEGER NOT NULL DEFAULT 0,
    favourite INTEGER NOT NULL DEFAULT 0,
    trashed_at REAL,
    uploaded_at REAL NOT NULL
);
CREATE TABLE albums (id INTEGER PRIMARY KEY, name TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE album_items (
    album_id INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
    media_id INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
    added_at REAL NOT NULL,
    PRIMARY KEY (album_id, media_id)
);
CREATE TABLE uploads (
    id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE, name TEXT NOT NULL, size INTEGER NOT NULL,
    received INTEGER NOT NULL DEFAULT 0, file_time TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
);
INSERT INTO media (id, sha256, kind, format, mime, extension, original_name, size, taken_at, taken_source, uploaded_at)
VALUES (7, '{sha}', 'image', 'jpeg', 'image/jpeg', 'jpg', 'old.jpg', 10, '2020-01-01T00:00:00', 'photo', 1);
INSERT INTO albums (id, name, created_at, updated_at) VALUES (1, 'Old album', 1, 1);
INSERT INTO album_items (album_id, media_id, added_at) VALUES (1, 7, 1);
INSERT INTO uploads (id, fingerprint, name, size, created_at, updated_at) VALUES ('{upload}', 'fp', 'x.jpg', 5, 1, 1);
"""


def test_ids_are_never_handed_out_again_after_delete_forever(owner):
    first = add(owner, "2020:01:01 00:00:00", "red.jpg", (255, 0, 0))
    owner.post("/api/media/bulk", json={"action": "trash", "ids": [first]})
    owner.post("/api/media/bulk", json={"action": "delete", "ids": [first]})
    second = add(owner, "2020:01:01 00:00:00", "blue.jpg", (0, 0, 255))
    assert second != first


def test_an_older_database_is_moved_to_ids_that_are_never_reused(tmp_path):
    import sqlite3
    from photos.db import Database
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(OLD_MEDIA_TABLE.replace("{sha}", "a" * 64).replace("{upload}", "b" * 32))
    database = Database(path)
    with database.connect() as connection:
        table_sql = connection.execute("SELECT sql FROM sqlite_master WHERE name = 'media'").fetchone()[0]
        assert "AUTOINCREMENT" in table_sql
        assert connection.execute("SELECT original_name FROM media WHERE id = 7").fetchone()[0] == "old.jpg"
        assert connection.execute("SELECT media_id FROM album_items").fetchone()[0] == 7
        assert connection.execute("SELECT state FROM uploads").fetchone()[0] == "receiving"
        connection.execute("DELETE FROM media WHERE id = 7")
        new_id = connection.execute(
            "INSERT INTO media (sha256, kind, format, mime, extension, original_name, size, taken_at, taken_source, "
            "uploaded_at) VALUES (?, 'image', 'jpeg', 'image/jpeg', 'jpg', 'n.jpg', 1, '2020-01-01T00:00:00', "
            "'photo', 1)", ("c" * 64,)).lastrowid
        assert new_id > 7
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    Database(path)


def test_emptying_the_bin_while_the_same_photo_comes_back_keeps_the_new_copy(owner, library, uploads, storage):
    import threading
    data = jpeg_bytes(color=(200, 10, 10))
    first = owner.upload(data, fingerprint="first").get_json()["item"]["id"]
    owner.post("/api/media/bulk", json={"action": "trash", "ids": [first]})
    removing, finished = threading.Event(), threading.Event()
    real_remove = storage.remove_stored

    def slow_remove(sha256, extension):
        removing.set()
        finished.wait(0.5)
        real_remove(sha256, extension)

    storage.remove_stored = slow_remove
    emptier = threading.Thread(target=library.empty_bin)
    emptier.start()
    removing.wait(5)
    again = owner.upload(data, fingerprint="second").get_json()["item"]
    finished.set()
    emptier.join()
    row, path = library.original_file(again["id"])
    assert path.is_file()
    assert storage.thumb_path(row["sha256"]).is_file()


def test_a_restore_while_the_bin_is_being_purged_keeps_the_item(owner, library, clock):
    import contextlib
    item = add(owner, "2020:01:01 00:00:00", "restored.jpg", (10, 200, 10))
    owner.post("/api/media/bulk", json={"action": "trash", "ids": [item]})
    clock.advance(BIN_SECONDS + 1)
    real_connect = library.database.connect
    armed = {"on": True}

    class RestoresAfterTheSelect:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, params=()):
            cursor = self.connection.execute(sql, params)
            if armed["on"] and sql.startswith("SELECT id, sha256, extension FROM media"):
                armed["on"] = False
                rows = cursor.fetchall()
                with real_connect() as other:
                    other.execute("UPDATE media SET trashed_at = NULL WHERE id = ?", (item,))
                return _Rows(rows)
            return cursor

        def executemany(self, sql, params):
            return self.connection.executemany(sql, params)

    class _Rows:
        def __init__(self, rows):
            self.rows = rows

        def fetchall(self):
            return self.rows

    @contextlib.contextmanager
    def racing_connect():
        with real_connect() as connection:
            yield RestoresAfterTheSelect(connection)

    library.database.connect = racing_connect
    try:
        library.purge_expired()
    finally:
        library.database.connect = real_connect
    assert owner.get(f"/api/media/{item}").status_code == 200
    _, path = library.original_file(item)
    assert path.is_file()


@pytest.mark.parametrize("query", ["album=%C2%B2&view=album", "cursor=2024-01-01T00:00:00~9999999999999999999",
                                   "cursor=2024-01-01T00:00:00~%C2%B2", "from=%D9%A2%D9%A0%D9%A2%D9%A4-01-01"])
def test_odd_digits_and_huge_numbers_in_queries_are_refused_not_crashing(owner, query):
    assert owner.get(f"/api/media?{query}").status_code in (400, 404)

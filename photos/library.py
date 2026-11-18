import logging
import re
import sqlite3
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from .db import Database
from .errors import PhotoError
from .storage import Storage

BIN_DAYS = 30
SECONDS_PER_DAY = 24 * 60 * 60
BIN_SECONDS = BIN_DAYS * SECONDS_PER_DAY
VIEWS = ("timeline", "favourites", "bin", "album")
DEFAULT_PAGE_SIZE = 120
LARGEST_PAGE_SIZE = 500
LARGEST_BULK = 1000
LARGEST_DATABASE_INTEGER = 2 ** 63 - 1
ALBUM_NAME_LIMIT = 120
SEARCH_LIMIT = 200
CURSOR_PATTERN = re.compile(r"([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})~([0-9]{1,19})")
DATE_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
VERSION_LENGTH = 16
DAY_START = "T00:00:00"
BULK_ACTIONS = ("favourite", "unfavourite", "trash", "restore", "delete")

log = logging.getLogger("photos.library")


@dataclass(frozen=True)
class NewMedia:
    sha256: str
    kind: str
    format: str
    mime: str
    extension: str
    original_name: str
    size: int
    width: int | None
    height: int | None
    duration: float | None
    taken_at: str
    taken_source: str
    has_preview: bool


def printable_text(raw, field: str, limit: int) -> str:
    if not isinstance(raw, str):
        raise PhotoError(f"{field} must be text")
    cleaned = "".join(character for character in unicodedata.normalize("NFC", raw) if character.isprintable())
    cleaned = cleaned.strip()
    if not cleaned:
        raise PhotoError(f"{field} cannot be empty")
    if len(cleaned) > limit:
        raise PhotoError(f"{field} can be at most {limit} characters")
    return cleaned


def id_list(raw) -> list[int]:
    if not isinstance(raw, list) or not raw:
        raise PhotoError("ids must be a list of item numbers")
    if len(raw) > LARGEST_BULK:
        raise PhotoError(f"at most {LARGEST_BULK} items at a time")
    if not all(isinstance(value, int) and not isinstance(value, bool) and 0 < value <= LARGEST_DATABASE_INTEGER
               for value in raw):
        raise PhotoError("ids must be whole numbers above zero")
    return list(dict.fromkeys(raw))


def _calendar_day(raw: str, field: str) -> date:
    if not DATE_PATTERN.fullmatch(raw):
        raise PhotoError(f"{field} must look like 2024-05-31")
    try:
        return date.fromisoformat(raw)
    except ValueError as error:
        raise PhotoError(f"{field} is not a real date") from error


def _page_size(raw) -> int:
    if raw in (None, ""):
        return DEFAULT_PAGE_SIZE
    try:
        value = int(raw)
    except (TypeError, ValueError) as error:
        raise PhotoError("limit must be a whole number") from error
    return max(1, min(value, LARGEST_PAGE_SIZE))


def _like_pattern(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def media_json(row: sqlite3.Row) -> dict:
    trashed_at = row["trashed_at"]
    return {
        "id": row["id"],
        "version": row["sha256"][:VERSION_LENGTH],
        "kind": row["kind"],
        "name": row["original_name"],
        "format": row["format"],
        "mime": row["mime"],
        "size": row["size"],
        "width": row["width"],
        "height": row["height"],
        "duration": row["duration"],
        "takenAt": row["taken_at"],
        "takenSource": row["taken_source"],
        "favourite": bool(row["favourite"]),
        "hasPreview": bool(row["has_preview"]),
        "trashedAt": trashed_at,
        "purgeAt": None if trashed_at is None else trashed_at + BIN_SECONDS,
    }


class Library:
    def __init__(self, database: Database, storage: Storage, clock=time.time):
        self.database = database
        self.storage = storage
        self.clock = clock
        self.files_lock = threading.RLock()

    def _row(self, connection, media_id: int) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM media WHERE id = ?", (media_id,)).fetchone()
        if row is None:
            raise PhotoError("no such photo or video", 404)
        return row

    def get(self, media_id: int) -> dict:
        with self.database.connect() as connection:
            row = self._row(connection, media_id)
            albums = connection.execute(
                "SELECT a.id, a.name FROM album_items ai JOIN albums a ON a.id = ai.album_id "
                "WHERE ai.media_id = ? ORDER BY a.name COLLATE NOCASE", (media_id,)).fetchall()
        return {**media_json(row), "albums": [{"id": album["id"], "name": album["name"]} for album in albums]}

    def stored(self, media_id: int) -> sqlite3.Row:
        with self.database.connect() as connection:
            return self._row(connection, media_id)

    def find_by_hash(self, sha256: str) -> sqlite3.Row | None:
        with self.database.connect() as connection:
            return connection.execute("SELECT * FROM media WHERE sha256 = ?", (sha256,)).fetchone()

    def insert(self, media: NewMedia) -> int:
        with self.database.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO media (sha256, kind, format, mime, extension, original_name, size, width, height, "
                "duration, taken_at, taken_source, has_preview, uploaded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (media.sha256, media.kind, media.format, media.mime, media.extension, media.original_name,
                 media.size, media.width, media.height, media.duration, media.taken_at, media.taken_source,
                 int(media.has_preview), self.clock()))
            return cursor.lastrowid

    def _filters(self, view: str, album_id: int | None, query: str, date_from: str, date_to: str):
        if view not in VIEWS:
            raise PhotoError(f"view must be one of {', '.join(VIEWS)}")
        join, clauses, params = "", [], []
        if view == "album":
            if album_id is None:
                raise PhotoError("pick an album")
            self._album_exists(album_id)
            join = "JOIN album_items ai ON ai.media_id = m.id AND ai.album_id = ?"
            params.append(album_id)
        clauses.append("m.trashed_at IS NOT NULL" if view == "bin" else "m.trashed_at IS NULL")
        if view == "favourites":
            clauses.append("m.favourite = 1")
        if query:
            if len(query) > SEARCH_LIMIT:
                raise PhotoError(f"search text can be at most {SEARCH_LIMIT} characters")
            clauses.append("m.original_name LIKE ? ESCAPE '\\'")
            params.append(_like_pattern(query))
        first_day = _calendar_day(date_from, "from") if date_from else None
        last_day = _calendar_day(date_to, "to") if date_to else None
        if first_day and last_day and first_day > last_day:
            raise PhotoError("the from date is after the to date")
        if first_day:
            clauses.append("m.taken_at >= ?")
            params.append(first_day.isoformat() + DAY_START)
        if last_day:
            clauses.append("m.taken_at < ?")
            params.append((last_day + timedelta(days=1)).isoformat() + DAY_START)
        return join, clauses, params

    def list_media(self, view: str = "timeline", album_id: int | None = None, query: str = "",
                   date_from: str = "", date_to: str = "", cursor: str = "", limit=None) -> dict:
        join, clauses, params = self._filters(view, album_id, query.strip(), date_from.strip(), date_to.strip())
        count_sql = f"SELECT COUNT(*) FROM media m {join} WHERE {' AND '.join(clauses)}"
        page_clauses, page_params = list(clauses), list(params)
        if cursor:
            match = CURSOR_PATTERN.fullmatch(cursor)
            if not match or int(match.group(2)) > LARGEST_DATABASE_INTEGER:
                raise PhotoError("that page marker is not valid")
            page_clauses.append("(m.taken_at, m.id) < (?, ?)")
            page_params += [match.group(1), int(match.group(2))]
        size = _page_size(limit)
        page_sql = (f"SELECT m.* FROM media m {join} WHERE {' AND '.join(page_clauses)} "
                    "ORDER BY m.taken_at DESC, m.id DESC LIMIT ?")
        with self.database.connect() as connection:
            total = connection.execute(count_sql, params).fetchone()[0]
            rows = connection.execute(page_sql, [*page_params, size + 1]).fetchall()
        more = len(rows) > size
        rows = rows[:size]
        next_cursor = f"{rows[-1]['taken_at']}~{rows[-1]['id']}" if more else None
        return {"items": [media_json(row) for row in rows], "total": total, "nextCursor": next_cursor}

    def bulk(self, action, ids) -> dict:
        if action not in BULK_ACTIONS:
            raise PhotoError(f"action must be one of {', '.join(BULK_ACTIONS)}")
        chosen = id_list(ids)
        if action in ("favourite", "unfavourite"):
            return {"changed": self._set_favourite(chosen, action == "favourite")}
        if action == "trash":
            return {"changed": self._trash(chosen)}
        if action == "restore":
            return {"changed": self._restore(chosen)}
        return {"changed": self._delete_forever(chosen, only_trashed=True)}

    def _update_many(self, sql: str, leading: tuple, ids: list[int]) -> int:
        marks = ",".join("?" * len(ids))
        with self.database.connect() as connection:
            return connection.execute(sql.format(marks=marks), (*leading, *ids)).rowcount

    def _set_favourite(self, ids: list[int], value: bool) -> int:
        return self._update_many("UPDATE media SET favourite = ? WHERE id IN ({marks})", (int(value),), ids)

    def _trash(self, ids: list[int]) -> int:
        return self._update_many("UPDATE media SET trashed_at = ? WHERE trashed_at IS NULL AND id IN ({marks})",
                                 (self.clock(),), ids)

    def _restore(self, ids: list[int]) -> int:
        return self._update_many("UPDATE media SET trashed_at = NULL WHERE trashed_at IS NOT NULL AND id IN ({marks})",
                                 (), ids)

    def restore_one(self, media_id: int) -> None:
        self._restore([media_id])

    def _delete_forever(self, ids: list[int], only_trashed: bool) -> int:
        marks = ",".join("?" * len(ids))
        condition = "trashed_at IS NOT NULL AND " if only_trashed else ""
        return self._delete_where(f"{condition}id IN ({marks})", tuple(ids))

    def _remove_files(self, row: sqlite3.Row) -> None:
        try:
            self.storage.remove_stored(row["sha256"], row["extension"])
        except OSError as error:
            log.error("could not remove the files of item %s: %s", row["id"], error)

    def empty_bin(self) -> int:
        return self._delete_where("trashed_at IS NOT NULL", ())

    def purge_expired(self) -> int:
        return self._delete_where("trashed_at IS NOT NULL AND trashed_at <= ?", (self.clock() - BIN_SECONDS,))

    def _delete_where(self, condition: str, params: tuple) -> int:
        with self.files_lock:
            with self.database.connect() as connection:
                rows = connection.execute(f"SELECT id, sha256, extension FROM media WHERE {condition}",
                                          params).fetchall()
                deleted = [row for row in rows if connection.execute(
                    f"DELETE FROM media WHERE id = ? AND {condition}", (row["id"], *params)).rowcount]
            for row in deleted:
                self._remove_files(row)
        return len(deleted)

    def totals(self) -> dict:
        with self.database.connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count, COALESCE(SUM(size), 0) AS bytes FROM media").fetchone()
        return {"count": row["count"], "libraryBytes": row["bytes"]}

    def _album_exists(self, album_id: int) -> None:
        with self.database.connect() as connection:
            if connection.execute("SELECT 1 FROM albums WHERE id = ?", (album_id,)).fetchone() is None:
                raise PhotoError("no such album", 404)

    def albums(self) -> list[dict]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT a.id, a.name, a.updated_at, "
                "(SELECT COUNT(*) FROM album_items ai JOIN media m ON m.id = ai.media_id "
                " WHERE ai.album_id = a.id AND m.trashed_at IS NULL) AS count, "
                "(SELECT m.id FROM album_items ai JOIN media m ON m.id = ai.media_id "
                " WHERE ai.album_id = a.id AND m.trashed_at IS NULL "
                " ORDER BY m.has_preview DESC, m.taken_at DESC, m.id DESC LIMIT 1) AS cover_id "
                "FROM albums a ORDER BY a.name COLLATE NOCASE, a.id").fetchall()
        return [{"id": row["id"], "name": row["name"], "count": row["count"], "coverId": row["cover_id"]}
                for row in rows]

    def album(self, album_id: int) -> dict:
        found = next((album for album in self.albums() if album["id"] == album_id), None)
        if found is None:
            raise PhotoError("no such album", 404)
        return found

    def create_album(self, name) -> dict:
        cleaned = printable_text(name, "album name", ALBUM_NAME_LIMIT)
        now = self.clock()
        with self.database.connect() as connection:
            album_id = connection.execute("INSERT INTO albums (name, created_at, updated_at) VALUES (?, ?, ?)",
                                          (cleaned, now, now)).lastrowid
        return self.album(album_id)

    def rename_album(self, album_id: int, name) -> dict:
        cleaned = printable_text(name, "album name", ALBUM_NAME_LIMIT)
        with self.database.connect() as connection:
            changed = connection.execute("UPDATE albums SET name = ?, updated_at = ? WHERE id = ?",
                                         (cleaned, self.clock(), album_id)).rowcount
        if not changed:
            raise PhotoError("no such album", 404)
        return self.album(album_id)

    def delete_album(self, album_id: int) -> None:
        with self.database.connect() as connection:
            if not connection.execute("DELETE FROM albums WHERE id = ?", (album_id,)).rowcount:
                raise PhotoError("no such album", 404)

    def add_to_album(self, album_id: int, ids) -> dict:
        chosen = id_list(ids)
        self._album_exists(album_id)
        marks = ",".join("?" * len(chosen))
        now = self.clock()
        with self.database.connect() as connection:
            existing = [row["id"] for row in
                        connection.execute(f"SELECT id FROM media WHERE id IN ({marks})", chosen).fetchall()]
            added = connection.executemany(
                "INSERT OR IGNORE INTO album_items (album_id, media_id, added_at) VALUES (?, ?, ?)",
                [(album_id, media_id, now) for media_id in existing]).rowcount
            connection.execute("UPDATE albums SET updated_at = ? WHERE id = ?", (now, album_id))
        return {"added": added, "album": self.album(album_id)}

    def remove_from_album(self, album_id: int, ids) -> dict:
        chosen = id_list(ids)
        self._album_exists(album_id)
        marks = ",".join("?" * len(chosen))
        with self.database.connect() as connection:
            removed = connection.execute(f"DELETE FROM album_items WHERE album_id = ? AND media_id IN ({marks})",
                                         (album_id, *chosen)).rowcount
        return {"removed": removed, "album": self.album(album_id)}

    def original_file(self, media_id: int) -> tuple[sqlite3.Row, Path]:
        row = self.stored(media_id)
        return row, self.storage.original_path(row["sha256"], row["extension"])

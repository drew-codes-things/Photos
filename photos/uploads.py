import hashlib
import json
import logging
import re
import secrets
import threading
import time
import unicodedata
from datetime import datetime
from pathlib import Path

from .errors import PhotoError
from .imaging import ImageTooLarge, process_image, thumbnail_from_poster
from .library import Library, NewMedia
from .sniff import HEADER_BYTES, detect
from .video import VideoTools, make_poster, probe_video

MAX_CHUNK_BYTES = 8 * 1024 * 1024
COPY_BLOCK_BYTES = 1024 * 1024
HASH_BLOCK_BYTES = 4 * 1024 * 1024
FILE_NAME_LIMIT = 200
FINGERPRINT_LIMIT = 600
STALE_UPLOAD_SECONDS = 3 * 24 * 60 * 60
EMPTY_UPLOAD_SECONDS = 60 * 60
FINISHED_UPLOAD_SECONDS = 24 * 60 * 60
FILE_TIME_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
EARLIEST_FILE_YEAR = 1971
UNNAMED = "untitled"
NOT_MEDIA_TEXT = "only photos and videos can be added, and this file is neither (or is a kind this library does not know)"
STOPPED_TEXT = ("the server stopped while adding this file, so it was not added; it may be too large or too damaged "
                "to open")
DELETED_SINCE_TEXT = "this upload finished, but the item it added has since been deleted forever"
UNPROCESSABLE = 422

log = logging.getLogger("photos.uploads")


class OffsetMismatch(PhotoError):
    def __init__(self, received: int):
        super().__init__(f"the server has {received} bytes of this file; carry on from there", 409)
        self.received = received


def clean_file_name(raw) -> str:
    if not isinstance(raw, str):
        raise PhotoError("name must be text")
    base = re.split(r"[\\/]", raw)[-1]
    cleaned = "".join(character for character in unicodedata.normalize("NFC", base) if character.isprintable())
    return cleaned.strip()[:FILE_NAME_LIMIT] or UNNAMED


def clean_file_time(raw) -> str | None:
    if raw in (None, ""):
        return None
    if not isinstance(raw, str) or not FILE_TIME_PATTERN.fullmatch(raw):
        raise PhotoError("fileTime must look like 2024-05-31T14:03:00")
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError as error:
        raise PhotoError("fileTime is not a real date and time") from error
    return raw if moment.year >= EARLIEST_FILE_YEAR else None


def _whole_number(raw, field: str) -> int:
    if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
        raise PhotoError(f"{field} must be a whole number of bytes")
    return raw


def _status(row) -> dict:
    return {"id": row["id"], "name": row["name"], "size": row["size"], "received": row["received"],
            "complete": row["received"] == row["size"], "state": row["state"]}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(HASH_BLOCK_BYTES):
            digest.update(block)
    return digest.hexdigest()


def _header(path: Path) -> bytes:
    with path.open("rb") as handle:
        return handle.read(HEADER_BYTES)


class Uploads:
    def __init__(self, library: Library, max_file_bytes: int, max_pixels: int, tools: VideoTools | None = None,
                 clock=time.time):
        self.library = library
        self.database = library.database
        self.storage = library.storage
        self.max_file_bytes = max_file_bytes
        self.max_pixels = max_pixels
        self.tools = tools or VideoTools.find()
        self.clock = clock
        self.start_lock = threading.Lock()
        self.locks_guard = threading.Lock()
        self.upload_locks: dict[str, threading.Lock] = {}

    def _lock_for(self, upload_id: str) -> threading.Lock:
        with self.locks_guard:
            return self.upload_locks.setdefault(upload_id, threading.Lock())

    def _forget_lock(self, upload_id: str) -> None:
        with self.locks_guard:
            self.upload_locks.pop(upload_id, None)

    def _row(self, upload_id: str):
        self.storage.incoming_path(upload_id)
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM uploads WHERE id = ?", (upload_id,)).fetchone()
        if row is None:
            raise PhotoError("no such upload; it may have finished or been cleared, so start it again", 404)
        return row

    def outstanding_bytes(self) -> int:
        with self.database.connect() as connection:
            return connection.execute(
                "SELECT COALESCE(SUM(size - received), 0) FROM uploads WHERE state = 'receiving'").fetchone()[0]

    def start(self, payload: dict) -> dict:
        name = clean_file_name(payload.get("name"))
        size = _whole_number(payload.get("size"), "size")
        if size == 0:
            raise PhotoError("that file is empty")
        if size > self.max_file_bytes:
            raise PhotoError(f"that file is larger than the {self.max_file_bytes // (1024 * 1024)} MB limit", 413)
        fingerprint = payload.get("fingerprint")
        if not isinstance(fingerprint, str) or not 0 < len(fingerprint) <= FINGERPRINT_LIMIT:
            raise PhotoError("fingerprint must be text of up to 600 characters")
        file_time = clean_file_time(payload.get("fileTime"))
        with self.start_lock:
            return self._start_or_resume(name, size, fingerprint, file_time)

    def _start_or_resume(self, name: str, size: int, fingerprint: str, file_time: str | None) -> dict:
        with self.database.connect() as connection:
            existing = connection.execute("SELECT * FROM uploads WHERE fingerprint = ?", (fingerprint,)).fetchone()
        if existing is not None and existing["state"] == "ingesting":
            raise PhotoError("this file is being added right now; wait for it to finish", 409)
        if (existing is not None and existing["state"] == "receiving" and existing["size"] == size
                and self.storage.incoming_path(existing["id"]).is_file()):
            return {**_status(existing), "resumed": True}
        if existing is not None:
            self.cancel(existing["id"])
        self.storage.ensure_room_for(size, reserved=self.outstanding_bytes())
        upload_id = secrets.token_hex(16)
        self.storage.incoming_path(upload_id).touch(mode=0o600, exist_ok=False)
        now = self.clock()
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO uploads (id, fingerprint, name, size, received, file_time, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 0, ?, ?, ?)", (upload_id, fingerprint, name, size, file_time, now, now))
        return {**_status(self._row(upload_id)), "resumed": False}

    def status(self, upload_id: str) -> dict:
        return _status(self._row(upload_id))

    def append(self, upload_id: str, offset: int, length: int | None, stream) -> dict:
        if length is None:
            raise PhotoError("send the chunk with a Content-Length", 411)
        if length <= 0 or length > MAX_CHUNK_BYTES:
            raise PhotoError(f"each chunk must be between 1 byte and {MAX_CHUNK_BYTES // (1024 * 1024)} MB", 413)
        self._row(upload_id)
        with self._lock_for(upload_id):
            row = self._row(upload_id)
            if row["state"] != "receiving" or offset != row["received"]:
                raise OffsetMismatch(row["received"])
            if offset + length > row["size"]:
                raise PhotoError("that chunk runs past the end of the file")
            self.storage.ensure_room_for(length)
            path = self.storage.incoming_path(upload_id)
            if not path.is_file():
                self.cancel(upload_id)
                raise PhotoError("the partial file was cleared; start this upload again", 410)
            written = self._write_at(path, offset, length, stream)
            if written != length:
                raise PhotoError("the chunk was cut short; send it again")
            with self.database.connect() as connection:
                connection.execute("UPDATE uploads SET received = ?, updated_at = ? WHERE id = ?",
                                   (offset + written, self.clock(), upload_id))
        return self.status(upload_id)

    @staticmethod
    def _write_at(path: Path, offset: int, length: int, stream) -> int:
        written = 0
        with path.open("r+b") as handle:
            handle.seek(offset)
            while written < length:
                block = stream.read(min(COPY_BLOCK_BYTES, length - written))
                if not block:
                    break
                handle.write(block)
                written += len(block)
            handle.truncate()
        return written

    def cancel(self, upload_id: str) -> None:
        path = self.storage.incoming_path(upload_id)
        with self.database.connect() as connection:
            connection.execute("DELETE FROM uploads WHERE id = ?", (upload_id,))
        path.unlink(missing_ok=True)
        self._forget_lock(upload_id)

    def finish(self, upload_id: str) -> dict:
        self._row(upload_id)
        with self._lock_for(upload_id):
            row = self._row(upload_id)
            if row["state"] in ("done", "failed"):
                return self._answer_again(row)
            if row["state"] == "ingesting":
                self._fail(upload_id, PhotoError(STOPPED_TEXT, UNPROCESSABLE))
                raise PhotoError(STOPPED_TEXT, UNPROCESSABLE)
            if row["received"] != row["size"]:
                raise OffsetMismatch(row["received"])
            self._settle(upload_id, "ingesting")
            return self._ingest_and_remember(row)

    def _ingest_and_remember(self, row) -> dict:
        try:
            with self.library.files_lock:
                result = self._ingest(row)
        except PhotoError as error:
            self._fail(row["id"], error)
            raise
        except Exception as error:
            self._fail(row["id"], PhotoError(f"adding this file failed on the server ({type(error).__name__})", 500))
            raise
        outcome = {"duplicate": result["duplicate"], "restored": result["restored"]}
        self._settle(row["id"], "done", result["item"]["id"], outcome)
        return result

    def _settle(self, upload_id: str, state: str, media_id: int | None = None, outcome: dict | None = None) -> None:
        with self.database.connect() as connection:
            connection.execute("UPDATE uploads SET state = ?, media_id = ?, outcome = ?, updated_at = ? WHERE id = ?",
                               (state, media_id, None if outcome is None else json.dumps(outcome), self.clock(),
                                upload_id))
        if state != "ingesting":
            self.storage.incoming_path(upload_id).unlink(missing_ok=True)

    def _fail(self, upload_id: str, error: PhotoError) -> None:
        self._settle(upload_id, "failed", outcome={"status": error.status, "error": str(error)})

    def _answer_again(self, row) -> dict:
        outcome = json.loads(row["outcome"])
        if row["state"] == "failed":
            raise PhotoError(outcome["error"], outcome["status"])
        try:
            item = self.library.get(row["media_id"])
        except PhotoError as error:
            raise PhotoError(DELETED_SINCE_TEXT, 410) from error
        return {"duplicate": outcome["duplicate"], "restored": outcome["restored"], "item": item}

    def _ingest(self, row) -> dict:
        path = self.storage.incoming_path(row["id"])
        if not path.is_file() or path.stat().st_size != row["size"]:
            raise PhotoError("the uploaded file is incomplete on the server; start it again", 410)
        file_type = detect(_header(path))
        if file_type is None:
            raise PhotoError(NOT_MEDIA_TEXT, 415)
        sha256 = file_sha256(path)
        existing = self.library.find_by_hash(sha256)
        if existing is not None:
            return self._duplicate(existing)
        thumb, preview = self.storage.thumb_path(sha256), self.storage.preview_path(sha256)
        if file_type.kind == "image":
            facts = self._image_facts(path, thumb, preview)
        else:
            facts = self._video_facts(path, thumb, preview)
        taken_at, taken_source = self._taken(facts["taken_at"], facts["source"], row["file_time"])
        self.storage.place_original(path, sha256, file_type.extension)
        media = NewMedia(sha256=sha256, kind=file_type.kind, format=file_type.format, mime=file_type.mime,
                         extension=file_type.extension, original_name=row["name"], size=row["size"],
                         width=facts["width"], height=facts["height"], duration=facts["duration"],
                         taken_at=taken_at, taken_source=taken_source, has_preview=facts["has_preview"])
        try:
            media_id = self.library.insert(media)
        except Exception:
            self.storage.remove_stored(sha256, file_type.extension)
            raise
        return {"duplicate": False, "restored": False, "item": self.library.get(media_id)}

    def _duplicate(self, existing) -> dict:
        restored = existing["trashed_at"] is not None
        if restored:
            self.library.restore_one(existing["id"])
        return {"duplicate": True, "restored": restored, "item": self.library.get(existing["id"])}

    def _image_facts(self, path: Path, thumb: Path, preview: Path) -> dict:
        try:
            facts = process_image(path, thumb, preview, self.max_pixels)
        except ImageTooLarge as error:
            raise PhotoError(str(error), 413) from error
        return {"width": facts.width, "height": facts.height, "duration": None, "taken_at": facts.taken_at,
                "source": "photo", "has_preview": facts.has_preview}

    def _video_facts(self, path: Path, thumb: Path, preview: Path) -> dict:
        facts = probe_video(self.tools, path)
        has_preview = make_poster(self.tools, path, preview, facts.duration)
        if has_preview:
            try:
                thumbnail_from_poster(preview, thumb)
            except OSError as error:
                log.warning("could not shrink the poster of %s: %s", path.name, error)
                preview.unlink(missing_ok=True)
                has_preview = False
        return {"width": facts.width, "height": facts.height, "duration": facts.duration,
                "taken_at": facts.taken_at, "source": "video", "has_preview": has_preview}

    def _taken(self, from_metadata: str | None, metadata_source: str, file_time: str | None) -> tuple[str, str]:
        if from_metadata:
            return from_metadata, metadata_source
        if file_time:
            return file_time, "file"
        return datetime.fromtimestamp(self.clock()).replace(microsecond=0).isoformat(), "upload"

    def purge_stale(self) -> int:
        now = self.clock()
        cutoff = now - STALE_UPLOAD_SECONDS
        with self.database.connect() as connection:
            stale = [row["id"] for row in connection.execute(
                "SELECT id FROM uploads WHERE (state = 'receiving' AND received = 0 AND updated_at < ?) "
                "OR (state IN ('receiving', 'ingesting') AND updated_at < ?) "
                "OR (state IN ('done', 'failed') AND updated_at < ?)",
                (now - EMPTY_UPLOAD_SECONDS, cutoff, now - FINISHED_UPLOAD_SECONDS)).fetchall()]
        for upload_id in stale:
            self.cancel(upload_id)
        return len(stale) + self._remove_orphan_parts(cutoff)

    def _remove_orphan_parts(self, cutoff: float) -> int:
        with self.database.connect() as connection:
            known = {row["id"] for row in connection.execute("SELECT id FROM uploads").fetchall()}
        removed = 0
        for part in self.storage.incoming.glob("*.part"):
            if part.stem not in known and part.stat().st_mtime < cutoff:
                part.unlink(missing_ok=True)
                removed += 1
        return removed


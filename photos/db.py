import sqlite3
from contextlib import contextmanager
from pathlib import Path

BUSY_TIMEOUT_SECONDS = 10

MEDIA_TABLE = """
CREATE TABLE IF NOT EXISTS {name} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
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
"""
MEDIA_COLUMNS = ("id, sha256, kind, format, mime, extension, original_name, size, width, height, duration, taken_at, "
                 "taken_source, has_preview, favourite, trashed_at, uploaded_at")
ADDED_UPLOAD_COLUMNS = (
    ("state", "TEXT NOT NULL DEFAULT 'receiving' CHECK (state IN ('receiving', 'ingesting', 'done', 'failed'))"),
    ("media_id", "INTEGER"),
    ("outcome", "TEXT"),
)

SCHEMA = MEDIA_TABLE.format(name="media") + """
CREATE TABLE IF NOT EXISTS albums (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS album_items (
    album_id INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
    media_id INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
    added_at REAL NOT NULL,
    PRIMARY KEY (album_id, media_id)
);
CREATE TABLE IF NOT EXISTS uploads (
    id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    size INTEGER NOT NULL,
    received INTEGER NOT NULL DEFAULT 0,
    file_time TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
""" + ",\n".join(f"    {name} {definition}" for name, definition in ADDED_UPLOAD_COLUMNS) + """
);
"""
INDEX_STATEMENTS = (
    "CREATE INDEX IF NOT EXISTS media_by_taken ON media (taken_at DESC, id DESC)",
    "CREATE INDEX IF NOT EXISTS media_by_trashed ON media (trashed_at)",
    "CREATE INDEX IF NOT EXISTS album_items_by_media ON album_items (media_id)",
)


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(SCHEMA)
            self._add_missing_upload_columns(connection)
        if not self._media_ids_are_never_reused():
            self._rebuild_media_table()
        with self.connect() as connection:
            for statement in INDEX_STATEMENTS:
                connection.execute(statement)

    @staticmethod
    def _add_missing_upload_columns(connection: sqlite3.Connection) -> None:
        present = {row["name"] for row in connection.execute("PRAGMA table_info(uploads)")}
        for name, definition in ADDED_UPLOAD_COLUMNS:
            if name not in present:
                connection.execute(f"ALTER TABLE uploads ADD COLUMN {name} {definition}")

    def _media_ids_are_never_reused(self) -> bool:
        with self.connect() as connection:
            table = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'media'").fetchone()
        return "AUTOINCREMENT" in table["sql"].upper()

    def _rebuild_media_table(self) -> None:
        connection = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None)
        try:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DROP TABLE IF EXISTS media_rebuilt")
            connection.execute(MEDIA_TABLE.format(name="media_rebuilt"))
            connection.execute(f"INSERT INTO media_rebuilt ({MEDIA_COLUMNS}) SELECT {MEDIA_COLUMNS} FROM media")
            connection.execute("DROP TABLE media")
            connection.execute("ALTER TABLE media_rebuilt RENAME TO media")
            broken = connection.execute("PRAGMA foreign_key_check").fetchall()
            if broken:
                raise sqlite3.IntegrityError(f"moving media to never-reused ids broke {len(broken)} links")
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def _open(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_SECONDS)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def connect(self):
        connection = self._open()
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def is_reachable(self) -> bool:
        try:
            with self.connect() as connection:
                connection.execute("SELECT COUNT(*) FROM media").fetchone()
        except sqlite3.Error:
            return False
        return True

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .errors import PhotoError, StorageFull

SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
UPLOAD_ID_PATTERN = re.compile(r"[0-9a-f]{32}")
EXTENSION_PATTERN = re.compile(r"[a-z0-9]{2,5}")
SHARD_LENGTH = 2
GIGABYTE = 1024 ** 3
SMALLEST_USEFUL_ROOM = 1024 ** 2


@dataclass(frozen=True)
class DiskSpace:
    total: int
    free: int
    min_free: int
    reserved: int = 0

    @property
    def room(self) -> int:
        return max(0, self.free - self.min_free - self.reserved)

    def as_json(self) -> dict:
        return {"totalBytes": self.total, "freeBytes": self.free, "minFreeBytes": self.min_free,
                "reservedBytes": self.reserved, "roomBytes": self.room,
                "uploadsAllowed": self.room >= SMALLEST_USEFUL_ROOM}


def _gigabytes(size: int) -> str:
    return f"{size / GIGABYTE:.1f} GB"


class Storage:
    def __init__(self, root: Path, min_free_bytes: int, disk_usage=shutil.disk_usage):
        self.root = Path(root)
        self.min_free_bytes = min_free_bytes
        self.disk_usage = disk_usage
        self.originals = self.root / "originals"
        self.renditions = self.root / "renditions"
        self.incoming = self.root / "incoming"
        for folder in (self.originals, self.renditions, self.incoming):
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _sharded(self, base: Path, sha256: str, suffix: str) -> Path:
        if not SHA256_PATTERN.fullmatch(sha256 or ""):
            raise PhotoError("that is not a stored file", 404)
        return base / sha256[:SHARD_LENGTH] / f"{sha256}{suffix}"

    def original_path(self, sha256: str, extension: str) -> Path:
        if not EXTENSION_PATTERN.fullmatch(extension or ""):
            raise PhotoError("that is not a stored file", 404)
        return self._sharded(self.originals, sha256, f".{extension}")

    def thumb_path(self, sha256: str) -> Path:
        return self._sharded(self.renditions, sha256, "-thumb.jpg")

    def preview_path(self, sha256: str) -> Path:
        return self._sharded(self.renditions, sha256, "-preview.jpg")

    def incoming_path(self, upload_id: str) -> Path:
        if not UPLOAD_ID_PATTERN.fullmatch(upload_id or ""):
            raise PhotoError("no such upload", 404)
        return self.incoming / f"{upload_id}.part"

    def space(self, reserved: int = 0) -> DiskSpace:
        usage = self.disk_usage(self.root)
        return DiskSpace(total=usage.total, free=usage.free, min_free=self.min_free_bytes, reserved=reserved)

    def ensure_room_for(self, size: int, reserved: int = 0) -> None:
        space = self.space(reserved)
        if space.free - reserved - size >= space.min_free:
            return
        held = f", {_gigabytes(reserved)} of it held for uploads still arriving," if reserved else ""
        raise StorageFull(f"not enough disk space: {_gigabytes(space.free)} is free{held} and uploads stop when "
                          f"less than {_gigabytes(space.min_free)} would be left")

    def place_original(self, incoming: Path, sha256: str, extension: str) -> Path:
        target = self.original_path(sha256, extension)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.replace(incoming, target)
        return target

    def remove_stored(self, sha256: str, extension: str) -> None:
        for path in (self.original_path(sha256, extension), self.thumb_path(sha256), self.preview_path(sha256)):
            path.unlink(missing_ok=True)

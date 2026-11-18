import json
import logging
import math
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

PROBE_TIMEOUT_SECONDS = 60
POSTER_TIMEOUT_SECONDS = 120
POSTER_AT_SECONDS = 1.0
POSTER_EDGE = 2048
PROBE_OUTPUT_LIMIT = 4 * 1024 * 1024
APPLE_CREATION_DATE = "com.apple.quicktime.creationdate"
CREATION_TIME = "creation_time"
ROTATED_DEGREES = (90, 270)
EARLIEST_YEAR = 1971

LOCAL_FILES_ONLY = ("-protocol_whitelist", "file")

log = logging.getLogger("photos.video")


@dataclass(frozen=True)
class VideoFacts:
    width: int | None
    height: int | None
    duration: float | None
    taken_at: str | None


NO_FACTS = VideoFacts(None, None, None, None)


@dataclass(frozen=True)
class VideoTools:
    ffprobe: str | None
    ffmpeg: str | None

    @classmethod
    def find(cls) -> "VideoTools":
        return cls(ffprobe=shutil.which("ffprobe"), ffmpeg=shutil.which("ffmpeg"))

    @property
    def can_probe(self) -> bool:
        return self.ffprobe is not None

    @property
    def can_make_posters(self) -> bool:
        return self.ffmpeg is not None


def wall_clock_from_metadata(raw) -> str | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        moment = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    if moment.year < EARLIEST_YEAR:
        return None
    if moment.utcoffset() == timedelta(0):
        moment = moment.astimezone()
    return moment.replace(tzinfo=None, microsecond=0).isoformat()


def _taken_at(probe: dict) -> str | None:
    tags = [probe.get("format", {}).get("tags", {})]
    tags += [stream.get("tags", {}) for stream in probe.get("streams", [])]
    for key in (APPLE_CREATION_DATE, CREATION_TIME):
        for group in tags:
            found = wall_clock_from_metadata(group.get(key))
            if found:
                return found
    return None


def _rotation(stream: dict) -> int:
    for side_data in stream.get("side_data_list", []):
        if "rotation" in side_data:
            return abs(int(side_data["rotation"])) % 360
    raw = stream.get("tags", {}).get("rotate", "0")
    digits = str(raw).lstrip("-")
    return abs(int(raw)) % 360 if digits.isascii() and digits.isdigit() else 0


def _size(probe: dict) -> tuple[int | None, int | None]:
    video_streams = [stream for stream in probe.get("streams", []) if stream.get("codec_type") == "video"]
    if not video_streams:
        return None, None
    stream = video_streams[0]
    width, height = stream.get("width"), stream.get("height")
    if not isinstance(width, int) or not isinstance(height, int):
        return None, None
    if _rotation(stream) in ROTATED_DEGREES:
        return height, width
    return width, height


def _duration(probe: dict) -> float | None:
    try:
        duration = float(probe.get("format", {}).get("duration"))
    except (TypeError, ValueError):
        return None
    return duration if math.isfinite(duration) and duration >= 0 else None


def probe_video(tools: VideoTools, source: Path) -> VideoFacts:
    if not tools.can_probe:
        return NO_FACTS
    command = [tools.ffprobe, "-v", "error", *LOCAL_FILES_ONLY, "-print_format", "json", "-show_format",
               "-show_streams", str(source)]
    try:
        finished = subprocess.run(command, capture_output=True, timeout=PROBE_TIMEOUT_SECONDS, check=True)
        probe = json.loads(finished.stdout[:PROBE_OUTPUT_LIMIT])
    except (subprocess.SubprocessError, OSError, ValueError) as error:
        log.warning("ffprobe could not read %s: %s", source.name, error)
        return NO_FACTS
    if not isinstance(probe, dict):
        return NO_FACTS
    width, height = _size(probe)
    return VideoFacts(width, height, _duration(probe), _taken_at(probe))


def _poster_command(tools: VideoTools, source: Path, target: Path, at_seconds: float) -> list[str]:
    scale = f"scale='min({POSTER_EDGE},iw)':'min({POSTER_EDGE},ih)':force_original_aspect_ratio=decrease"
    return [tools.ffmpeg, "-v", "error", "-nostdin", "-y", *LOCAL_FILES_ONLY, "-ss", f"{at_seconds:.2f}",
            "-i", str(source), "-frames:v", "1", "-vf", scale, "-f", "image2", "-c:v", "mjpeg", str(target)]


def make_poster(tools: VideoTools, source: Path, target: Path, duration: float | None) -> bool:
    if not tools.can_make_posters:
        return False
    at_seconds = min(POSTER_AT_SECONDS, duration / 2) if duration else 0.0
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    partial = target.with_name(f".{target.name}.partial")
    try:
        subprocess.run(_poster_command(tools, source, partial, at_seconds), capture_output=True,
                       timeout=POSTER_TIMEOUT_SECONDS, check=True)
    except (subprocess.SubprocessError, OSError) as error:
        log.warning("ffmpeg could not make a poster for %s: %s", source.name, error)
        partial.unlink(missing_ok=True)
        return False
    if not partial.is_file() or partial.stat().st_size == 0:
        partial.unlink(missing_ok=True)
        return False
    os.replace(partial, target)
    return True

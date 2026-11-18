import shutil
import subprocess

import pytest

from photos.video import VideoTools, make_poster, probe_video, wall_clock_from_metadata

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                                  reason="ffmpeg and ffprobe are not installed")


def make_clip(target, seconds=2, size="160x120", metadata=()):
    command = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size={size}:rate=10",
               "-pix_fmt", "yuv420p"]
    for entry in metadata:
        command += ["-metadata", entry]
    subprocess.run([*command, str(target)], check=True, capture_output=True, timeout=60)
    return target


@pytest.mark.parametrize("raw, expected", [
    ("2024-05-01T13:34:56+0100", "2024-05-01T13:34:56"),
    ("2024-07-01T12:00:00-04:00", "2024-07-01T12:00:00"),
    ("1970-01-01T00:00:00.000000Z", None),
    ("not a date", None),
    ("", None),
    (None, None),
])
def test_dates_with_their_own_offset_keep_their_wall_clock(raw, expected):
    assert wall_clock_from_metadata(raw) == expected


def test_utc_creation_times_are_shown_in_local_time(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/London")
    import time
    time.tzset()
    try:
        assert wall_clock_from_metadata("2024-07-01T12:34:56.000000Z") == "2024-07-01T13:34:56"
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()


def test_without_ffmpeg_a_video_is_stored_without_facts_or_poster(tmp_path):
    tools = VideoTools(ffprobe=None, ffmpeg=None)
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"\x00" * 100)
    assert probe_video(tools, source).duration is None
    assert make_poster(tools, source, tmp_path / "poster.jpg", None) is False
    assert not (tmp_path / "poster.jpg").exists()


@needs_ffmpeg
def test_a_clip_is_measured_dated_and_given_a_poster(tmp_path):
    clip = make_clip(tmp_path / "clip.mp4", metadata=("creation_time=2023-03-04T10:11:12.000000Z",))
    tools = VideoTools.find()
    facts = probe_video(tools, clip)
    assert (facts.width, facts.height) == (160, 120)
    assert facts.duration == pytest.approx(2, abs=0.2)
    assert facts.taken_at is not None and facts.taken_at.startswith("2023-03-04T")
    poster = tmp_path / "poster.jpg"
    assert make_poster(tools, clip, poster, facts.duration)
    assert poster.read_bytes()[:3] == b"\xff\xd8\xff"


@needs_ffmpeg
def test_a_file_ffmpeg_cannot_read_gets_no_poster(tmp_path):
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 100)
    tools = VideoTools.find()
    assert probe_video(tools, broken).width is None
    assert make_poster(tools, broken, tmp_path / "poster.jpg", None) is False
    assert not list(tmp_path.glob("*poster*"))


def test_ffmpeg_and_ffprobe_may_only_read_local_files(tmp_path, monkeypatch):
    from photos import video
    commands = []

    def record(command, **kwargs):
        commands.append(command)
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(video.subprocess, "run", record)
    tools = VideoTools(ffprobe="/usr/bin/ffprobe", ffmpeg="/usr/bin/ffmpeg")
    source = tmp_path / "clip.mp4"
    probe_video(tools, source)
    make_poster(tools, source, tmp_path / "poster.jpg", 2.0)
    assert len(commands) == 2
    for command in commands:
        position = command.index("-protocol_whitelist")
        assert command[position + 1] == "file"
        assert position < command.index(str(source))

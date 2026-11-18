import subprocess
import sys
from pathlib import Path

from PIL import Image

REPO = Path(__file__).resolve().parent.parent
PROBE_WIDTH = 6000
PROBE_HEIGHT = 4000
PROBE_PIXELS = PROBE_WIDTH * PROBE_HEIGHT
PROBE_SCRIPT = """
import re, sys, warnings
from pathlib import Path
warnings.simplefilter("ignore")
from photos.imaging import process_image
def peak_bytes():
    status = Path("/proc/self/status").read_text()
    return int(re.search(r"VmHWM:\\s+(\\d+) kB", status).group(1)) * 1024
source, folder = Path(sys.argv[1]), Path(sys.argv[2])
before = peak_bytes()
facts = process_image(source, folder / "t.jpg", folder / "p.jpg", 10 ** 12)
after = peak_bytes()
assert facts.has_preview
print(before, after)
"""


SEE_THROUGH_GREEN = (10, 200, 10, 128)
SAVE_OPTIONS = {"PNG": {"compress_level": 1}, "WEBP": {"lossless": True, "method": 0}}


def see_through_image(folder: Path, image_format: str) -> Path:
    path = folder / f"probe.{image_format.lower()}"
    image = Image.new("RGBA", (PROBE_WIDTH, PROBE_HEIGHT), SEE_THROUGH_GREEN)
    image.save(path, image_format, **SAVE_OPTIONS[image_format])
    return path


def bytes_per_pixel(folder: Path, image_format: str) -> float:
    source = see_through_image(folder, image_format)
    finished = subprocess.run([sys.executable, "-c", PROBE_SCRIPT, str(source), str(folder)], cwd=REPO,
                              capture_output=True, text=True, timeout=300, check=True)
    before, after = (int(value) for value in finished.stdout.split())
    return (after - before) / PROBE_PIXELS

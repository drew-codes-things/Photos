from dataclasses import dataclass

HEADER_BYTES = 64
FTYP_OFFSET = 4
BRAND_START = 8
BRAND_LENGTH = 4
COMPATIBLE_BRANDS_START = 16
EBML_MAGIC = b"\x1a\x45\xdf\xa3"


@dataclass(frozen=True)
class FileType:
    kind: str
    format: str
    mime: str
    extension: str
    other_extensions: tuple[str, ...] = ()

    @property
    def known_extensions(self) -> frozenset[str]:
        return frozenset((self.extension, *self.other_extensions))


JPEG = FileType("image", "jpeg", "image/jpeg", "jpg", ("jpeg", "jpe", "jfif"))
PNG = FileType("image", "png", "image/png", "png")
GIF = FileType("image", "gif", "image/gif", "gif")
WEBP = FileType("image", "webp", "image/webp", "webp")
HEIC = FileType("image", "heic", "image/heic", "heic", ("heif", "hif"))
AVIF = FileType("image", "avif", "image/avif", "avif")
MP4 = FileType("video", "mp4", "video/mp4", "mp4", ("m4v",))
MOV = FileType("video", "mov", "video/quicktime", "mov", ("qt",))
THREE_GP = FileType("video", "3gp", "video/3gpp", "3gp", ("3gpp", "3g2"))
WEBM = FileType("video", "webm", "video/webm", "webm")
MATROSKA = FileType("video", "mkv", "video/x-matroska", "mkv")

ALL_TYPES = (JPEG, PNG, GIF, WEBP, HEIC, AVIF, MP4, MOV, THREE_GP, WEBM, MATROSKA)

HEIF_BRANDS = {b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx"}
GENERIC_HEIF_BRANDS = {b"mif1", b"msf1"}
AVIF_BRANDS = {b"avif", b"avis"}
QUICKTIME_BRANDS = {b"qt  "}
MP4_BRANDS = {b"isom", b"iso2", b"iso4", b"iso5", b"iso6", b"mp41", b"mp42", b"avc1", b"M4V ", b"M4VH",
              b"M4VP", b"dash", b"mmp4", b"MSNV", b"XAVC", b"f4v "}
THREE_GP_BRANDS = {b"3gp4", b"3gp5", b"3gp6", b"3gp7", b"3g2a", b"3g2b", b"3g2c"}

BRAND_TYPES = (
    (AVIF_BRANDS, AVIF),
    (HEIF_BRANDS, HEIC),
    (QUICKTIME_BRANDS, MOV),
    (THREE_GP_BRANDS, THREE_GP),
    (MP4_BRANDS, MP4),
)


def _brands(header: bytes) -> list[bytes]:
    box_size = int.from_bytes(header[:FTYP_OFFSET], "big")
    end = min(max(box_size, COMPATIBLE_BRANDS_START), len(header))
    major = header[BRAND_START:BRAND_START + BRAND_LENGTH]
    compatible = [header[start:start + BRAND_LENGTH]
                  for start in range(COMPATIBLE_BRANDS_START, end - BRAND_LENGTH + 1, BRAND_LENGTH)]
    return [major, *compatible]


def _type_for_brand(brand: bytes) -> FileType | None:
    return next((file_type for known, file_type in BRAND_TYPES if brand in known), None)


def _iso_media_type(header: bytes) -> FileType | None:
    major, *compatible = _brands(header)
    by_major = _type_for_brand(major)
    if by_major is not None:
        return by_major
    by_compatible = next((found for found in map(_type_for_brand, compatible) if found is not None), None)
    if by_compatible is not None:
        return by_compatible
    return HEIC if major in GENERIC_HEIF_BRANDS else None


def detect(header: bytes) -> FileType | None:
    if header.startswith(b"\xff\xd8\xff"):
        return JPEG
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return PNG
    if header.startswith((b"GIF87a", b"GIF89a")):
        return GIF
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return WEBP
    if header[FTYP_OFFSET:BRAND_START] == b"ftyp":
        return _iso_media_type(header)
    if header.startswith(EBML_MAGIC):
        return WEBM if b"webm" in header else MATROSKA
    return None


def type_named(file_format: str) -> FileType | None:
    return next((file_type for file_type in ALL_TYPES if file_type.format == file_format), None)

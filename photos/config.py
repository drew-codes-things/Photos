import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path

AUTH_MODES = ("builtin", "proxy", "local")
SECURE_COOKIE_CHOICES = ("auto", "true", "false")
ENV_FILE = Path(".env")
LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")
DEFAULT_PORT = 8098
DEFAULT_USER_HEADER = "X-Forwarded-User"
DEFAULT_MIN_FREE_GB = 15
DEFAULT_MAX_FILE_MB = 4096
DEFAULT_MAX_MEGAPIXELS = 120
BYTES_PER_GB = 1024 ** 3
BYTES_PER_MB = 1024 ** 2
PIXELS_PER_MEGAPIXEL = 1_000_000


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    bind_host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    auth_mode: str = "builtin"
    trusted_proxies: tuple[str, ...] = ()
    user_header: str = DEFAULT_USER_HEADER
    allowed_users: tuple[str, ...] = ()
    local_user: str = "local"
    secret_key: str = ""
    secure_cookies: str = "auto"
    min_free_gb: int = DEFAULT_MIN_FREE_GB
    max_file_mb: int = DEFAULT_MAX_FILE_MB
    max_megapixels: int = DEFAULT_MAX_MEGAPIXELS
    allowed_hosts: tuple[str, ...] = ()

    @property
    def database_path(self) -> Path:
        return self.data_dir / "photos.db"

    @property
    def min_free_bytes(self) -> int:
        return self.min_free_gb * BYTES_PER_GB

    @property
    def max_file_bytes(self) -> int:
        return self.max_file_mb * BYTES_PER_MB

    @property
    def max_pixels(self) -> int:
        return self.max_megapixels * PIXELS_PER_MEGAPIXEL


def _text(name: str, default: str) -> str:
    value = os.getenv(name)
    return (default if value is None else value).strip()


def _number(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = _text(name, str(default))
    try:
        value = int(raw)
    except ValueError as error:
        raise ConfigError(f"{name} must be a whole number, got {raw!r}") from error
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}, got {value}")
    return value


def _list(name: str, default: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in _text(name, default).split(",") if part.strip())


def _has_host_bits(entry: str) -> bool:
    try:
        ipaddress.ip_network(entry, strict=True)
    except ValueError:
        return True
    return False


def _check_networks(name: str, entries: tuple[str, ...]) -> None:
    if not entries:
        raise ConfigError(f"{name} needs at least one address: AUTH_MODE=proxy trusts the user header only from the proxy named there")
    for entry in entries:
        try:
            network = ipaddress.ip_network(entry, strict=False)
        except ValueError as error:
            raise ConfigError(f"{name} has {entry!r}, which is not an address or network") from error
        if _has_host_bits(entry):
            raise ConfigError(f"{name} has {entry!r}, which has host bits set; write {network} for the network "
                              f"or {entry.split('/')[0]} for one address")
        if network.num_addresses > 1 and not network.is_private:
            raise ConfigError(f"{name} has {entry!r}, which reaches public addresses; list the proxy's own address "
                              "or a private network")


def read_env_file(path: Path = ENV_FILE) -> None:
    if not path.is_file():
        return
    for number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if not separator or not name.strip():
            raise ConfigError(f"{path}:{number} should look like NAME=value")
        os.environ.setdefault(name.strip(), value.strip().strip('"').strip("'"))


def load_settings() -> Settings:
    read_env_file()
    auth_mode = _text("AUTH_MODE", "builtin").lower()
    if auth_mode not in AUTH_MODES:
        raise ConfigError(f"AUTH_MODE must be one of {', '.join(AUTH_MODES)}, got {auth_mode!r}")
    bind_host = _text("BIND_HOST", "127.0.0.1")
    if auth_mode == "local" and bind_host not in LOOPBACK_HOSTS:
        raise ConfigError("AUTH_MODE=local has no sign-in, so BIND_HOST must be 127.0.0.1, ::1 or localhost")
    trusted_proxies = _list("TRUSTED_PROXIES", "")
    if auth_mode == "proxy" or trusted_proxies:
        _check_networks("TRUSTED_PROXIES", trusted_proxies)
    secure_cookies = _text("SECURE_COOKIES", "auto").lower()
    if secure_cookies not in SECURE_COOKIE_CHOICES:
        raise ConfigError(f"SECURE_COOKIES must be one of {', '.join(SECURE_COOKIE_CHOICES)}, got {secure_cookies!r}")
    user_header = _text("USER_HEADER", DEFAULT_USER_HEADER)
    if not user_header:
        raise ConfigError("USER_HEADER cannot be empty")
    return Settings(
        data_dir=Path(_text("DATA_DIR", "./data")).resolve(),
        bind_host=bind_host,
        port=_number("PORT", DEFAULT_PORT, 1, 65535),
        auth_mode=auth_mode,
        trusted_proxies=trusted_proxies,
        user_header=user_header,
        allowed_users=_list("ALLOWED_USERS", ""),
        local_user=_text("LOCAL_USER", "local") or "local",
        secret_key=_text("SECRET_KEY", ""),
        secure_cookies=secure_cookies,
        min_free_gb=_number("MIN_FREE_GB", DEFAULT_MIN_FREE_GB, 0, 100_000),
        max_file_mb=_number("MAX_FILE_MB", DEFAULT_MAX_FILE_MB, 1, 1_000_000),
        max_megapixels=_number("MAX_MEGAPIXELS", DEFAULT_MAX_MEGAPIXELS, 1, 2_000),
        allowed_hosts=tuple(host.lower() for host in _list("ALLOWED_HOSTS", "")),
    )

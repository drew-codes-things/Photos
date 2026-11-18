import pytest

from photos.config import ConfigError, load_settings

SETTING_NAMES = ("AUTH_MODE", "BIND_HOST", "PORT", "DATA_DIR", "TRUSTED_PROXIES", "USER_HEADER", "ALLOWED_USERS",
                 "LOCAL_USER", "SECRET_KEY", "MIN_FREE_GB", "MAX_FILE_MB", "MAX_MEGAPIXELS",
                 "ALLOWED_HOSTS")


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch, tmp_path):
    for name in SETTING_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)


def test_defaults_keep_fifteen_gigabytes_free():
    settings = load_settings()
    assert settings.min_free_gb == 15
    assert settings.min_free_bytes == 15 * 1024 ** 3
    assert settings.max_file_bytes == 4096 * 1024 ** 2
    assert settings.max_pixels == 120_000_000
    assert settings.auth_mode == "builtin"


def test_the_free_space_floor_is_a_setting(monkeypatch):
    monkeypatch.setenv("MIN_FREE_GB", "20")
    assert load_settings().min_free_bytes == 20 * 1024 ** 3


@pytest.mark.parametrize("name, value", [
    ("MIN_FREE_GB", "-1"), ("MIN_FREE_GB", "lots"), ("MAX_FILE_MB", "0"), ("MAX_MEGAPIXELS", "99999"),
    ("AUTH_MODE", "none"), ("TRUSTED_PROXIES", "0.0.0.0/0"), ("TRUSTED_PROXIES", "10.0.0.16/24"),
    ("TRUSTED_PROXIES", "not-an-address"), ("USER_HEADER", " "), ("PORT", "70000"),
])
def test_bad_settings_stop_the_app(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigError):
        load_settings()


def test_local_mode_refuses_to_listen_beyond_this_machine(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "local")
    monkeypatch.setenv("BIND_HOST", "0.0.0.0")
    with pytest.raises(ConfigError):
        load_settings()


def test_proxy_mode_may_listen_on_all_addresses_for_docker(monkeypatch):
    monkeypatch.setenv("BIND_HOST", "0.0.0.0")
    monkeypatch.setenv("TRUSTED_PROXIES", "10.0.0.16")
    settings = load_settings()
    assert settings.bind_host == "0.0.0.0"
    assert settings.trusted_proxies == ("10.0.0.16",)


def test_allowed_hosts_is_a_list_of_names(monkeypatch):
    monkeypatch.setenv("ALLOWED_HOSTS", "photos.example.com, Other.Example ")
    assert load_settings().allowed_hosts == ("photos.example.com", "other.example")


def test_allowed_hosts_defaults_to_any():
    assert load_settings().allowed_hosts == ()

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
DEPLOY = REPO / "deploy"
AFTER_RULES = """*filter
:ufw-after-input - [0:0]
-A ufw-after-input -p udp --dport 137 -j ufw-skip-to-policy-input
COMMIT

*filter
:DOCKER-USER - [0:0]
-A DOCKER-USER -p tcp -m conntrack --ctorigdstport 5003 --ctdir ORIGINAL -s 10.0.0.11 -j RETURN
-A DOCKER-USER -p tcp -m conntrack --ctorigdstport 5003 --ctdir ORIGINAL ! -s 10.0.0.16 -j DROP
-A DOCKER-USER -j RETURN
COMMIT
"""
SITE_VALUES = {"SITE_HOST": "photos.example.com", "SIGNED_IN_USER": "admin", "APP_ADDRESS": "10.0.0.10",
               "PROXY_ADDRESS": "10.0.0.20", "DATA_PATH": "/srv/photos-data",
               "OTHER_SITES": "keep.example.com,tasks.example.com"}
LAPTOP_SITE = {"PROXMOX": "proxmox-host", "APP_CT": "4100", "PROXY_CT": "4109", **SITE_VALUES}
LAPTOP_STUB = '#!/bin/sh\necho "$(basename "$0") $*" >> "$STUB_LOG"\n'


def inner_script(function_name: str, tmp_path: Path) -> Path:
    target = tmp_path / f"{function_name}.sh"
    command = f"source <(sed '$d' {REPO / 'deploy.sh'}); {function_name} > {target}"
    subprocess.run(["bash", "-c", command], check=True)
    return target


@pytest.fixture
def app_script(tmp_path):
    return inner_script("write_app_script", tmp_path)


@pytest.fixture
def proxy_script(tmp_path):
    return inner_script("write_proxy_script", tmp_path)


def test_every_script_parses(app_script, proxy_script):
    for script in (REPO / "deploy.sh", app_script, proxy_script):
        subprocess.run(["bash", "-n", str(script)], check=True)


def test_the_container_port_is_published_on_the_lan_address_only():
    compose = (DEPLOY / "compose.yaml").read_text()
    published = re.findall(r'- "([^"]+):8080"', compose)
    assert published == ["__APP_ADDRESS__:5004"]
    assert "0.0.0.0:5004" not in compose and "[::]" not in compose


def test_the_container_runs_locked_down():
    compose = (DEPLOY / "compose.yaml").read_text()
    assert 'user: "65532:65532"' in compose
    assert "read_only: true" in compose
    assert "cap_drop: [ALL]" in compose
    assert "no-new-privileges:true" in compose
    assert "- __DATA_PATH__:/data" in compose
    assert "USER 65532:65532" in (DEPLOY / "Dockerfile").read_text()


def test_the_app_trusts_only_the_proxy_and_keeps_fifteen_gigabytes_free():
    settings = dict(line.split("=", 1) for line in (DEPLOY / "photos.env").read_text().split())
    assert settings["TRUSTED_PROXIES"] == "__PROXY_ADDRESS__"
    assert settings["ALLOWED_USERS"] == "__SIGNED_IN_USER__"
    assert settings["MIN_FREE_GB"] == "15"
    assert settings["AUTH_MODE"] == "proxy"


def test_the_caddy_block_is_admin_only_and_sets_its_own_user_header():
    block = (DEPLOY / "Caddyfile.photos").read_text()
    assert block.startswith("http://__SITE_HOST__ {")
    assert "request_header -X-Forwarded-User" in block
    assert "header_up X-Forwarded-User __SIGNED_IN_USER__" in block
    assert "uri /api/auth/users" in block
    assert "reverse_proxy __APP_ADDRESS__:__PORT__ {" in block
    assert "root * /srv/signin/photos" in block


def test_the_caddy_refusal_names_starfire_photos():
    assert 'respond "Starfire Photos is only for the admin account, so this account cannot open it. Sign in with the admin account instead, or ask the admin for what you need." 403' in (DEPLOY / "Caddyfile.photos").read_text()


def test_the_sign_in_page_is_built_as_starfire_photos():
    script = (REPO / "deploy.sh").read_text()
    assert "--first Starfire --second Photos" in script


def test_the_port_agrees_everywhere(app_script, proxy_script):
    assert "PORT=5004" in app_script.read_text()
    assert "PORT=5004" in proxy_script.read_text()


def run_rule_function(app_script: Path, tmp_path: Path, rules: Path, function: str) -> subprocess.CompletedProcess:
    stubs = tmp_path / "bin"
    stubs.mkdir(exist_ok=True)
    (stubs / "ufw").write_text("#!/bin/sh\nexit 0\n")
    (stubs / "ufw").chmod(0o755)
    body = app_script.read_text().rsplit("case \"${1:-}\" in", 1)[0]
    library = tmp_path / "functions.sh"
    library.write_text(body)
    command = (f"source {library}; UFW_RULES={rules}; RULE_CHANGE={tmp_path / 'change'}; "
               f"{function} 20260101-000000")
    return subprocess.run(["bash", "-c", command], capture_output=True, text=True,
                          env={"PATH": f"{stubs}:/usr/bin:/bin", **SITE_VALUES})


def test_the_lock_rule_goes_before_the_final_return_and_comes_out_cleanly(app_script, tmp_path):
    rules = tmp_path / "after.rules"
    rules.write_text(AFTER_RULES)
    added = run_rule_function(app_script, tmp_path, rules, "add_lock_rule")
    assert added.returncode == 0, added.stdout + added.stderr
    lines = rules.read_text().splitlines()
    drop = "-A DOCKER-USER -p tcp -m conntrack --ctorigdstport 5004 --ctdir ORIGINAL ! -s 10.0.0.20 -j DROP"
    assert lines.index(drop) < lines.index("-A DOCKER-USER -j RETURN")
    assert "--ctorigdstport 5003" in rules.read_text()
    again = run_rule_function(app_script, tmp_path, rules, "add_lock_rule")
    assert rules.read_text().count(drop) == 1, again.stdout
    (tmp_path / "change").write_text("added\n")
    removed = run_rule_function(app_script, tmp_path, rules, "remove_lock_rule")
    assert removed.returncode == 0, removed.stdout + removed.stderr
    assert rules.read_text() == AFTER_RULES


def test_the_lock_rule_is_not_guessed_when_the_anchor_is_missing(app_script, tmp_path):
    rules = tmp_path / "after.rules"
    rules.write_text(AFTER_RULES.replace("-A DOCKER-USER -j RETURN\n", ""))
    before = rules.read_text()
    result = run_rule_function(app_script, tmp_path, rules, "add_lock_rule")
    assert result.returncode != 0
    assert rules.read_text() == before


DOCKER_STUB = """#!/bin/sh
echo "$*" >> "$STUB_LOG"
case "$1" in
  ps) echo photos ;;
  inspect) echo "running healthy" ;;
  exec)
    case "$*" in
      *api/health*) echo '{"ok":true}' ;;
      */data/.photos-backup.db*) echo copy > "$DATA/.photos-backup.db" ;;
    esac ;;
esac
"""
IPTABLES_STUB = """#!/bin/sh
echo "-N DOCKER-USER"
echo "-A DOCKER-USER ! -s 10.0.0.20/32 -p tcp -m conntrack --ctorigdstport 5004 --ctdir ORIGINAL -j DROP"
echo "-A DOCKER-USER -j RETURN"
"""
CURL_STUB = """#!/bin/sh
all="$*"
case "$all" in
  *api/session*)
    case "$all" in
      *X-Forwarded-User*) echo "${SESSION_WITH_USER:-200}" ;;
      *) echo "${SESSION_WITHOUT_USER:-401}" ;;
    esac ;;
  *signin/*) echo 200 ;;
  *api/health*) echo '{"ok":true}' ;;
  *) echo "303 https://example.invalid/signin/" ;;
esac
"""


def stub_folder(tmp_path: Path, **stubs: str) -> Path:
    folder = tmp_path / "bin"
    folder.mkdir(exist_ok=True)
    for name, body in stubs.items():
        (folder / name).write_text(body)
        (folder / name).chmod(0o755)
    return folder


def run_app_function(app_script: Path, tmp_path: Path, command: str, **env: str) -> subprocess.CompletedProcess:
    stubs = stub_folder(tmp_path, docker=DOCKER_STUB, iptables=IPTABLES_STUB, ufw="#!/bin/sh\nexit 0\n")
    library = tmp_path / "functions.sh"
    library.write_text(app_script.read_text().rsplit("case \"${1:-}\" in", 1)[0])
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    setup = f"source {library}; DATA={data}; STATE={tmp_path}; BACKUP_POINTER={tmp_path / 'pointer'}; "
    return subprocess.run(["bash", "-c", setup + command], capture_output=True, text=True,
                          env={"PATH": f"{stubs}:/usr/bin:/bin", "STUB_LOG": str(tmp_path / "docker.log"),
                               "DATA": str(data), **SITE_VALUES, **env})


def test_the_drop_rule_is_found_in_the_order_iptables_prints_it(app_script, tmp_path):
    result = run_app_function(app_script, tmp_path, "do_check")
    assert "drop rule for port 5004 live: 1" in result.stdout, result.stdout + result.stderr
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_drop_rule_check_still_fails_when_the_rule_is_missing(app_script, tmp_path):
    result = run_app_function(app_script, tmp_path, "iptables() { :; }; do_check")
    assert "live: 0" in result.stdout
    assert result.returncode != 0


def test_the_database_copy_goes_through_the_data_folder_not_tmpfs(app_script, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    result = run_app_function(app_script, tmp_path, f"copy_database_into {work}")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (work / "photos.db").read_text() == "copy\n"
    assert not (tmp_path / "data" / ".photos-backup.db").exists()
    log = (tmp_path / "docker.log").read_text()
    assert "/tmp/" not in log
    assert not any(line.startswith("cp ") for line in log.splitlines())


def test_apply_needs_a_backup_from_the_last_hour(app_script, tmp_path):
    backup = tmp_path / "photos-before.tgz"
    backup.write_text("backup")
    (tmp_path / "pointer").write_text(f"{backup}\n")
    assert run_app_function(app_script, tmp_path, "backup_is_fresh").returncode == 0
    two_hours_ago = backup.stat().st_mtime - 2 * 3600
    os.utime(backup, (two_hours_ago, two_hours_ago))
    assert run_app_function(app_script, tmp_path, "backup_is_fresh").returncode != 0
    (tmp_path / "pointer").write_text("")
    assert run_app_function(app_script, tmp_path, "backup_is_fresh").returncode != 0


def function_body(script: str, name: str) -> str:
    start = script.index(f"\n{name}() {{")
    return script[start:script.index("\n}\n", start)]


def test_the_port_is_locked_before_the_container_is_published(app_script):
    apply = function_body(app_script.read_text(), "do_apply")
    assert "backup_is_fresh" in apply
    assert apply.index("add_lock_rule") < apply.index("compose up")
    assert apply.index("add_lock_rule") < apply.index("wait_for_health")


def run_proxy_check(proxy_script: Path, tmp_path: Path, **env: str) -> subprocess.CompletedProcess:
    stubs = stub_folder(tmp_path, curl=CURL_STUB)
    return subprocess.run(["bash", str(proxy_script), "check"], capture_output=True, text=True,
                          env={"PATH": f"{stubs}:/usr/bin:/bin", **SITE_VALUES, **env})


def test_the_proxy_check_proves_a_signed_in_request_reaches_the_app(proxy_script, tmp_path):
    passing = run_proxy_check(proxy_script, tmp_path)
    assert passing.returncode == 0, passing.stdout + passing.stderr
    assert "signed in -> 200" in passing.stdout and "no user -> 401" in passing.stdout
    assert run_proxy_check(proxy_script, tmp_path, SESSION_WITH_USER="401").returncode != 0
    assert run_proxy_check(proxy_script, tmp_path, SESSION_WITHOUT_USER="200").returncode != 0


def test_the_app_only_answers_its_own_host_name():
    settings = dict(line.split("=", 1) for line in (DEPLOY / "photos.env").read_text().split())
    assert settings["ALLOWED_HOSTS"] == "__SITE_HOST__"


APP_BASELINE_BYTES = 256 * 1024 * 1024


def test_the_memory_cap_holds_the_largest_image_the_app_accepts(tmp_path):
    from memory_probe import bytes_per_pixel
    compose = (DEPLOY / "compose.yaml").read_text()
    limit = re.search(r"mem_limit: (\d+)([mg])", compose)
    limit_bytes = int(limit.group(1)) * (1024 ** 3 if limit.group(2) == "g" else 1024 ** 2)
    settings = dict(line.split("=", 1) for line in (DEPLOY / "photos.env").read_text().split())
    largest_pixels = int(settings["MAX_MEGAPIXELS"]) * 1_000_000
    needed = largest_pixels * bytes_per_pixel(tmp_path, "WEBP") + APP_BASELINE_BYTES
    assert needed < limit_bytes, f"needs {needed / 2 ** 20:.0f} MiB, cap {limit_bytes / 2 ** 20:.0f} MiB"


CADDYFILE_BEFORE = """{
    admin off
}

http://keep.example.com {
    reverse_proxy 10.0.0.10:5001
}
"""


def run_proxy_function(proxy_script: Path, tmp_path: Path, command: str, **stubs: str) -> subprocess.CompletedProcess:
    stubs = stub_folder(tmp_path, **{"caddy": "#!/bin/sh\nexit 0\n", "systemctl": "#!/bin/sh\nexit 0\n", **stubs})
    library = tmp_path / "proxy-functions.sh"
    library.write_text(proxy_script.read_text().rsplit("case \"${1:-}\" in", 1)[0])
    setup = (f"source {library}; CADDYFILE={tmp_path / 'Caddyfile'}; CADDY_CHANGE={tmp_path / 'caddy-change'}; ")
    return subprocess.run(["bash", "-c", setup + command], capture_output=True, text=True,
                          env={"PATH": f"{stubs}:/usr/bin:/bin", "STUB_LOG": str(tmp_path / "caddy.log"), **SITE_VALUES})


def test_undo_puts_the_caddyfile_back_byte_for_byte_and_the_pushed_block_is_not_left_behind(proxy_script, tmp_path):
    caddyfile = tmp_path / "Caddyfile"
    caddyfile.write_text(CADDYFILE_BEFORE)
    block = tmp_path / "photos-caddy-block"
    block.write_text((DEPLOY / "Caddyfile.photos").read_text())
    added = run_proxy_function(proxy_script, tmp_path, f"add_site {block}")
    assert added.returncode == 0, added.stdout + added.stderr
    assert "reverse_proxy 10.0.0.10:5004 {" in caddyfile.read_text()
    assert not block.exists()
    removed = run_proxy_function(proxy_script, tmp_path, "remove_site")
    assert removed.returncode == 0, removed.stdout + removed.stderr
    assert caddyfile.read_text() == CADDYFILE_BEFORE


def test_the_site_values_are_filled_in_where_they_are_used(app_script, proxy_script, tmp_path):
    caddyfile = tmp_path / "Caddyfile"
    caddyfile.write_text(CADDYFILE_BEFORE)
    block = tmp_path / "photos-caddy-block"
    block.write_text((DEPLOY / "Caddyfile.photos").read_text())
    assert run_proxy_function(proxy_script, tmp_path, f"add_site {block}").returncode == 0
    added = caddyfile.read_text()
    assert "http://photos.example.com {" in added and "header_up X-Forwarded-User admin" in added
    env = run_app_function(app_script, tmp_path, f"fill_in_site_values {DEPLOY / 'photos.env'}").stdout
    assert "TRUSTED_PROXIES=10.0.0.20\n" in env and "ALLOWED_USERS=admin\n" in env
    assert "ALLOWED_HOSTS=photos.example.com\n" in env and "__" not in env


def test_the_installed_compose_file_names_the_data_path_and_the_lan_address(app_script, tmp_path):
    tree = tmp_path / "tree"
    (tree / "deploy").mkdir(parents=True)
    (tree / "deploy" / "compose.yaml").write_text((DEPLOY / "compose.yaml").read_text())
    bundle = tmp_path / "code.tgz"
    subprocess.run(["tar", "-czf", str(bundle), "-C", str(tree), "."], check=True)
    stub_folder(tmp_path, chown="#!/bin/sh\nexit 0\n")
    app = tmp_path / "opt" / "photos"
    app.parent.mkdir()
    command = f"APP={app}; BEFORE_POINTER={tmp_path / 'before'}; install_code {bundle} 20260101-000000"
    result = run_app_function(app_script, tmp_path, command)
    assert result.returncode == 0, result.stdout + result.stderr
    compose = (app / "deploy" / "compose.yaml").read_text()
    assert f"- {tmp_path / 'data'}:/data\n" in compose
    assert '- "10.0.0.10:5004:8080"\n' in compose
    assert "__" not in compose


def test_the_app_container_keeps_the_photos_in_data_path(app_script):
    library = app_script.with_name("app-library.sh")
    library.write_text(app_script.read_text().rsplit("case \"${1:-}\" in", 1)[0])
    result = subprocess.run(["bash", "-c", f"source {library}; echo $DATA"], capture_output=True, text=True,
                            env={"PATH": "/usr/bin:/bin", **SITE_VALUES})
    assert result.stdout.strip() == "/srv/photos-data"


def run_laptop_side(tmp_path, site_text):
    stubs = tmp_path / "laptop-stubs"
    stubs.mkdir()
    for name in ("ssh", "scp"):
        (stubs / name).write_text(LAPTOP_STUB)
        (stubs / name).chmod(0o755)
    log = tmp_path / "laptop.log"
    log.touch()
    site = tmp_path / "site.env"
    if site_text is not None:
        site.write_text(site_text)
    environment = {name: value for name, value in os.environ.items() if name not in LAPTOP_SITE}
    environment.update(PATH=f"{stubs}:{os.environ['PATH']}", STUB_LOG=str(log), SITE_ENV=str(site))
    result = subprocess.run(["bash", str(REPO / "deploy.sh"), "check"], capture_output=True, text=True,
                            env=environment, timeout=60)
    return result, log.read_text()


def test_the_laptop_side_refuses_without_the_site_file(tmp_path):
    result, calls = run_laptop_side(tmp_path, None)
    assert result.returncode == 1
    assert "deploy/site.env.example" in result.stdout
    assert calls == ""


def test_the_laptop_side_passes_the_site_values_to_both_containers(tmp_path):
    site_text = "".join(f"{name}={value}\n" for name, value in LAPTOP_SITE.items())
    result, calls = run_laptop_side(tmp_path, site_text)
    assert result.returncode == 0, result.stdout + result.stderr
    values = ("env SITE_HOST=photos.example.com SIGNED_IN_USER=admin APP_ADDRESS=10.0.0.10 PROXY_ADDRESS=10.0.0.20 "
              "DATA_PATH=/srv/photos-data OTHER_SITES=keep.example.com,tasks.example.com")
    assert f"ssh -o BatchMode=yes proxmox-host pct exec 4100 -- {values} bash /root/photos-deploy-app.sh check" in calls
    assert f"ssh -o BatchMode=yes proxmox-host pct exec 4109 -- {values} bash /root/photos-deploy-proxy.sh check" in calls


@pytest.mark.parametrize("name, value", [("SITE_HOST", "photos.example.com;reboot"), ("APP_CT", ""),
                                         ("DATA_PATH", "relative/path"), ("DATA_PATH", "/srv/a b"),
                                         ("OTHER_SITES", "keep.example.com tasks.example.com")])
def test_the_laptop_side_refuses_a_site_value_that_is_not_plain(tmp_path, name, value):
    site_text = "".join(f"{key}={value if key == name else default}\n" for key, default in LAPTOP_SITE.items())
    result, calls = run_laptop_side(tmp_path, site_text)
    assert result.returncode == 1
    assert f"{name} in" in result.stdout
    assert calls == ""


LOGGING_CADDY = '#!/bin/sh\necho "caddy $*" >> "$STUB_LOG"\nif [ "$1" = validate ] && grep -q BROKEN "$3"; then exit 1; fi\n'
LOGGING_SYSTEMCTL = '#!/bin/sh\necho "systemctl $*" >> "$STUB_LOG"\n'
OLD_REFUSAL = 'respond "Photo Library is for the admin account only." 403'
CURRENT_REFUSAL = 'respond "Starfire Photos is only for the admin account, so this account cannot open it. Sign in with the admin account instead, or ask the admin for what you need." 403'


def add_logged_site(proxy_script: Path, tmp_path: Path, template: str) -> subprocess.CompletedProcess:
    block = tmp_path / "photos-caddy-block"
    block.write_text(template)
    (tmp_path / "caddy.log").write_text("")
    return run_proxy_function(proxy_script, tmp_path, f"add_site {block}", caddy=LOGGING_CADDY,
                              systemctl=LOGGING_SYSTEMCTL)


def with_old_refusal_in_the_marked_block(caddyfile: Path) -> str:
    current = caddyfile.read_text()
    marked = current.index("# >>> photos block")
    caddyfile.write_text(current[:marked] + current[marked:].replace(CURRENT_REFUSAL, OLD_REFUSAL))
    return current


def test_an_existing_block_gets_the_current_refusal_and_nothing_else_changes(proxy_script, tmp_path):
    caddyfile = tmp_path / "Caddyfile"
    caddyfile.write_text(CADDYFILE_BEFORE + f"\nhttp://other.example.com {{\n    {OLD_REFUSAL}\n}}\n")
    template = (DEPLOY / "Caddyfile.photos").read_text()
    assert add_logged_site(proxy_script, tmp_path, template).returncode == 0
    current = with_old_refusal_in_the_marked_block(caddyfile)
    updated = add_logged_site(proxy_script, tmp_path, template)
    assert updated.returncode == 0, updated.stdout + updated.stderr
    assert caddyfile.read_text() == current
    assert caddyfile.read_text().count(OLD_REFUSAL) == 1
    assert "systemctl reload caddy" in (tmp_path / "caddy.log").read_text()
    assert any(OLD_REFUSAL in backup.read_text()[backup.read_text().index("# >>> photos block"):]
               for backup in tmp_path.glob("Caddyfile.bak-photos-*"))
    assert not (tmp_path / "caddy-change").exists()
    assert not (tmp_path / "photos-caddy-block").exists()
    again = add_logged_site(proxy_script, tmp_path, template)
    assert again.returncode == 0, again.stdout + again.stderr
    assert caddyfile.read_text() == current
    assert "systemctl reload caddy" not in (tmp_path / "caddy.log").read_text()


def test_a_refusal_that_does_not_validate_leaves_the_existing_block_alone(proxy_script, tmp_path):
    caddyfile = tmp_path / "Caddyfile"
    caddyfile.write_text(CADDYFILE_BEFORE)
    template = (DEPLOY / "Caddyfile.photos").read_text()
    assert add_logged_site(proxy_script, tmp_path, template).returncode == 0
    before = caddyfile.read_text()
    broken = add_logged_site(proxy_script, tmp_path, template.replace("Starfire Photos is", "BROKEN Photos is"))
    assert broken.returncode != 0
    assert caddyfile.read_text() == before


def test_the_caddy_refusal_says_who_the_app_is_for_and_what_to_do_instead():
    refusal = re.search(r'respond "([^"]+)" 403', (DEPLOY / "Caddyfile.photos").read_text()).group(1)
    assert refusal.startswith("Starfire Photos is only for the admin account")
    assert "Sign in with the admin account instead" in refusal
    assert "ask the admin" in refusal
    assert len(refusal) < 200

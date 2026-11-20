#!/usr/bin/env bash
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SITE_ENV="${SITE_ENV:-$REPO/deploy/site.env}"
SITE_NAMES=(PROXMOX APP_CT PROXY_CT SITE_HOST SIGNED_IN_USER APP_ADDRESS PROXY_ADDRESS DATA_PATH OTHER_SITES)
CONTAINER_SIDE_NAMES=(SITE_HOST SIGNED_IN_USER APP_ADDRESS PROXY_ADDRESS DATA_PATH OTHER_SITES)
PLAIN_VALUE_PATTERN='^[A-Za-z0-9][A-Za-z0-9._:-]*$'
PATH_VALUE_PATTERN='^/[A-Za-z0-9._/-]+$'
LIST_VALUE_PATTERN='^[A-Za-z0-9.,-]*$'
SITE_SIGNIN="${SITE_SIGNIN:-$HOME/Projects/site-signin}"
APP_SCRIPT=/root/photos-deploy-app.sh
PROXY_SCRIPT=/root/photos-deploy-proxy.sh

usage() {
  cat <<'USAGE'
usage: deploy.sh backup | apply [commit] | check | undo

Runs from the laptop and reaches the server through ssh to the Proxmox host and
`pct`. Every value for your own setup comes from deploy/site.env (copy
deploy/site.env.example).

The app runs in Docker in the app container (APP_CT). Its port,
APP_ADDRESS:5004, is dropped for everyone except the proxy container
(PROXY_ADDRESS) by a marked DOCKER-USER rule in /etc/ufw/after.rules. The proxy
container's Caddy (PROXY_CT) serves SITE_HOST behind the starfire admin login
and passes requests to that port.

  backup          app container: /opt/photos, /etc/photos, /etc/ufw/after.rules and a
                  copy of the database into /root/photos-before-<stamp>.tgz (mode 600);
                  proxy container: the Caddyfile and /srv/signin/photos into the same name.
                  Photos and videos themselves are not copied (too large).
  apply [commit]  needs a backup from the last hour on both containers, then installs
                  or updates from the commit (default HEAD): code in /opt/photos,
                  env in /etc/photos/photos.env (kept if present), data folder
                  DATA_PATH, the ufw lock, docker compose build and up, then the
                  sign-in page and the marked Caddy block in the proxy container
  check           container health, the lock rule, sign-in redirects through Caddy,
                  and that the proxy container reaches the app signed in (200) and
                  not without a user (401)
  undo            put back what the last apply changed on both containers; the
                  photos in DATA_PATH are never touched
USAGE
}

site_value() {
  sed -n "s/^$1=//p" "$SITE_ENV" | tail -n 1
}

site_pattern() {
  case "$1" in
    DATA_PATH) echo "$PATH_VALUE_PATTERN" ;;
    OTHER_SITES) echo "$LIST_VALUE_PATTERN" ;;
    *) echo "$PLAIN_VALUE_PATTERN" ;;
  esac
}

load_site() {
  local name value pattern
  if [ ! -f "$SITE_ENV" ]; then
    echo "refusing: $SITE_ENV is missing; copy deploy/site.env.example to deploy/site.env and fill it in"
    exit 1
  fi
  for name in "${SITE_NAMES[@]}"; do
    value="${!name:-$(site_value "$name")}"
    pattern="$(site_pattern "$name")"
    if [[ ! "$value" =~ $pattern ]]; then
      echo "refusing: $name in $SITE_ENV is not a plain host name, address, number, path or list, got '$value'"
      exit 1
    fi
    printf -v "$name" '%s' "$value"
  done
}

container_side_env() {
  local name
  printf 'env'
  for name in "${CONTAINER_SIDE_NAMES[@]}"; do
    printf ' %s=%s' "$name" "${!name}"
  done
}

write_app_script() {
  cat <<'APP_SIDE'
#!/usr/bin/env bash
set -euo pipefail
APP=/opt/photos
CONF=/etc/photos
ENV_FILE="$CONF/photos.env"
DATA="${DATA_PATH:?DATA_PATH is missing: deploy.sh passes it from deploy/site.env}"
COMPOSE="$APP/deploy/compose.yaml"
PROJECT=photos
CONTAINER=photos
CONTAINER_USER=65532
PORT=5004
PROXY_ADDRESS="${PROXY_ADDRESS:?PROXY_ADDRESS is missing: deploy.sh passes it from deploy/site.env}"
APP_ADDRESS="${APP_ADDRESS:?APP_ADDRESS is missing: deploy.sh passes it from deploy/site.env}"
SITE_HOST="${SITE_HOST:?SITE_HOST is missing: deploy.sh passes it from deploy/site.env}"
SIGNED_IN_USER="${SIGNED_IN_USER:?SIGNED_IN_USER is missing: deploy.sh passes it from deploy/site.env}"
UFW_RULES=/etc/ufw/after.rules
RULE_BEGIN="# >>> photos block, added and removed by the photos deploy.sh"
RULE_END="# <<< photos block"
RULE_ANCHOR="-A DOCKER-USER -j RETURN"
STATE=/root
BACKUP_POINTER="$STATE/photos-last-backup"
BEFORE_POINTER="$STATE/photos-code-before"
RULE_CHANGE="$STATE/photos-ufw-change"
KEPT_COPIES=3
FRESH_BACKUP_MINUTES=60

compose() {
  docker compose -p "$PROJECT" -f "$COMPOSE" "$@"
}

fill_in_site_values() {
  sed -e "s/__SITE_HOST__/$SITE_HOST/g" -e "s/__SIGNED_IN_USER__/$SIGNED_IN_USER/g" \
    -e "s/__APP_ADDRESS__/$APP_ADDRESS/g" -e "s/__PROXY_ADDRESS__/$PROXY_ADDRESS/g" -e "s|__DATA_PATH__|$DATA|g" "$@"
}

health() {
  docker exec "$CONTAINER" python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=4).read().decode())" 2>/dev/null || true
}

wait_for_health() {
  for _ in $(seq 1 60); do
    case "$(health)" in *'"ok":true'*) return 0 ;; esac
    sleep 2
  done
  echo "Photos did not answer /api/health within 120 s"
  return 1
}

copy_database_into() {
  local target="$1" copy="$DATA/.photos-backup.db"
  rm -f "$copy"
  docker exec "$CONTAINER" python -c "import sqlite3; s = sqlite3.connect('file:/data/photos.db?mode=ro', uri=True); c = sqlite3.connect('/data/.photos-backup.db'); s.backup(c); c.close(); s.close()"
  mv "$copy" "$target/photos.db"
}

backup_is_fresh() {
  local backup
  backup="$(cat "$BACKUP_POINTER" 2>/dev/null || true)"
  [ -n "$backup" ] && [ -s "$backup" ] && [ -n "$(find "$backup" -mmin "-$FRESH_BACKUP_MINUTES")" ]
}

do_backup() {
  local stamp backup work parts=()
  stamp="$(date +%Y%m%d-%H%M%S)"
  backup="$STATE/photos-before-$stamp.tgz"
  work="$(mktemp -d)"
  if docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
    copy_database_into "$work"
  fi
  for path in opt/photos etc/photos etc/ufw/after.rules; do
    if [ -e "/$path" ]; then parts+=("$path"); fi
  done
  ( umask 077 && tar -czf "$backup" -C / "${parts[@]}" -C "$work" . )
  rm -rf "$work"
  chmod 600 "$backup"
  echo "$backup" > "$BACKUP_POINTER"
  echo "backup in the app container: $backup ($(du -h "$backup" | cut -f1))"
}

port_is_taken_by_another_program() {
  local listeners
  listeners="$(ss -ltnp "sport = :$PORT" | grep LISTEN || true)"
  [ -n "$listeners" ] || return 1
  ! docker ps --format '{{.Names}} {{.Ports}}' | grep -q "^$CONTAINER .*:$PORT->"
}

install_code() {
  local bundle="$1" stamp="$2" fresh="$APP-new-$2"
  mkdir "$fresh"
  tar -xzf "$bundle" -C "$fresh" --no-same-owner
  rm -f "$bundle"
  fill_in_site_values "$fresh/deploy/compose.yaml" > "$fresh/deploy/compose.yaml.filled"
  mv "$fresh/deploy/compose.yaml.filled" "$fresh/deploy/compose.yaml"
  chown -R root:root "$fresh"
  chmod -R u=rwX,go=rX "$fresh"
  if [ -d "$APP" ]; then
    mv "$APP" "$APP-before-$stamp"
    echo "$APP-before-$stamp" > "$BEFORE_POINTER"
  else
    echo none > "$BEFORE_POINTER"
  fi
  mv "$fresh" "$APP"
}

install_env_and_data() {
  install -d -m 750 "$CONF"
  if [ -f "$ENV_FILE" ]; then
    echo "env: $ENV_FILE kept as it is"
  else
    install -m 640 /dev/null "$ENV_FILE"
    fill_in_site_values "$APP/deploy/photos.env" > "$ENV_FILE"
    echo "env: wrote $ENV_FILE"
  fi
  install -d -m 700 -o "$CONTAINER_USER" -g "$CONTAINER_USER" "$DATA"
}

render_rule_block() {
  printf '%s\n' "$RULE_BEGIN"
  printf '%s\n' "-A DOCKER-USER -p tcp -m conntrack --ctorigdstport $PORT --ctdir ORIGINAL ! -s $PROXY_ADDRESS -j DROP"
  printf '%s\n' "$RULE_END"
}

rule_block_count() {
  grep -cxF -- "$RULE_BEGIN" "$UFW_RULES" || true
}

add_lock_rule() {
  local stamp="$1" work
  rm -f "$RULE_CHANGE"
  if [ "$(rule_block_count)" != 0 ]; then
    echo "ufw: the marked photos rule is already in $UFW_RULES"
    return 0
  fi
  if [ "$(grep -cxF -- "$RULE_ANCHOR" "$UFW_RULES" || true)" != 1 ]; then
    echo "ufw: $UFW_RULES does not have exactly one '$RULE_ANCHOR' line; add the rule by hand"
    return 1
  fi
  cp -a "$UFW_RULES" "$UFW_RULES.bak-photos-$stamp"
  work="$(mktemp)"
  render_rule_block > "$work.block"
  awk -v anchor="$RULE_ANCHOR" -v block="$work.block" '
    $0 == anchor { while ((getline line < block) > 0) print line }
    { print }
  ' "$UFW_RULES" > "$work"
  cat "$work" > "$UFW_RULES"
  rm -f "$work" "$work.block"
  if ! ufw reload > /dev/null; then
    cat "$UFW_RULES.bak-photos-$stamp" > "$UFW_RULES"
    ufw reload > /dev/null || true
    echo "ufw: the reload failed, so $UFW_RULES was put back"
    return 1
  fi
  echo added > "$RULE_CHANGE"
  echo "ufw: port $PORT now only answers $PROXY_ADDRESS (earlier file: $UFW_RULES.bak-photos-$stamp)"
}

remove_lock_rule() {
  local stamp work
  [ "$(cat "$RULE_CHANGE" 2>/dev/null || true)" = added ] || return 0
  stamp="$(date +%Y%m%d-%H%M%S)"
  cp -a "$UFW_RULES" "$UFW_RULES.bak-photos-undo-$stamp"
  work="$(mktemp)"
  awk -v begin="$RULE_BEGIN" -v end="$RULE_END" '
    $0 == begin { inside = 1; next }
    inside { if ($0 == end) inside = 0; next }
    { print }
  ' "$UFW_RULES" > "$work"
  cat "$work" > "$UFW_RULES"
  rm -f "$work" "$RULE_CHANGE"
  ufw reload > /dev/null
  echo "ufw: removed the photos rule (earlier file: $UFW_RULES.bak-photos-undo-$stamp)"
}

keep_newest_copies() {
  local pattern="$1" copies surplus index
  shopt -s nullglob
  copies=($pattern)
  shopt -u nullglob
  surplus=$(( ${#copies[@]} - KEPT_COPIES ))
  for (( index = 0; index < surplus; index++ )); do
    rm -rf -- "${copies[index]}"
  done
}

live_drop_rules() {
  iptables -S DOCKER-USER | awk -v port="--ctorigdstport $PORT " -v source="! -s $PROXY_ADDRESS/32 " '
    index($0 " ", port) && index($0 " ", source) && $NF == "DROP" { count++ }
    END { print count + 0 }
  '
}

do_check() {
  local failures=0 state answer rule
  state="$(docker inspect -f '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' "$CONTAINER" 2>/dev/null || echo missing)"
  answer="$(health)"
  rule="$(live_drop_rules)"
  echo "container: $state"
  echo "health: ${answer:-no answer}"
  echo "DOCKER-USER drop rule for port $PORT live: $rule"
  echo "data folder: $(stat -c '%a %u:%g' "$DATA")"
  echo "disk: $(df -h --output=avail "$DATA" | tail -n 1 | tr -d ' ') free on $DATA"
  case "$state" in "running healthy"|"running starting") ;; *) failures=$((failures + 1)) ;; esac
  case "$answer" in *'"ok":true'*) ;; *) failures=$((failures + 1)) ;; esac
  [ "$rule" -ge 1 ] || failures=$((failures + 1))
  echo "check failures in the app container: $failures"
  [ "$failures" = 0 ]
}

do_apply() {
  local bundle="$1" stamp
  backup_is_fresh || { echo "refusing: run backup first (none found from the last $FRESH_BACKUP_MINUTES minutes)"; exit 1; }
  if port_is_taken_by_another_program; then
    echo "refusing: something other than the photos container listens on port $PORT"
    exit 1
  fi
  stamp="$(date +%Y%m%d-%H%M%S)"
  install_code "$bundle" "$stamp"
  install_env_and_data
  add_lock_rule "$stamp"
  compose up -d --build
  wait_for_health
  keep_newest_copies "$APP-before-*"
  keep_newest_copies "$UFW_RULES.bak-photos-*"
  do_check
}

do_undo() {
  local before stamp
  before="$(cat "$BEFORE_POINTER" 2>/dev/null || true)"
  if [ -z "$before" ]; then
    echo "nothing to undo in the app container"
    return 0
  fi
  stamp="$(date +%Y%m%d-%H%M%S)"
  if [ "$before" = none ]; then
    compose down
    remove_lock_rule
    mv "$APP" "$APP-undone-$stamp"
    echo "container removed, code kept in $APP-undone-$stamp, photos left in $DATA"
  else
    mv "$APP" "$APP-undone-$stamp"
    mv "$before" "$APP"
    compose up -d --build
    wait_for_health
    echo "earlier code back in place, undone code kept in $APP-undone-$stamp"
  fi
  rm -f "$BEFORE_POINTER"
}

case "${1:-}" in
  backup) do_backup ;;
  fresh) backup_is_fresh || { echo "app container: no backup from the last $FRESH_BACKUP_MINUTES minutes; run backup first"; exit 1; } ;;
  apply) do_apply "${2:?apply needs the code bundle}" ;;
  check) do_check ;;
  undo) do_undo ;;
  *) echo "unknown action"; exit 2 ;;
esac
APP_SIDE
}

write_proxy_script() {
  cat <<'PROXY_SIDE'
#!/usr/bin/env bash
set -euo pipefail
SIGNIN=/srv/signin/photos
CADDYFILE=/etc/caddy/Caddyfile
LOCK=/root/caddy-edit.lock
STATE=/root
SITE_HOST="${SITE_HOST:?SITE_HOST is missing: deploy.sh passes it from deploy/site.env}"
APP_ADDRESS="${APP_ADDRESS:?APP_ADDRESS is missing: deploy.sh passes it from deploy/site.env}"
PROXY_ADDRESS="${PROXY_ADDRESS:?PROXY_ADDRESS is missing: deploy.sh passes it from deploy/site.env}"
PORT=5004
SIGNED_IN_USER="${SIGNED_IN_USER:?SIGNED_IN_USER is missing: deploy.sh passes it from deploy/site.env}"
BACKUP_POINTER="$STATE/photos-last-backup"
FRESH_BACKUP_MINUTES=60
BLOCK_BEGIN="# >>> photos block, added and removed by the photos deploy.sh"
BLOCK_END="# <<< photos block"
CADDY_CHANGE="$STATE/photos-caddy-change"
SIGNIN_BEFORE="$STATE/photos-signin-before"
OTHER_SITES="${OTHER_SITES-}"

fill_in_site_values() {
  sed -e "s/__SITE_HOST__/$SITE_HOST/g" -e "s/__SIGNED_IN_USER__/$SIGNED_IN_USER/g" \
    -e "s/__APP_ADDRESS__/$APP_ADDRESS/g" -e "s/__PROXY_ADDRESS__/$PROXY_ADDRESS/g" "$@"
}

take_lock() {
  for _ in $(seq 1 120); do
    if mkdir "$LOCK" 2> /dev/null; then
      trap 'rmdir "$LOCK" 2> /dev/null || true' EXIT
      return 0
    fi
    sleep 1
  done
  echo "refusing: $LOCK has been held for two minutes; another Caddy edit is running"
  exit 1
}

caddy_answer() {
  curl -s -o /dev/null -w '%{http_code} %{redirect_url}' -H "Host: $1" "${@:2}" http://127.0.0.1/
}

do_backup() {
  local backup parts=(etc/caddy/Caddyfile)
  backup="$STATE/photos-before-$(date +%Y%m%d-%H%M%S).tgz"
  if [ -d "$SIGNIN" ]; then parts+=(srv/signin/photos); fi
  ( umask 077 && tar -czf "$backup" -C / "${parts[@]}" )
  echo "$backup" > "$BACKUP_POINTER"
  echo "backup in the proxy container: $backup"
}

backup_is_fresh() {
  local backup
  backup="$(cat "$BACKUP_POINTER" 2>/dev/null || true)"
  [ -n "$backup" ] && [ -s "$backup" ] && [ -n "$(find "$backup" -mmin "-$FRESH_BACKUP_MINUTES")" ]
}

app_session_status() {
  curl -s -o /dev/null -w '%{http_code}' -m 5 -H "Host: $SITE_HOST" "$@" "http://$APP_ADDRESS:$PORT/api/session"
}

block_count() {
  grep -cxF -- "$BLOCK_BEGIN" "$CADDYFILE" || true
}

reload_or_restore() {
  local backup="$1"
  if caddy validate --config "$CADDYFILE" --adapter caddyfile > /dev/null 2>&1 && systemctl reload caddy; then
    return 0
  fi
  cat "$backup" > "$CADDYFILE"
  systemctl reload caddy || true
  echo "caddy: the new Caddyfile did not validate or reload, so $backup was put back"
  return 1
}

add_site() {
  local block_file="$1" stamp backup block
  rm -f "$CADDY_CHANGE"
  block="$(sed "s/__PORT__/$PORT/g" "$block_file" | fill_in_site_values)"
  rm -f "$block_file"
  if [ "$(block_count)" != 0 ] || grep -qxF "http://$SITE_HOST {" "$CADDYFILE"; then
    echo "caddy: a $SITE_HOST block is already present, left as is"
    return 0
  fi
  stamp="$(date +%Y%m%d-%H%M%S)"
  backup="$CADDYFILE.bak-photos-$stamp"
  cp -a "$CADDYFILE" "$backup"
  printf '\n%s\n%s\n%s\n' "$BLOCK_BEGIN" "$block" "$BLOCK_END" >> "$CADDYFILE"
  reload_or_restore "$backup"
  echo added > "$CADDY_CHANGE"
  echo "caddy: added the $SITE_HOST block (earlier file: $backup)"
}

remove_site() {
  local stamp backup work
  [ "$(cat "$CADDY_CHANGE" 2>/dev/null || true)" = added ] || return 0
  stamp="$(date +%Y%m%d-%H%M%S)"
  backup="$CADDYFILE.bak-photos-undo-$stamp"
  cp -a "$CADDYFILE" "$backup"
  work="$(mktemp)"
  awk -v begin="$BLOCK_BEGIN" -v end="$BLOCK_END" '
    inside { if ($0 == end) inside = 0; next }
    $0 == begin { inside = 1; blank_held = 0; next }
    blank_held { print ""; blank_held = 0 }
    $0 == "" { blank_held = 1; next }
    { print }
    END { if (blank_held) print "" }
  ' "$CADDYFILE" > "$work"
  cat "$work" > "$CADDYFILE"
  rm -f "$work"
  reload_or_restore "$backup"
  rm -f "$CADDY_CHANGE"
  echo "caddy: removed the $SITE_HOST block (earlier file: $backup)"
}

install_signin() {
  local bundle="$1" stamp
  stamp="$(date +%Y%m%d-%H%M%S)"
  if [ -d "$SIGNIN" ]; then
    mv "$SIGNIN" "$SIGNIN-before-$stamp"
    echo "$SIGNIN-before-$stamp" > "$SIGNIN_BEFORE"
  else
    echo none > "$SIGNIN_BEFORE"
  fi
  mkdir -p "$SIGNIN"
  tar -xzf "$bundle" -C "$SIGNIN" --no-same-owner
  rm -f "$bundle"
  chown -R root:caddy "$SIGNIN"
  chmod -R u=rwX,g=rX,o= "$SIGNIN"
}

undo_signin() {
  local before
  before="$(cat "$SIGNIN_BEFORE" 2>/dev/null || true)"
  [ -n "$before" ] || return 0
  rm -rf "$SIGNIN"
  if [ "$before" != none ] && [ -d "$before" ]; then mv "$before" "$SIGNIN"; fi
  rm -f "$SIGNIN_BEFORE"
}

do_check() {
  local failures=0 plain dummy signin site answer signed_in anonymous
  plain="$(caddy_answer "$SITE_HOST")"
  dummy="$(caddy_answer "$SITE_HOST" -H 'Cookie: starfire_session=not-a-session')"
  signin="$(curl -s -o /dev/null -w '%{http_code}' -H "Host: $SITE_HOST" http://127.0.0.1/signin/)"
  echo "through Caddy: no cookie -> $plain, made-up cookie -> $dummy, /signin/ -> $signin"
  echo "app health from the proxy container: $(curl -s -m 5 "http://$APP_ADDRESS:$PORT/api/health" || echo none)"
  signed_in="$(app_session_status -H "X-Forwarded-User: $SIGNED_IN_USER")"
  anonymous="$(app_session_status)"
  echo "app session from the proxy container: signed in -> $signed_in, no user -> $anonymous"
  [ "$signed_in" = 200 ] || failures=$((failures + 1))
  [ "$anonymous" = 401 ] || failures=$((failures + 1))
  for answer in "$plain" "$dummy"; do
    case "$answer" in "303 "*"/signin/") ;; *) failures=$((failures + 1)) ;; esac
  done
  [ "$signin" = 200 ] || failures=$((failures + 1))
  for site in ${OTHER_SITES//,/ }; do
    answer="$(caddy_answer "$site")"
    echo "other site $site: $answer"
    case "$answer" in "303 "*) ;; *) failures=$((failures + 1)) ;; esac
  done
  echo "check failures in the proxy container: $failures"
  [ "$failures" = 0 ]
}

case "${1:-}" in
  backup) do_backup ;;
  fresh) backup_is_fresh || { echo "proxy container: no backup from the last $FRESH_BACKUP_MINUTES minutes; run backup first"; exit 1; } ;;
  apply)
    backup_is_fresh || { echo "refusing: run backup first (none found from the last $FRESH_BACKUP_MINUTES minutes)"; exit 1; }
    take_lock; install_signin "${2:?apply needs the sign-in bundle}"; add_site "${3:?apply needs the Caddy block}"; do_check ;;
  check) do_check ;;
  undo) take_lock; remove_site; undo_signin; do_check || true ;;
  *) echo "unknown action"; exit 2 ;;
esac
PROXY_SIDE
}

push_to() {
  local ct="$1" source="$2" target="$3" mode="$4" name
  name="$(basename "$target")"
  scp -q "$source" "$PROXMOX:/root/$name"
  ssh -o BatchMode=yes "$PROXMOX" "pct push $ct /root/$name $target --perms $mode && rm /root/$name"
}

push_scripts() {
  local work
  work="$(mktemp -d)"
  write_app_script > "$work/app.sh"
  write_proxy_script > "$work/proxy.sh"
  push_to "$APP_CT" "$work/app.sh" "$APP_SCRIPT" 700
  push_to "$PROXY_CT" "$work/proxy.sh" "$PROXY_SCRIPT" 700
  rm -rf "$work"
}

in_app() {
  ssh -o BatchMode=yes "$PROXMOX" "pct exec $APP_CT -- $(container_side_env) bash $APP_SCRIPT $*"
}

in_proxy() {
  ssh -o BatchMode=yes "$PROXMOX" "pct exec $PROXY_CT -- $(container_side_env) bash $PROXY_SCRIPT $*"
}

deploy_code() {
  local commit="$1" work
  [ -f "$SITE_SIGNIN/build.py" ] || { echo "refusing: no site-signin builder at $SITE_SIGNIN (set SITE_SIGNIN)"; exit 1; }
  in_app fresh
  in_proxy fresh
  work="$(mktemp -d)"
  git -C "$REPO" archive --format=tar.gz -o "$work/code.tgz" "$commit"
  python3 "$SITE_SIGNIN/build.py" --first Starfire --second Photos --tagline "Sign in to reach your photos." \
    --icon "$REPO/photos/static/icon.svg" --out "$work/signin"
  tar -czf "$work/signin.tgz" -C "$work/signin" .
  push_to "$APP_CT" "$work/code.tgz" /root/photos-code.tgz 600
  push_to "$PROXY_CT" "$work/signin.tgz" /root/photos-signin.tgz 600
  push_to "$PROXY_CT" "$REPO/deploy/Caddyfile.photos" /root/photos-caddy-block 600
  rm -rf "$work"
  in_app apply /root/photos-code.tgz
  in_proxy apply /root/photos-signin.tgz /root/photos-caddy-block
}

main() {
  case "${1:-}" in
    backup|apply|check|undo) load_site ;;
  esac
  case "${1:-}" in
    backup) push_scripts; in_app backup; in_proxy backup ;;
    apply) push_scripts; deploy_code "${2:-HEAD}" ;;
    check) push_scripts; in_app check; in_proxy check ;;
    undo) push_scripts; in_proxy undo; in_app undo ;;
    -h|--help|help) usage ;;
    *) usage; exit 2 ;;
  esac
}

main "$@"

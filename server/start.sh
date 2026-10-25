#!/bin/sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
umask 077
. ./compose.sh
docker info >/dev/null
[ -f bootstrap/manifest.json ] || { echo 'Missing public bootstrap. Extract the complete Docker kit.' >&2; exit 1; }
for container in ngs-server-app-1 ngs-server-db-1; do
  owner="$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' "$container" 2>/dev/null || true)"
  if [ -n "$owner" ] && [ "$owner" != "$PWD" ]; then
    echo 'NGS belongs to another folder; refusing to mix installations.' >&2; exit 1
  fi
done
mkdir -p secrets data backups empty-games empty-installers client
[ -f .env ] || cp .env.example .env
if [ ! -f secrets/tactic_admin_password ]; then
  printf 'Choose an admin password (12–128 characters): ' >&2
  previous="$(stty -g)"; trap 'stty "$previous"' EXIT HUP INT TERM
  stty -echo; IFS= read -r password; stty "$previous"; trap - EXIT HUP INT TERM; printf '\n' >&2
  [ "${#password}" -ge 12 ] && [ "${#password}" -le 128 ] || { echo 'Invalid password length.' >&2; exit 1; }
  printf '%s' "$password" > secrets/tactic_admin_password
  unset password
fi
for name in postgres_password ngs_proxy_token; do
  if [ ! -f "secrets/$name" ]; then
    count=36; [ "$name" != ngs_proxy_token ] || count=32
    head -c "$count" /dev/urandom | od -An -v -tx1 | tr -d ' \n' > "secrets/$name"
  fi
done
chmod 0700 secrets; chmod 0600 secrets/*
domain="$(sed -n 's/^NGS_PUBLIC_DOMAIN=//p' .env | tail -1 | tr -d '\r')"
if [ -n "$domain" ]; then
  printf '%s' "$domain" | grep -Eq '^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$' || { echo 'Invalid public domain' >&2; exit 1; }
  export COMPOSE_PROFILES=public NGS_BIND_IP=127.0.0.1
fi
compose pull
compose up -d --no-build
echo 'NGS is starting. First setup can take several minutes.'
echo 'Show status: docker compose -p ngs-server -f compose.yaml ps'
echo 'Show the local pairing code: docker compose -p ngs-server -f compose.yaml logs app'

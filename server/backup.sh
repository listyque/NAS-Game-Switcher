#!/bin/sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
umask 077
. ./compose.sh
# A Compose project name is global to the Docker daemon. Never combine another
# folder's databases with this folder's files and secrets.
for container in ngs-server-app-1 ngs-server-db-1; do
  if ! owner="$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' "$container")"; then
    echo "Cannot verify the owner of $container; backup refused." >&2
    exit 1
  fi
  if [ "$owner" != "$PWD" ]; then
    echo 'ngs-server belongs to another folder; backup refused.' >&2
    exit 1
  fi
done

mkdir -p backups
lock=backups/.backup.lock
if ! mkdir "$lock" 2>/dev/null; then
  echo 'Another backup is running, or its lock needs manual recovery.' >&2
  exit 1
fi
resume_app=false
cleanup() {
  result=$?
  trap - EXIT
  trap '' HUP INT TERM
  if [ "$resume_app" = true ]; then
    # Also covers a signal delivered while `compose stop` was finishing.
    if running="$(docker inspect --format '{{.State.Running}}' ngs-server-app-1)"; then
      case "$running" in
        false) if ! compose start app; then result=1; fi ;;
        true) ;;
        *) echo 'Cannot determine whether app needs restarting.' >&2; result=1 ;;
      esac
    else
      echo 'Cannot verify app state for restart; inspect it manually.' >&2
      result=1
    fi
  fi
  if ! rmdir "$lock"; then result=1; fi
  exit "$result"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

running="$(docker inspect --format '{{.State.Running}}' ngs-server-app-1)"
case "$running" in
  true|false) ;;
  *) echo 'Cannot determine app state; backup refused.' >&2; exit 1 ;;
esac
backup_dir="$(mktemp -d "backups/$(date -u +%Y%m%dT%H%M%SZ)XXXXXX")"
name="${backup_dir##*/}"
if [ "$running" = true ]; then
  resume_app=true
  compose stop app
fi
for db in sthpw ngs_launcher; do
  compose exec -T db sh -c 'export PGPASSWORD="$(cat /run/secrets/postgres_password)"; exec pg_dump -U postgres --format=custom --no-owner --no-acl "$1"' sh "$db" > "backups/$name/$db.dump"
done
compose run --rm --no-deps app /opt/ngs/backup_assets.py "$name"
cp -R secrets "backups/$name/secrets"
cp .env "backups/$name/compose.env"
cp bootstrap/manifest.json "backups/$name/manifest.json"
printf 'Complete NGS backup\n' > "backups/$name/COMPLETE"
echo "Private complete backup: backups/$name"

# Shared CLI selection for DSM (some versions ship docker-compose separately).
PATH="$PATH:/usr/local/bin:/var/packages/ContainerManager/target/usr/bin"
export PATH
if docker compose version >/dev/null 2>&1; then
  compose() { docker compose -p ngs-server -f compose.yaml "$@"; }
elif command -v docker-compose >/dev/null 2>&1; then
  compose() { docker-compose -p ngs-server -f compose.yaml "$@"; }
else
  echo 'Docker Compose is missing; install/enable Synology Container Manager.' >&2
  exit 1
fi

#!/bin/sh
set -eu
export NGS_PROXY_TOKEN="$(cat /run/secrets/ngs_proxy_token)"
[ "${#NGS_PROXY_TOKEN}" = 64 ] || { echo 'Invalid NGS gateway secret' >&2; exit 1; }
exec caddy run --config /etc/ngs-public/Caddyfile --adapter caddyfile

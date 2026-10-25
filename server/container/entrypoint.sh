#!/bin/bash
set -euo pipefail
umask 077
mkdir -p /opt/tactic/tactic_data/{config,plugins,temp,private} /opt/tactic/assets /opt/tactic/projects /run/apache2
chown tactic:tactic /opt/tactic/tactic_data /opt/tactic/tactic_data/{config,plugins,temp,private} /opt/tactic/assets /opt/tactic/projects
install -d -m 0700 -o tactic -g tactic /run/ngs-secrets
for secret in postgres_password tactic_admin_password ngs_proxy_token; do
  install -m 0400 -o tactic -g tactic "/run/secrets/$secret" "/run/ngs-secrets/$secret"
done
export NGS_SECRETS_DIR=/run/ngs-secrets
if [ "$#" -gt 0 ]; then exec python3 "$@"; fi
python3 /opt/ngs/runtime_update.py
gosu tactic python3 /opt/ngs/init_tactic.py
python3 /opt/ngs/restore.py
gosu tactic python3 /opt/ngs/first_run.py
if [ -d /opt/ngs-client ]; then
  client=/opt/tactic/assets/ngs_launcher/client
  [ ! -L "$client" ] || { echo 'Linked NGS client directory rejected' >&2; exit 1; }
  install -d -m 0755 -o tactic -g tactic "$client"
  for name in NGS-Setup.exe NGS-Client.zip index.html; do
    if [ -f "/opt/ngs-client/$name" ]; then
      install -m 0644 -o tactic -g tactic "/opt/ngs-client/$name" "$client/$name"
    fi
  done
fi
gosu tactic python3 /opt/tactic/tactic/src/bin/monitor.py &
monitor_pid=$!
apache2ctl -D FOREGROUND &
apache_pid=$!
cleanup() {
  trap - EXIT TERM INT
  kill "$monitor_pid" "$apache_pid" 2>/dev/null || true
  wait "$monitor_pid" "$apache_pid" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 0' TERM INT
wait -n "$monitor_pid" "$apache_pid"
exit 1

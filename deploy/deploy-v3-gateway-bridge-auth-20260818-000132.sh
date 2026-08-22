#!/usr/bin/env bash
set -euo pipefail

release=/opt/wanda-v3-backend/releases/v3-gateway-bridge-auth-20260818-000132
source_file=/tmp/wanda-v3-gateway-auth-wanda_quote.py
dropin=/tmp/wanda-v3-gateway-bridge-auth-20260818-000132.service.conf
env_file=/etc/wanda-v3-backend.env
env_backup=/etc/wanda-v3-backend.env.before-v3-gateway-bridge-auth-20260818-000132
old_release="$(systemctl show wanda-v3-backend -p WorkingDirectory --value)"

case "$old_release" in
  /opt/wanda-v3-backend/releases/*) ;;
  *) echo "unexpected V3 release: $old_release" >&2; exit 1 ;;
esac
test -f "$source_file"
test -f "$dropin"
test ! -e "$release"

# Reuse the existing, dedicated plugin bridge secret. It is never printed and
# only authorizes the ticket gateway's V3 quote-route allowlist.
gateway_key="$(sed -n 's/^PLUGIN_BRIDGE_KEY=//p' /etc/ticket-system/backend.env)"
test -n "$gateway_key"
cp -a "$env_file" "$env_backup"

mkdir -p "$release"
cp -al "$old_release/." "$release/"
install -m 0644 "$source_file" "$release/app/wanda_quote.py"
if grep -q '^WANDA_QUOTE_GATEWAY_KEY=' "$env_file"; then
  sed -i "s|^WANDA_QUOTE_GATEWAY_KEY=.*|WANDA_QUOTE_GATEWAY_KEY=$gateway_key|" "$env_file"
else
  printf '\nWANDA_QUOTE_GATEWAY_KEY=%s\n' "$gateway_key" >> "$env_file"
fi
chmod 0600 "$env_file"
install -D -m 0644 "$dropin" \
  /etc/systemd/system/wanda-v3-backend.service.d/zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz-v3-gateway-bridge-auth.conf
systemctl daemon-reload
systemctl restart wanda-v3-backend

healthy=false
for _ in $(seq 1 15); do
  if systemctl is-active --quiet wanda-v3-backend && curl -fsS http://127.0.0.1:8011/health >/dev/null; then
    healthy=true
    break
  fi
  sleep 1
done
if [[ "$healthy" != true ]]; then
  cp -a "$env_backup" "$env_file"
  rm -f /etc/systemd/system/wanda-v3-backend.service.d/zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz-v3-gateway-bridge-auth.conf
  systemctl daemon-reload
  systemctl restart wanda-v3-backend
  echo "V3 gateway auth release failed and was rolled back" >&2
  exit 1
fi

systemctl show wanda-seat-autoquote -p WorkingDirectory -p ExecStart
systemctl show wanda-v3-backend -p WorkingDirectory

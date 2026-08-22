#!/usr/bin/env bash
set -euo pipefail

service=wanda-seat-autoquote.service
previous=/opt/wanda-preview-plugin/releases/wanda-plugin-0.4.21-vision-backend-diagnostics-20260817
release=/opt/wanda-preview-plugin/releases/wanda-plugin-0.4.22-ui-syntax-20260817
package=/tmp/wanda-seat-autoquote-0.4.22-ui-syntax-20260817.tgz
dropin=/tmp/wanda-seat-autoquote-ui-syntax-0.4.22.service.conf

# Do not create a release with a partial package or without the dependency tree
# expected by this no-network production deployment.
test -f "$package"
test -f "$dropin"
test -d "$previous/node_modules"
test ! -e "$release"

install -d -o ticket-system -g ticket-system -m 0755 "$release"
tar -xzf "$package" -C "$release"
test -f "$release/index.mjs"
test -f "$release/src/ui-handler.mjs"
test -f "$release/ui/app.js"
cp -a "$previous/node_modules" "$release/node_modules"
chown -R ticket-system:ticket-system "$release"

install -D -m 0644 "$dropin" \
  /etc/systemd/system/wanda-seat-autoquote.service.d/zzzzzzzzzzzzzzzz-ui-syntax-0.4.22.conf
systemctl daemon-reload
systemctl restart "$service"
sleep 2

test "$(systemctl is-active "$service")" = active
test "$(systemctl show "$service" --property=WorkingDirectory --value)" = "$release"

# Report the effective backend target without printing bridge credentials. A
# 401 proves that the protected bridge route exists; 404 means the plugin is
# still pointed at the wrong/old backend release or port.
environment="$(systemctl show "$service" --property=Environment --value)"
backend_url="$(printf '%s\n' "$environment" | tr ' ' '\n' | sed -n 's/^BACKEND_BASE_URL=//p' | tail -n 1)"
if [[ -z "$backend_url" ]]; then
  echo 'BACKEND_BASE_URL is not visible in the unit environment; inspect its EnvironmentFile before testing images.' >&2
  exit 1
fi
backend_url="${backend_url%/}"
printf 'BACKEND_BASE_URL=%s\n' "$backend_url"

status="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "$backend_url/api/xianyu-plugin/bridge/runtime-settings")"
case "$status" in
  200|401) printf 'bridge runtime-settings route reachable (HTTP %s)\n' "$status" ;;
  *) echo "bridge route check failed (HTTP $status): BACKEND_BASE_URL points to an incompatible backend release or port" >&2; exit 1 ;;
esac

# These routes are intentionally unauthenticated in the current V3 backend;
# a 405/422 also proves route resolution, while 404 does not.
for path in /api/quotes/pending /api/wanda-ai/vision/recognize; do
  status="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "$backend_url$path")"
  if [[ "$status" == 404 || "$status" == 000 ]]; then
    echo "required V3 route unavailable: $backend_url$path (HTTP $status)" >&2
    exit 1
  fi
  printf '%s -> HTTP %s\n' "$path" "$status"
done

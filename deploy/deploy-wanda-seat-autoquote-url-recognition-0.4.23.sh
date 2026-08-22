#!/usr/bin/env bash
set -euo pipefail

service=wanda-seat-autoquote.service
previous=/opt/wanda-preview-plugin/releases/wanda-plugin-0.4.22-ui-syntax-20260817
release=/opt/wanda-preview-plugin/releases/wanda-plugin-0.4.23-url-recognition-20260817
package=/tmp/wanda-seat-autoquote-0.4.23-url-recognition-20260817.tgz
dropin=/tmp/wanda-seat-autoquote-url-recognition-0.4.23.service.conf

test -f "$package"
test -f "$dropin"
test -d "$previous/node_modules"
test ! -e "$release"

install -d -o ticket-system -g ticket-system -m 0755 "$release"
tar -xzf "$package" -C "$release"
test -f "$release/index.mjs"
test -f "$release/src/application.mjs"
test -f "$release/ui/app.js"
cp -a "$previous/node_modules" "$release/node_modules"
chown -R ticket-system:ticket-system "$release"

install -D -m 0644 "$dropin" \
  /etc/systemd/system/wanda-seat-autoquote.service.d/zzzzzzzzzzzzzzzzz-url-recognition-0.4.23.conf
systemctl daemon-reload
systemctl restart "$service"
sleep 2

test "$(systemctl is-active "$service")" = active
test "$(systemctl show "$service" --property=WorkingDirectory --value)" = "$release"
node --check "$release/ui/app.js"
node --check "$release/src/application.mjs"

# The new image-url path needs only a small JSON request. Verify V3's public
# image fetch and vision route are reachable without exposing bridge credentials.
status="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:8011/api/wanda-ai/vision/recognize)"
[[ "$status" != 000 && "$status" != 404 ]]
printf 'released %s; V3 vision route HTTP %s\n' "$release" "$status"

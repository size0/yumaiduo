#!/usr/bin/env bash
set -euo pipefail
service=wanda-seat-autoquote.service
previous=/opt/wanda-preview-plugin/releases/wanda-plugin-0.4.24-ui-restore-20260817
release=/opt/wanda-preview-plugin/releases/wanda-plugin-0.4.25-url-ui-20260817
package=/tmp/wanda-seat-autoquote-0.4.25-url-ui-20260817.tgz
dropin=/tmp/wanda-seat-autoquote-url-ui-0.4.25.service.conf
test -f "$package"; test -f "$dropin"; test -d "$previous/node_modules"; test ! -e "$release"
install -d -o ticket-system -g ticket-system -m 0755 "$release"
tar -xzf "$package" -C "$release"
test -f "$release/index.mjs"; test -f "$release/ui/app.js"; test -f "$release/ui/index.html"
cp -a "$previous/node_modules" "$release/node_modules"
chown -R ticket-system:ticket-system "$release"
install -D -m 0644 "$dropin" /etc/systemd/system/wanda-seat-autoquote.service.d/zzzzzzzzzzzzzzzzzzz-url-ui-0.4.25.conf
systemctl daemon-reload
systemctl restart "$service"
sleep 2
test "$(systemctl is-active "$service")" = active
test "$(systemctl show "$service" --property=WorkingDirectory --value)" = "$release"
node --check "$release/ui/app.js"
printf 'released %s\n' "$release"

#!/usr/bin/env bash
set -euo pipefail

previous=/opt/wanda-preview-plugin/releases/wanda-plugin-0.4.19-shop-switch-gate-20260817
release=/opt/wanda-preview-plugin/releases/wanda-plugin-0.4.20-vision-error-codes-20260817
test -d "$previous/node_modules"
test ! -e "$release"
install -d -o ticket-system -g ticket-system -m 0755 "$release"
tar -xzf /tmp/wanda-plugin-vision-error-codes-20260817.tgz -C "$release"
cp -a "$previous/node_modules" "$release/node_modules"
chown -R ticket-system:ticket-system "$release"
install -D -m 0644 /tmp/wanda-seat-autoquote-vision-error-codes-20260817.service.conf \
  /etc/systemd/system/wanda-seat-autoquote.service.d/zzzzzzzzzzzzz-vision-error-codes.conf
systemctl daemon-reload
systemctl restart wanda-seat-autoquote.service
sleep 2
systemctl is-active wanda-seat-autoquote.service
systemctl show wanda-seat-autoquote.service --property=WorkingDirectory --value

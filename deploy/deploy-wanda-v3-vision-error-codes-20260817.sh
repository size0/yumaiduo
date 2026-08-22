#!/usr/bin/env bash
set -euo pipefail

release=/opt/wanda-v3-backend/releases/v3-vision-error-codes-20260817
test ! -e "$release"
install -d -o ticket-system -g ticket-system -m 0755 "$release"
tar -xzf /tmp/wanda-v3-vision-error-codes-20260817.tgz -C "$release"
chown -R ticket-system:ticket-system "$release"
install -D -m 0644 /tmp/wanda-v3-vision-error-codes-20260817.service.conf \
  /etc/systemd/system/wanda-v3-backend.service.d/zzzzzzzzzzzzz-vision-error-codes.conf
systemctl daemon-reload
systemctl restart wanda-v3-backend.service
sleep 2
systemctl is-active wanda-v3-backend.service
systemctl show wanda-v3-backend.service --property=WorkingDirectory --value
curl -fsS http://127.0.0.1:8011/health

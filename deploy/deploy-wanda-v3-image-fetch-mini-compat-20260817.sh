#!/usr/bin/env bash
set -euo pipefail

release=/opt/wanda-v3-backend/releases/v3-image-fetch-mini-compat-20260817
test ! -e "$release"
install -d -o root -g root -m 0755 "$release"
tar -xzf /tmp/wanda-v3-image-fetch-mini-compat-20260817.tgz -C "$release"
chown -R root:root "$release"
install -D -m 0644 /tmp/wanda-v3-image-fetch-mini-compat-20260817.service.conf \
  /etc/systemd/system/wanda-v3-backend.service.d/zzzzzzzzzzzzzz-image-fetch-mini-compat.conf
systemctl daemon-reload
systemctl restart wanda-v3-backend.service
sleep 2
systemctl is-active wanda-v3-backend.service
systemctl show wanda-v3-backend.service --property=WorkingDirectory --value
curl -fsS http://127.0.0.1:8011/health

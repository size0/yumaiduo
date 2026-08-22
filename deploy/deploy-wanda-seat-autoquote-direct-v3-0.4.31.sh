#!/usr/bin/env bash
set -euo pipefail

release=/opt/wanda-preview-plugin/releases/wanda-plugin-0.4.31-direct-v3-20260817
test ! -e "$release"
install -d -o ticket-system -g ticket-system -m 0755 "$release"
tar -xzf /tmp/wanda-plugin-0.4.31-direct-v3-20260817.tgz -C "$release"
chown -R ticket-system:ticket-system "$release"
install -D -m 0644 /tmp/wanda-seat-autoquote-direct-v3-0.4.31.service.conf \
  /etc/systemd/system/wanda-seat-autoquote.service.d/zzzzzzzzzzzzzzzzzzzzzzzzzzz-direct-v3.conf
systemctl daemon-reload
systemctl restart wanda-seat-autoquote.service
sleep 2
systemctl is-active wanda-seat-autoquote.service
systemctl show wanda-seat-autoquote.service --property=WorkingDirectory --property=ExecStart --no-pager

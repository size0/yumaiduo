#!/usr/bin/env bash
set -euo pipefail

release=/opt/wanda-preview-plugin/releases/wanda-plugin-0.4.30-cos-ingest-20260817
test ! -e "$release"
install -d -o ticket-system -g ticket-system -m 0755 "$release"
tar -xzf /tmp/wanda-plugin-0.4.30-cos-ingest-20260817.tgz -C "$release"
chown -R ticket-system:ticket-system "$release"
install -D -m 0644 /tmp/wanda-seat-autoquote-cos-ingest-0.4.30.service.conf \
  /etc/systemd/system/wanda-seat-autoquote.service.d/zzzzzzzzzzzzzzzzzzzzzzzzzz-cos-ingest.conf
systemctl daemon-reload
systemctl restart wanda-seat-autoquote.service
sleep 2
systemctl is-active wanda-seat-autoquote.service
systemctl show wanda-seat-autoquote.service --property=WorkingDirectory --property=ExecStart --no-pager
curl -fsS http://127.0.0.1:4003/health

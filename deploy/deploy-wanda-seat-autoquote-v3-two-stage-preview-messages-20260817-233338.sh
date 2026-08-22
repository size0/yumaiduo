#!/usr/bin/env bash
set -euo pipefail

release=/opt/wanda-preview-plugin/releases/v3-two-stage-preview-messages-20260817-233338
archive=/tmp/wanda-seat-autoquote-v3-two-stage-preview-messages-20260817-233338.tgz
dropin=/tmp/wanda-seat-autoquote-v3-two-stage-preview-messages-20260817-233338.service.conf
base_release=/opt/wanda-preview-plugin/releases/v3-intent-based-text-replies-20260817-224715

# The release is immutable and V3-only; do not overwrite an existing release.
test ! -e "$release"
test -d "$base_release/node_modules"
install -d -o ticket-system -g ticket-system -m 0755 "$release"
tar -xzf "$archive" -C "$release"
# The packaged SDK is a local file dependency. Copy the already deployed,
# known-good production dependency tree instead of performing a network install.
cp -a "$base_release/node_modules" "$release/node_modules"
chown -R ticket-system:ticket-system "$release"
install -D -m 0644 "$dropin" \
  /etc/systemd/system/wanda-seat-autoquote.service.d/zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz-v3-two-stage-preview-messages.conf
systemctl daemon-reload
systemctl restart wanda-seat-autoquote.service
for _ in $(seq 1 15); do
  systemctl is-active --quiet wanda-seat-autoquote.service && break
  sleep 1
done
systemctl is-active wanda-seat-autoquote.service
systemctl show wanda-seat-autoquote -p WorkingDirectory -p ExecStart
systemctl show wanda-v3-backend -p WorkingDirectory

#!/usr/bin/env bash
set -euo pipefail
release="$(systemctl show wanda-v3-backend.service --property=WorkingDirectory --value)"
case "$release" in
  /opt/wanda-v3-backend/releases/*) ;;
  *) echo '{"ok":false,"code":"invalid_v3_working_directory"}' >&2; exit 1 ;;
esac
cd "$release"
exec /opt/wanda-v3-backend/venv/bin/python -m app.release_guard

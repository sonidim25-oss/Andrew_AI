#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

PROJECT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
SOURCE="$PROJECT/prototype/andrewbot"
TARGET=/opt/andrew/prototype/andrewbot
VALIDATE_ONLY=0

if [ "${1:-}" = "--validate-only" ]; then
  VALIDATE_ONLY=1
elif [ "$#" -gt 0 ]; then
  echo "Usage: $0 [--validate-only]" >&2
  exit 2
fi

if [ "$(id -u)" -ne 0 ]; then
  echo "Run with sudo from SSH: sudo bash $PROJECT/scripts/validate-and-start-controller.sh" >&2
  exit 2
fi
if ! systemctl cat andrew-control.service >/dev/null 2>&1; then
  echo "andrew-control.service is not installed." >&2
  exit 2
fi
if systemctl is-active --quiet andrew-control.service; then
  echo "andrew-control.service is already active; refusing to interrupt it." >&2
  exit 2
fi
if ! compgen -G "$SOURCE/*.py" >/dev/null; then
  echo "No Andrew controller modules found in $SOURCE" >&2
  exit 2
fi

# Install only Python modules from Andrew's trusted controller package.
find "$SOURCE" -maxdepth 1 -type f -name '*.py' -exec install -o root -g andrew-control -m 0640 {} "$TARGET/" \;

systemctl reset-failed andrew-control.service >/dev/null 2>&1 || true
echo "Running one-shot startup validation as andrew-control; no long-running controller is started yet."
if ! systemd-run --wait --pipe --collect --quiet --unit=andrew-control-validation \
  --uid=andrew-control --gid=andrew-control \
  --property=Type=oneshot \
  --property=WorkingDirectory=/opt/andrew/prototype \
  --property=EnvironmentFile=/etc/andrew/controller.conf \
  --property='Environment=PYTHONPATH=/opt/andrew/prototype ANDREW_LETTA_URL=ws://127.0.0.1:8283/ws ANDREW_GATEWAY_PORT=8284 ANDREW_MONTHLY_LIMIT_USD=10 ANDREW_MONTHLY_RESERVE_USD=1 ANDREW_DAILY_TARGET_USD=0.30 ANDREW_TIMEZONE=America/Toronto ANDREW_FALLBACK_WAKE=09:00' \
  --property=LoadCredentialEncrypted=andrew-vault-key:/etc/credstore.encrypted/andrew-vault-key \
  --property=LoadCredentialEncrypted=andrew-memory-backup-key:/etc/credstore.encrypted/andrew-memory-backup-key \
  --property=LoadCredentialEncrypted=andrew-history-key:/etc/credstore.encrypted/andrew-history-key \
  --property=LoadCredentialEncrypted=andrew-gateway-token:/etc/credstore.encrypted/andrew-gateway-token \
  --property=UMask=0077 \
  --property=NoNewPrivileges=yes \
  --property=PrivateTmp=yes \
  --property=ProtectSystem=strict \
  --property=ProtectHome=yes \
  --property=ProtectKernelTunables=yes \
  --property=ProtectKernelModules=yes \
  --property=ProtectControlGroups=yes \
  --property=LockPersonality=yes \
  --property=RestrictSUIDSGID=yes \
  --property='RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6' \
  --property='ReadWritePaths=/var/lib/andrew/control /var/lib/andrew/vault' \
  --property=MemoryMax=512M \
  --property=CPUQuota=100% \
  --property=TasksMax=64 \
  --property=LimitNOFILE=2048 \
  /usr/bin/python3 -m andrewbot.main --validate-only; then
  echo "Startup validation failed. The service was left stopped. Review the safe check name above; no credentials were printed." >&2
  exit 1
fi

if [ "$VALIDATE_ONLY" -eq 1 ]; then
  echo "One-shot validation passed; andrew-control.service remains stopped."
  exit 0
fi

echo "One-shot validation passed; restarting only andrew-control.service."
systemctl restart andrew-control.service
for _attempt in $(seq 1 12); do
  if systemctl is-active --quiet andrew-control.service; then
    echo "andrew-control.service is active. Send Andrew a private Telegram message to verify the reply path."
    exit 0
  fi
  sleep 1
done
echo "The controller did not remain active. Recent Andrew-only journal output follows:" >&2
journalctl -u andrew-control.service -n 30 --no-pager >&2
exit 1

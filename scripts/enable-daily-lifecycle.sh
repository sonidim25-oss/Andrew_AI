#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

PROJECT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
DROPIN=/etc/systemd/system/andrew-control.service.d/40-daily-lifecycle.conf
LIFECYCLE_ENV=/etc/andrew/daily-lifecycle.conf
PROBE_ENV=/run/andrew-lifecycle-probe.conf
EVIDENCE=/var/lib/andrew/control/lifecycle-validation.json
RETRY=0

if [ "$(id -u)" -ne 0 ]; then
  echo "Run from SSH: sudo bash $PROJECT/scripts/enable-daily-lifecycle.sh" >&2
  exit 2
fi
if [ "$#" -eq 1 ] && [ "$1" = "--retry" ]; then
  RETRY=1
elif [ "$#" -ne 0 ]; then
  echo "Usage: $0 [--retry]" >&2
  exit 2
fi
if [ "$RETRY" -eq 1 ]; then
  if [ -e "$EVIDENCE" ] || ! cmp -s "$LIFECYCLE_ENV" <(printf '%s\n' 'ANDREW_AUTONOMOUS_ENABLED=0' 'ANDREW_FALLBACK_WAKE_ENABLED=0' 'ANDREW_ISOLATED_RUNTIME_ENABLED=0') ||
     ! cmp -s "$DROPIN" <(printf '%s\n' '[Service]' "EnvironmentFile=$LIFECYCLE_ENV"); then
    echo "Retry requires the previous disabled configuration and no completed-stage evidence; inspect before proceeding." >&2
    exit 2
  fi
  for unit in andrew-control.service andrew-lifecycle-prepare.service andrew-lifecycle-resume.service; do
    if systemctl is-active --quiet "$unit"; then
      echo "Retry refused while $unit is active." >&2
      exit 2
    fi
  done
elif [ -e "$DROPIN" ] || [ -e "$LIFECYCLE_ENV" ] || [ -e "$EVIDENCE" ]; then
  echo "Existing lifecycle configuration or commissioning evidence found; inspect before repeating." >&2
  exit 2
fi
systemctl is-active --quiet andrew-letta.service || { echo "Andrew Letta must be running first." >&2; exit 1; }

rollback() {
  trap - ERR INT TERM
  systemctl stop andrew-lifecycle-prepare.service andrew-lifecycle-resume.service 2>/dev/null || true
  systemctl stop andrew-control.service || true
  install -d -o root -g root -m 0755 /etc/systemd/system/andrew-control.service.d
  printf '%s\n' 'ANDREW_AUTONOMOUS_ENABLED=0' 'ANDREW_FALLBACK_WAKE_ENABLED=0' 'ANDREW_ISOLATED_RUNTIME_ENABLED=0' > "$LIFECYCLE_ENV"
  chmod 0644 "$LIFECYCLE_ENV"
  printf '%s\n' '[Service]' "EnvironmentFile=$LIFECYCLE_ENV" > "$DROPIN"
  chmod 0644 "$DROPIN"
  rm -f -- "$PROBE_ENV"
  systemctl daemon-reload
  echo "Commissioning stopped. Controller left stopped and autonomous flags disabled; owner pause, memory and spending reservations were preserved." >&2
  exit 1
}
trap rollback ERR INT TERM

echo "Stopping only Andrew controller for two bounded live commissioning sessions..."
systemctl stop andrew-control.service
bash "$PROJECT/scripts/validate-and-start-controller.sh" --validate-only
install -d -o root -g andrew-control -m 0750 /opt/andrew/admin
install -o root -g andrew-control -m 0640 "$PROJECT/scripts/verify-live-lifecycle.py" /opt/andrew/admin/verify-live-lifecycle.py
cat /etc/andrew/controller.conf > "$PROBE_ENV"
printf '\n%s\n' 'ANDREW_AUTONOMOUS_ENABLED=1' 'ANDREW_FALLBACK_WAKE_ENABLED=1' 'ANDREW_ISOLATED_RUNTIME_ENABLED=0' \
  'ANDREW_TIMEZONE=America/Toronto' 'ANDREW_FALLBACK_WAKE=09:00' "ANDREW_LIFECYCLE_RETRY=$RETRY" >> "$PROBE_ENV"
chmod 0600 "$PROBE_ENV"

probe() {
  systemd-run --wait --pipe --collect --quiet --unit="andrew-lifecycle-$1" \
    --uid=andrew-control --gid=andrew-control \
    --property=Type=oneshot --property=TimeoutStartSec=8min \
    --property=WorkingDirectory=/opt/andrew/prototype \
    --property="EnvironmentFile=$PROBE_ENV" \
    --property='Environment=PYTHONPATH=/opt/andrew/prototype' \
    --property=LoadCredentialEncrypted=andrew-vault-key:/etc/credstore.encrypted/andrew-vault-key \
    --property=LoadCredentialEncrypted=andrew-gateway-token:/etc/credstore.encrypted/andrew-gateway-token \
    --property=UMask=0077 --property=NoNewPrivileges=yes --property=PrivateTmp=yes \
    --property=ProtectSystem=strict --property=ProtectHome=yes \
    --property=ProtectKernelTunables=yes --property=ProtectKernelModules=yes \
    --property=ProtectControlGroups=yes --property=LockPersonality=yes \
    --property=RestrictSUIDSGID=yes --property='RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6' \
    --property='ReadWritePaths=/var/lib/andrew/control /var/lib/andrew/vault' \
    --property=MemoryMax=512M --property=CPUQuota=100% --property=TasksMax=64 --property=LimitNOFILE=2048 \
    /usr/bin/python3 /opt/andrew/admin/verify-live-lifecycle.py "$1"
}

probe prepare
echo "Restarting only Andrew Letta; the second process must recover the saved sleep, diary and ledger..."
systemctl restart andrew-letta.service
probe resume

install -d -o root -g root -m 0755 /etc/systemd/system/andrew-control.service.d
printf '%s\n' '[Service]' \
  "EnvironmentFile=$LIFECYCLE_ENV" > "$DROPIN"
chmod 0644 "$DROPIN"
printf '%s\n' 'ANDREW_AUTONOMOUS_ENABLED=1' 'ANDREW_FALLBACK_WAKE_ENABLED=1' 'ANDREW_ISOLATED_RUNTIME_ENABLED=0' \
  'ANDREW_TIMEZONE=America/Toronto' 'ANDREW_FALLBACK_WAKE=09:00' > "$LIFECYCLE_ENV"
chmod 0644 "$LIFECYCLE_ENV"
rm -f -- "$PROBE_ENV"
systemctl daemon-reload
systemctl start andrew-control.service
started=0
for _attempt in $(seq 1 60); do
  invocation="$(systemctl show andrew-control.service -p InvocationID --value)"
  if [[ -n "$invocation" ]] && systemctl is-active --quiet andrew-control.service &&
     journalctl --quiet "_SYSTEMD_INVOCATION_ID=$invocation" --no-pager -o cat \
       --grep='Andrew Telegram controller started' | grep -q 'Andrew Telegram controller started'; then
    journalctl --quiet "_SYSTEMD_INVOCATION_ID=$invocation" --no-pager -o cat \
      --grep='Lifecycle schedules: fallback=enabled autonomous=enabled' | grep -q 'fallback=enabled autonomous=enabled'
    started=1
    break
  fi
  sleep 1
done
[ "$started" -eq 1 ]
trap - ERR INT TERM
echo "Daily conversation/memory lifecycle enabled. Fallback: 09:00 America/Toronto. External runtime: disabled."
echo "Live fallback, diary delivery, timed sleep, restart persistence, scheduled wake and spending settlement passed."
echo "Evidence: $EVIDENCE. Owner controls: /status /budget /diary /pause; /wake clears owner pause."

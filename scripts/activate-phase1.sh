#!/usr/bin/env bash
set -euo pipefail
umask 077

PROJECT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
SOURCE="$PROJECT/prototype"
TARGET=/opt/andrew/prototype
GATEWAY_CREDENTIAL=/etc/credstore.encrypted/andrew-gateway-token
CONTROL_DROPIN=/etc/systemd/system/andrew-control.service.d/30-phase1-runtime.conf

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this script with sudo from SSH: sudo $PROJECT/scripts/activate-phase1.sh" >&2
  exit 2
fi
if [ ! -d "$SOURCE/andrewbot" ] || [ ! -d "$SOURCE/tests" ]; then
  echo "Phase 1 source files are missing." >&2
  exit 2
fi
for unit in andrew-control.service andrew-letta.service; do
  if ! systemctl cat "$unit" >/dev/null 2>&1; then
    echo "Required Andrew unit is missing: $unit" >&2
    exit 2
  fi
done
if [ ! -d /etc/credstore.encrypted ] || [ ! -f /etc/credstore.encrypted/andrew-vault-key ]; then
  echo "The encrypted Andrew vault key is missing; no services were changed." >&2
  exit 2
fi
if ss -H -ltn 'sport = :8284' | grep -q .; then
  echo "TCP port 8284 is already in use; no services were changed." >&2
  exit 2
fi

controller_started=0
on_exit() {
  code=$?
  trap - EXIT
  if [ "$code" -ne 0 ] && [ "$controller_started" -eq 1 ]; then
    systemctl stop andrew-control.service >/dev/null 2>&1 || true
    systemctl disable andrew-control.service >/dev/null 2>&1 || true
    echo "Phase 1 activation stopped with status $code. Andrew controller was stopped if it had started; existing production services were not changed." >&2
  fi
  exit "$code"
}
trap on_exit EXIT

install -d -o root -g root -m 0755 "$TARGET/andrewbot" "$TARGET/tests" /etc/systemd/system/andrew-control.service.d
for file in "$SOURCE"/andrewbot/*.py; do
  install -o root -g root -m 0644 "$file" "$TARGET/andrewbot/"
done
for file in "$SOURCE"/tests/*.py; do
  install -o root -g root -m 0644 "$file" "$TARGET/tests/"
done

if [ ! -f "$GATEWAY_CREDENTIAL" ]; then
  openssl rand -hex 32 | systemd-creds encrypt --name=andrew-gateway-token --with-key=host - "$GATEWAY_CREDENTIAL" >/dev/null
  chown root:root "$GATEWAY_CREDENTIAL"
  chmod 0600 "$GATEWAY_CREDENTIAL"
fi
systemd-creds decrypt "$GATEWAY_CREDENTIAL" /dev/null >/dev/null

cat > "$CONTROL_DROPIN" <<'EOF'
[Service]
LoadCredentialEncrypted=andrew-gateway-token:/etc/credstore.encrypted/andrew-gateway-token
Environment=ANDREW_LETTA_URL=ws://127.0.0.1:8283/ws
Environment=ANDREW_GATEWAY_PORT=8284
Environment=ANDREW_MONTHLY_LIMIT_USD=10
Environment=ANDREW_MONTHLY_RESERVE_USD=1
Environment=ANDREW_DAILY_TARGET_USD=0.30
Environment=ANDREW_TIMEZONE=America/Toronto
Environment=ANDREW_FALLBACK_WAKE=09:00
EOF
chown root:root "$CONTROL_DROPIN"
chmod 0644 "$CONTROL_DROPIN"

export PYTHONPATH="$TARGET"
python3 -m unittest discover -s "$TARGET/tests" -p 'test_*.py' -v

systemctl daemon-reload
systemctl enable andrew-letta.service >/dev/null
systemctl start andrew-letta.service
curl -fsS --max-time 2 http://127.0.0.1:8283/healthz >/dev/null
controller_started=1
systemctl enable --now andrew-control.service

agent_ready=0
for _attempt in $(seq 1 30); do
  if systemctl is-active --quiet andrew-control.service && curl -fsS --max-time 2 http://127.0.0.1:8283/v1/models | python3 -c 'import json,sys; raise SystemExit(0 if json.load(sys.stdin).get("data") else 1)' >/dev/null 2>&1; then
    agent_ready=1
    break
  fi
  sleep 2
done
if [ "$agent_ready" -ne 1 ]; then
  echo "Andrew agent did not become ready; activation is stopping." >&2
  exit 1
fi

runuser -u andrew-control -- env \
  PYTHONPATH="$TARGET" ANDREW_DATA_DIR=/var/lib/andrew/control \
  ANDREW_LETTA_URL=ws://127.0.0.1:8283/ws \
  ANDREW_LETTA_SERVER_TEST=1 ANDREW_LETTA_INTEGRATION=1 \
  ANDREW_LETTA_INTEGRATION_STAGE=prepare \
  python3 -m unittest discover -s "$TARGET/tests" -p 'test_appserver.py' -v

systemctl restart andrew-letta.service
for _attempt in $(seq 1 30); do
  if curl -fsS --max-time 2 http://127.0.0.1:8283/healthz >/dev/null; then
    break
  fi
  sleep 2
done
curl -fsS --max-time 2 http://127.0.0.1:8283/healthz >/dev/null

runuser -u andrew-control -- env \
  PYTHONPATH="$TARGET" ANDREW_DATA_DIR=/var/lib/andrew/control \
  ANDREW_LETTA_URL=ws://127.0.0.1:8283/ws \
  ANDREW_LETTA_INTEGRATION=1 ANDREW_LETTA_INTEGRATION_STAGE=verify \
  python3 -m unittest discover -s "$TARGET/tests" -p 'test_appserver.py' -v

systemd-run --wait --pipe --collect --quiet --uid=andrew-control \
  --property=WorkingDirectory="$TARGET" \
  --property=EnvironmentFile=/etc/andrew/controller.conf \
  --property=Environment=PYTHONPATH="$TARGET" \
  --property=LoadCredentialEncrypted=andrew-vault-key:/etc/credstore.encrypted/andrew-vault-key \
  /usr/bin/python3 -m andrewbot.telegram_smoke

systemctl is-active --quiet andrew-control.service
systemctl is-active --quiet andrew-letta.service
systemctl is-enabled --quiet andrew-control.service
systemctl is-enabled --quiet andrew-letta.service

echo "Andrew Phase 1 controller and Letta agent are active and enabled for reboot."
echo "Telegram test message sent. Send Andrew a private Telegram message to begin a real conversation."
echo "Shell and browser tools remain disabled."

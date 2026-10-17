#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
SOURCE_DIR="$PROJECT_DIR/prototype/andrewbot"
INSTALL_DIR=/opt/andrew/prototype
LETTA_VERSION=0.34.13

if [[ ${EUID} -ne 0 ]]; then
  echo "Run with sudo: sudo bash $PROJECT_DIR/scripts/admin-setup.sh" >&2
  exit 1
fi
if [[ ! -f /etc/os-release ]] || ! grep -q '^VERSION_ID="24.04"' /etc/os-release; then
  echo "This setup script is scoped to the reviewed Ubuntu 24.04 host." >&2
  exit 1
fi
if systemctl is-active --quiet andrew-control.service; then
  echo 'Stop andrew-control.service before installing a controller package.' >&2
  exit 1
fi
if [[ ! -d "$SOURCE_DIR" ]]; then
  echo "Controller source not found at $SOURCE_DIR" >&2
  exit 1
fi
if [[ ! ${ANDREW_OWNER_ID:-} =~ ^[1-9][0-9]{0,19}$ ]]; then
  echo "Set ANDREW_OWNER_ID to Andrew's Telegram numeric owner ID before setup." >&2
  exit 1
fi
if ! command -v node >/dev/null || ! command -v npm >/dev/null; then
  echo "Node.js and npm are required for the pinned Letta CLI; install them separately, then rerun." >&2
  exit 1
fi

echo "Firewall changes are deliberately excluded. /etc/nftables.conf contains 'flush ruleset';"
echo "active nftables/UFW state must be captured and reviewed before any Andrew firewall proposal."

BASELINE="$(mktemp -d /root/andrew-admin-baseline.XXXXXX)"
chmod 0700 "$BASELINE"
snapshot_docker() {
  local destination="$1"
  if ! command -v docker >/dev/null; then
    : > "$destination"
    return
  fi
  while IFS= read -r id; do
    [[ -n "$id" ]] || continue
    docker inspect "$id" | python3 -c 'import json,sys; c=json.load(sys.stdin)[0]; mounts=sorted(({k:m.get(k) for k in ("Type","Source","Destination","RW","Propagation")} for m in c.get("Mounts",[])), key=lambda m:(m.get("Source") or "",m.get("Destination") or "")); print(json.dumps({"id":c["Id"],"name":c["Name"],"state":c["State"]["Status"],"ports":c["NetworkSettings"]["Ports"],"network":c["HostConfig"]["NetworkMode"],"mounts":mounts},sort_keys=True,separators=(",",":")))'
  done < <(docker ps -q --no-trunc | sort) | sort > "$destination"
}
if command -v docker >/dev/null; then
  snapshot_docker "$BASELINE/docker-containers.before"
  docker network ls --no-trunc --format '{{.ID}}|{{.Name}}|{{.Driver}}' | sort > "$BASELINE/docker-networks.before"
fi
for unit in docker ssh tailscaled; do
  systemctl is-active "$unit" > "$BASELINE/$unit.before" 2>/dev/null || true
  systemctl show "$unit" -p ActiveEnterTimestampMonotonic --value > "$BASELINE/$unit.timestamp.before" 2>/dev/null || true
done

export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=l
apt-get update
apt-get install --yes --no-install-recommends \
  podman crun uidmap slirp4netns fuse-overlayfs python3-cryptography make g++

# Ubuntu's Podman package enables rootful convenience units. Andrew uses rootless
# Podman only; keep those newly installed host-level sockets and timers disabled.
systemctl disable --now podman-auto-update.timer podman-auto-update.service \
  podman-clean-transient.service podman-restart.service podman.service podman.socket \
  >/dev/null 2>&1 || true

ensure_user() {
  local name="$1" home="$2"
  if ! getent passwd "$name" >/dev/null; then
    useradd --system --no-create-home --home-dir "$home" --shell /usr/sbin/nologin --user-group "$name"
  fi
}

ensure_user andrew-control /var/lib/andrew/control
ensure_user andrew-letta /var/lib/andrew/letta
ensure_user andrew-runtime /var/lib/andrew/runtime
ensure_user andrew-egress /var/lib/andrew/egress

ensure_subid_range() {
  local user="$1" file="$2" base end
  if awk -F: -v name="$user" '$1 == name { found=1 } END { exit !found }' "$file"; then
    return
  fi
  base="$(python3 - "$file" <<'PY'
import sys
path = sys.argv[1]
ranges = []
try:
    with open(path, encoding="utf-8") as stream:
        for line in stream:
            parts = line.strip().split(":")
            if len(parts) == 3:
                start, count = int(parts[1]), int(parts[2])
                ranges.append((start, start + count - 1))
except FileNotFoundError:
    pass
candidate = 200000
while any(candidate <= high and candidate + 65535 >= low for low, high in ranges):
    candidate += 65536
print(candidate)
PY
  )"
  end=$((base + 65535))
  usermod "--add-sub${file##*/sub}" "$base-$end" "$user"
}

ensure_subid_range andrew-letta /etc/subuid
ensure_subid_range andrew-letta /etc/subgid
ensure_subid_range andrew-runtime /etc/subuid
ensure_subid_range andrew-runtime /etc/subgid

install -d -o root -g root -m 0755 /etc/andrew /opt/andrew
install -d -o andrew-control -g andrew-control -m 0700 /var/lib/andrew/control /var/lib/andrew/control/backups /var/lib/andrew/vault
install -d -o andrew-letta -g andrew-letta -m 0700 /var/lib/andrew/letta /var/lib/andrew/letta-app
install -d -o andrew-runtime -g andrew-runtime -m 0700 /var/lib/andrew/runtime /srv/andrew/workspace
install -d -o andrew-egress -g andrew-egress -m 0700 /var/lib/andrew/egress
install -d -o root -g root -m 0755 /run/andrew
install -d -o root -g root -m 0700 /etc/credstore.encrypted

# Encrypt the persistent vault key with systemd's host key. The decrypted key
# is supplied only to a trusted systemd service credential directory.
VAULT_KEY=/var/lib/andrew/vault/vault.key
ENCRYPTED_VAULT_KEY=/etc/credstore.encrypted/andrew-vault-key
KEY_CHECK="$(mktemp /run/andrew/vault-key-check.XXXXXX)"
trap 'if [[ -n ${KEY_CHECK:-} ]]; then shred -u "$KEY_CHECK" 2>/dev/null || true; fi' EXIT
if [[ -e "$ENCRYPTED_VAULT_KEY" ]]; then
  systemd-creds decrypt "$ENCRYPTED_VAULT_KEY" "$KEY_CHECK"
  if [[ -e "$VAULT_KEY" ]] && ! cmp -s "$VAULT_KEY" "$KEY_CHECK"; then
    echo "Encrypted and plaintext vault keys do not match; refusing to continue." >&2
    exit 1
  fi
  if [[ -e "$VAULT_KEY" ]]; then
    shred -u "$VAULT_KEY"
  fi
else
  if [[ ! -e "$VAULT_KEY" ]]; then
    (umask 077; python3 -c 'import secrets,sys; sys.stdout.buffer.write(secrets.token_bytes(32))' > "$VAULT_KEY")
  fi
  systemd-creds setup
  systemd-creds encrypt --name=andrew-vault-key --with-key=host "$VAULT_KEY" "$ENCRYPTED_VAULT_KEY"
  chmod 0600 "$ENCRYPTED_VAULT_KEY"
  shred -u "$VAULT_KEY"
fi
shred -u "$KEY_CHECK"
KEY_CHECK=
python3 "$PROJECT_DIR/scripts/provision-controller-data-keys.py"
install -d -o root -g andrew-control -m 0750 "$INSTALL_DIR"
install -d -o root -g andrew-control -m 0750 "$INSTALL_DIR/andrewbot"
python3 "$PROJECT_DIR/scripts/install-controller-package.py" "$PROJECT_DIR" "$INSTALL_DIR/andrewbot"

printf 'ANDREW_DATA_DIR=/var/lib/andrew/control\nANDREW_VAULT_DIR=/var/lib/andrew/vault\nANDREW_OWNER_ID=%s\n' "$ANDREW_OWNER_ID" > /etc/andrew/controller.conf
chown root:root /etc/andrew/controller.conf
chmod 0644 /etc/andrew/controller.conf

install -o root -g root -m 0644 "$PROJECT_DIR/systemd/andrew-control.service" /etc/systemd/system/andrew-control.service
install -o root -g root -m 0644 "$PROJECT_DIR/systemd/andrew-letta.service" /etc/systemd/system/andrew-letta.service
install -o root -g root -m 0644 "$PROJECT_DIR/systemd/andrew-runtime.tmpfiles.conf" /etc/tmpfiles.d/andrew-runtime.conf
install -o root -g root -m 0755 "$PROJECT_DIR/scripts/andrew-vault-import" /usr/local/sbin/andrew-vault-import
install -o root -g root -m 0755 "$PROJECT_DIR/scripts/andrew-memory-backup" /usr/local/sbin/andrew-memory-backup
systemd-tmpfiles --create /etc/tmpfiles.d/andrew-runtime.conf

if [[ ! -x /var/lib/andrew/letta-app/node_modules/.bin/letta ]]; then
  runuser -u andrew-letta -- env HOME=/var/lib/andrew/letta \
    npm install --prefix /var/lib/andrew/letta-app --no-audit --no-fund \
    "@letta-ai/letta-code@$LETTA_VERSION"
fi
chown -R andrew-letta:andrew-letta /var/lib/andrew/letta /var/lib/andrew/letta-app

systemctl daemon-reload
systemctl enable --now andrew-letta.service

if command -v docker >/dev/null; then
  snapshot_docker "$BASELINE/docker-containers.after"
  docker network ls --no-trunc --format '{{.ID}}|{{.Name}}|{{.Driver}}' | sort > "$BASELINE/docker-networks.after"
  diff -u "$BASELINE/docker-containers.before" "$BASELINE/docker-containers.after"
  diff -u "$BASELINE/docker-networks.before" "$BASELINE/docker-networks.after"
fi
for unit in docker ssh tailscaled; do
  systemctl is-active "$unit" > "$BASELINE/$unit.after" 2>/dev/null || true
  systemctl show "$unit" -p ActiveEnterTimestampMonotonic --value > "$BASELINE/$unit.timestamp.after" 2>/dev/null || true
  if ! diff -q "$BASELINE/$unit.before" "$BASELINE/$unit.after" >/dev/null; then
    echo "Existing service state changed for $unit; stop here and investigate." >&2
    exit 1
  fi
  if ! diff -q "$BASELINE/$unit.timestamp.before" "$BASELINE/$unit.timestamp.after" >/dev/null; then
    echo "Existing service $unit appears to have restarted; stop here and investigate." >&2
    exit 1
  fi
done

echo "Administrator bootstrap complete. Existing Docker containers/networks and protected service states match baseline."
echo "Firewall was not changed. Andrew's shell/browser worker has not been started."
echo "Use systemd-run with LoadCredentialEncrypted to import credentials without exposing the vault key; see PHASE1_IMPLEMENTATION_PLAN.md."

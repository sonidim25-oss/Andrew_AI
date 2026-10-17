#!/usr/bin/env bash
# Already authorized by owner: no per-site or per-action approvals.
# Root is needed for dedicated accounts, the scoped firewall and cgroup setup.
set -Eeuo pipefail
umask 077
if [ "$#" -ne 0 ]; then echo "No arguments accepted." >&2; exit 2; fi
PROJECT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
# Service identities cannot traverse the invoking owner's private home. Keep
# their inherited cwd accessible; every checkout reference below is absolute.
cd -- /
if [ "$(id -u)" -ne 0 ]; then
  echo "Run from SSH: sudo bash $PROJECT/scripts/enable-browser-runtime.sh" >&2; exit 2
fi
for binary in podman nft ip mkfs.ext4 mount findmnt systemd-run loginctl python3 curl crun; do command -v "$binary" >/dev/null; done
if ! command -v setfacl >/dev/null; then apt-get update -qq; apt-get install -y --no-install-recommends acl; fi
for unit in andrew-control.service andrew-letta.service; do systemctl is-active --quiet "$unit"; done
MANIFEST="$PROJECT/runtime/browser-release.json"
ARCHIVE="$PROJECT/.artifacts/browser-runtime.oci.tar"
python3 - "$MANIFEST" "$ARCHIVE" <<'PY'
import hashlib,json,sys
from pathlib import Path
m=json.loads(Path(sys.argv[1]).read_text()); h=hashlib.sha256()
with open(sys.argv[2],'rb') as f:
 for block in iter(lambda:f.read(1024*1024),b''): h.update(block)
assert h.hexdigest()==m['archive_sha256'],'Image archive does not match release manifest'
assert m['image'].startswith('localhost/andrew-browser@sha256:')
for relative,digest in m['sources'].items():
 assert hashlib.sha256((Path(sys.argv[1]).parents[1]/relative).read_bytes()).hexdigest()==digest,'Image source changed; rebuild release'
print('Pinned browser/worker archive verified; no credentials accessed.')
PY
IMAGE="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["image"])' "$MANIFEST")"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="/var/lib/andrew/runtime-commissioning/$STAMP"
install -d -o root -g root -m 0700 "$BACKUP"
# Preserve only Andrew-owned files; never restore a whole host ruleset.
managed=(/etc/andrew/runner.conf /etc/andrew/egress.conf /etc/andrew/runtime-proof.json
 /etc/andrew/runner-environment.conf /etc/andrew/browser-environment.conf /etc/andrew/browser-runtime.conf
 /etc/systemd/system/andrew-runner.service.d/50-rootless-runtime.conf
 /etc/systemd/system/andrew-browser.service.d/50-rootless-runtime.conf
 /etc/nftables.d/andrew-runtime.nft /etc/systemd/system/andrew-control.service
 /etc/systemd/system/andrew-control.service.d/99-browser-runtime.conf)
for name in runner browser egress; do
  managed+=("/etc/systemd/system/andrew-$name.service" "/etc/systemd/system/andrew-$name.socket")
done
managed+=(/etc/systemd/system/andrew-runtime-firewall.service)
for file in "${managed[@]}"; do
  if [ -e "$file" ]; then cp --parents -a "$file" "$BACKUP/"; fi
done
nft -s -j list ruleset > "$BACKUP/firewall-before.json"
if nft list table inet andrew_runtime > "$BACKUP/own-firewall.nft" 2>/dev/null; then :; else rm -f "$BACKUP/own-firewall.nft"; fi
systemctl is-enabled andrew-runtime-firewall.service > "$BACKUP/firewall-enabled" 2>/dev/null || true
for unit in andrew-runner.socket andrew-browser.socket andrew-egress.socket; do
 systemctl is-enabled "$unit" > "$BACKUP/$unit.enabled" 2>/dev/null || true
done
for unit in andrew-runner.socket andrew-browser.socket andrew-egress.socket andrew-runner.service andrew-browser.service andrew-egress.service andrew-runtime-firewall.service; do
 systemctl is-active "$unit" > "$BACKUP/$unit.active" 2>/dev/null || true
done
controller_stopped=0
success=0
check='identity provisioning'
rollback() {
  code=$?
  trap - EXIT
  set +e
  restore_failed=0
  if [ "$success" -eq 0 ]; then
    printf 'Commissioning failed at safe check: %s (status %s).\n' "${check:-unspecified}" "$code" >&2
    systemctl stop andrew-runner.socket andrew-browser.socket andrew-egress.socket \
      andrew-runner.service andrew-browser.service andrew-egress.service andrew-runtime-firewall.service >/dev/null 2>&1 || true
    if [ "$controller_stopped" -eq 1 ]; then systemctl stop andrew-control.service >/dev/null 2>&1 || true; fi
    for file in "${managed[@]}"; do
      if [ -e "$BACKUP$file" ]; then cp -a "$BACKUP$file" "$file" || restore_failed=1; else rm -f "$file" || restore_failed=1; fi
    done
    if [ -f "$BACKUP/controller-assets.json" ]; then
      python3 "$PROJECT/scripts/install-controller-package.py" --restore-backup "$BACKUP/controller-assets.json" || restore_failed=1
    elif [ -d "$BACKUP/controller-package" ]; then
      rm -rf /opt/andrew/prototype/andrewbot
      cp -a "$BACKUP/controller-package" /opt/andrew/prototype/andrewbot || restore_failed=1
    fi
    if [ -d "$BACKUP/service-package" ]; then
      rm -rf /opt/andrew/runtime-services/andrewbot
      cp -a "$BACKUP/service-package" /opt/andrew/runtime-services/andrewbot || restore_failed=1
    fi
    nft delete table inet andrew_runtime >/dev/null 2>&1 || true
    if [ -f "$BACKUP/own-firewall.nft" ]; then nft -f "$BACKUP/own-firewall.nft" || restore_failed=1; fi
    systemctl daemon-reload
    for unit in andrew-runner.socket andrew-browser.socket andrew-egress.socket; do
      if [ "$(cat "$BACKUP/$unit.enabled")" = enabled ]; then systemctl enable "$unit" >/dev/null; else systemctl disable "$unit" >/dev/null 2>&1 || true; fi
    done
    if [ "$(cat "$BACKUP/firewall-enabled")" != enabled ]; then systemctl disable andrew-runtime-firewall.service >/dev/null 2>&1 || true; fi
    for unit in andrew-runtime-firewall.service andrew-egress.socket andrew-runner.socket andrew-browser.socket andrew-egress.service andrew-runner.service andrew-browser.service; do
      if [ -f "$BACKUP/$unit.active" ] && [ "$(cat "$BACKUP/$unit.active")" = active ]; then systemctl start "$unit" || restore_failed=1; fi
    done
    if [ "$controller_stopped" -eq 1 ]; then systemctl restart andrew-control.service || restore_failed=1; fi
    if [ "$restore_failed" -eq 0 ]; then
      echo "Commissioning failed; previous controller files and flags restored. Private evidence: $BACKUP" >&2
    else
      echo "Commissioning failed and rollback needs administrator inspection: $BACKUP" >&2
    fi
    echo 'Existing memory, ledger and lifecycle state were preserved. New dedicated storage/accounts retained for retry.' >&2
  fi
  exit "$code"
}
trap rollback EXIT
ensure_identity() {
  local name="$1" home_dir="$2"
  if ! id "$name" >/dev/null 2>&1; then useradd --system --no-create-home --home-dir "$home_dir" --shell /usr/sbin/nologin --user-group "$name"; fi
  [ "$(getent passwd "$name" | cut -d: -f6)" = "$home_dir" ]
  install -d -o "$name" -g "$name" -m 0700 "$home_dir"
}
ensure_identity andrew-runtime /var/lib/andrew/runtime
ensure_identity andrew-browser /var/lib/andrew/browser
ensure_identity andrew-egress /var/lib/andrew/egress
getent group andrew-egress-access >/dev/null || groupadd --system andrew-egress-access
usermod -aG andrew-egress-access andrew-runtime
usermod -aG andrew-egress-access andrew-browser
for name in andrew-runtime andrew-browser; do
 for task_group in $(id -Gn "$name"); do
  case "$task_group" in "$name"|andrew-egress-access) ;; *) echo 'Unexpected launcher group; refusing to inherit it.' >&2; exit 1;; esac
 done
done
# Reserve non-overlapping subordinate maps only for the two Andrew launchers.
python3 - <<'PY'
from pathlib import Path
for file in ('/etc/subuid','/etc/subgid'):
 p=Path(file); text=p.read_text(); lines=[v.split(':') for v in text.splitlines() if v]
 end=max([100000]+[int(v[1])+int(v[2]) for v in lines])
 for name in ('andrew-runtime','andrew-browser'):
  if not any(v[0]==name for v in lines):
   text+=f'{name}:{end}:65536\n'; end+=65536
 p.write_text(text)
PY
install -d -o root -g root -m 0755 /opt/andrew/runtime-services /etc/andrew /etc/nftables.d /run/andrew
if [ -d /opt/andrew/runtime-services/andrewbot ]; then cp -a /opt/andrew/runtime-services/andrewbot "$BACKUP/service-package"; fi
install -d -o root -g root -m 0755 /opt/andrew/runtime-services/andrewbot /opt/andrew/runtime-services/checks
for file in "$PROJECT"/prototype/andrewbot/*.py; do install -o root -g root -m 0644 "$file" /opt/andrew/runtime-services/andrewbot/; done
for file in verify-runtime.py verify-browser-fixture.py verify-worker-launch.py; do install -o root -g root -m 0644 "$PROJECT/scripts/$file" /opt/andrew/runtime-services/checks/; done
IMPORTED_IMAGE=''
for name in andrew-runtime andrew-browser; do
  task_uid="$(id -u "$name")"
  check="rootless user manager: $name"
  loginctl enable-linger "$name"
  systemctl start "user@$task_uid.service"
  # Import with the proper rootless identity, HOME and runtime bus.
  task_home="$(getent passwd "$name" | cut -d: -f6)"
  # runuser cannot traverse the private checkout; root streams the pinned archive.
  check="pinned image import: $name"
  cat "$ARCHIVE" | runuser -u "$name" -- env HOME="$task_home" XDG_RUNTIME_DIR="/run/user/$task_uid" \
    DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$task_uid/bus" podman load >/dev/null
  check="imported image content identity: $name"
  imported_reference="$(runuser -u "$name" -- env HOME="$task_home" XDG_RUNTIME_DIR="/run/user/$task_uid" \
    podman image inspect localhost/andrew-browser:integration | python3 "$PROJECT/scripts/verify-imported-image.py" "$MANIFEST")"
  runuser -u "$name" -- env HOME="$task_home" XDG_RUNTIME_DIR="/run/user/$task_uid" \
    podman image exists "$imported_reference"
  if [ -n "$IMPORTED_IMAGE" ] && [ "$IMPORTED_IMAGE" != "$imported_reference" ]; then
    echo 'Dedicated stores disagree on the imported immutable image reference.' >&2; exit 1
  fi
  IMPORTED_IMAGE="$imported_reference"
done
IMAGE="$IMPORTED_IMAGE"
check='dedicated projects filesystem'
[ "$(df --output=avail -B1 /var/lib/andrew | tail -1)" -gt 12884901888 ]
# A real filesystem capacity limit; never cover a populated directory.
install -d -o root -g root -m 0755 /srv/andrew /srv/andrew/workspace /srv/andrew/workspace/projects
if ! mountpoint -q /srv/andrew/workspace/projects; then
  if [ -n "$(ls -A /srv/andrew/workspace/projects)" ]; then echo 'Projects directory is populated; refusing to hide it.' >&2; exit 1; fi
  if [ ! -e /var/lib/andrew/projects.ext4 ]; then
    truncate -s 10G /var/lib/andrew/projects.ext4
    chmod 0600 /var/lib/andrew/projects.ext4
    mkfs.ext4 -q -m 0 /var/lib/andrew/projects.ext4
  fi
fi
[ "$(stat -c %s /var/lib/andrew/projects.ext4)" -eq 10737418240 ]
install -o root -g root -m 0644 "$PROJECT/systemd/srv-andrew-workspace-projects.mount" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now srv-andrew-workspace-projects.mount >/dev/null
findmnt -rn -T /srv/andrew/workspace/projects -o FSTYPE | grep -qx ext4
[ "$(findmnt -rn -T /srv/andrew/workspace/projects -o TARGET)" = /srv/andrew/workspace/projects ]
chown andrew-runtime:andrew-runtime /srv/andrew/workspace/projects
chmod 0750 /srv/andrew/workspace/projects
# Controller can read model-selected uploads, but browser launcher cannot read projects.
setfacl -m u:andrew-control:rx,d:u:andrew-control:rx,d:m:rwx /srv/andrew/workspace/projects
install -d -o andrew-browser -g andrew-browser -m 0700 /var/lib/andrew/browser/projects
printf 'IMAGE=%s\nWORKSPACE=/srv/andrew/workspace/projects\nPROXY_SOCKET=/run/andrew/egress.sock\n' "$IMAGE" > /etc/andrew/runner.conf
for role in runner browser; do
  name=andrew-runtime; [ "$role" = browser ] && name=andrew-browser
  task_uid="$(id -u "$name")"
  install -d -o root -g root -m 0755 "/etc/systemd/system/andrew-$role.service.d"
  printf '[Service]\nBindPaths=/run/user/%s\nReadWritePaths=/run/user/%s\n' "$task_uid" "$task_uid" > "/etc/systemd/system/andrew-$role.service.d/50-rootless-runtime.conf"
  chmod 0644 "/etc/systemd/system/andrew-$role.service.d/50-rootless-runtime.conf"
  printf 'XDG_RUNTIME_DIR=/run/user/%s\nDBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/%s/bus\n' "$task_uid" "$task_uid" > "/etc/andrew/$role-environment.conf"
done
check='public network exclusions'
ip -j address show > "$BACKUP/addresses.json"
# Include this host's public NAT address, which may not appear on an interface.
curl -fsS --max-time 15 --max-filesize 1024 https://api.ipify.org > "$BACKUP/public-address"
python3 - "$BACKUP" <<'PY'
import ipaddress,json,sys
from pathlib import Path
p=Path(sys.argv[1]); ip=ipaddress.ip_address((p/'public-address').read_text().strip())
a=json.loads((p/'addresses.json').read_text()); a.append({'addr_info':[{'local':str(ip),'prefixlen':ip.max_prefixlen}]})
(p/'addresses.json').write_text(json.dumps(a))
PY
python3 "$PROJECT/scripts/build-runtime-policy.py" --runtime-uid "$(id -u andrew-runtime)" \
 --browser-uid "$(id -u andrew-browser)" --egress-uid "$(id -u andrew-egress)" \
 --addresses "$BACKUP/addresses.json" --dns /etc/resolv.conf \
 --firewall /etc/nftables.d/andrew-runtime.nft --proxy /etc/andrew/egress.conf
chmod 0644 /etc/andrew/{runner,egress,runner-environment,browser-environment}.conf /etc/nftables.d/andrew-runtime.nft
nft add table inet andrew_runtime 2>/dev/null || true
check='dedicated firewall syntax'
nft -c -f /etc/nftables.d/andrew-runtime.nft
for unit in andrew-runner.service andrew-runner.socket andrew-browser.service andrew-browser.socket andrew-egress.service andrew-egress.socket andrew-runtime-firewall.service; do
 install -o root -g root -m 0644 "$PROJECT/systemd/$unit" /etc/systemd/system/
done
systemctl daemon-reload
check='dedicated firewall activation'
systemctl restart andrew-runtime-firewall.service
systemctl restart andrew-runner.socket andrew-browser.socket andrew-egress.socket
check='protected rootless startup inspection'
bash "$PROJECT/scripts/diagnose-browser-runtime.sh" | tee "$BACKUP/startup-results.json"
# Run kernel, network, OOM, PID, CPU and 120-second wall-clock proofs through
# the actual protected sockets, which launch as the installed service identities.
check='live kernel and browser acceptance'
echo 'Running bounded kernel/browser commissioning; the wall-clock check takes 120 seconds...'
systemd-run --wait --pipe --collect --quiet --unit=andrew-runtime-acceptance --uid=andrew-control --gid=andrew-control \
 --property=WorkingDirectory=/opt/andrew/runtime-services --property=Environment=PYTHONPATH=/opt/andrew/runtime-services \
 /usr/bin/python3 /opt/andrew/runtime-services/checks/verify-runtime.py | tee "$BACKUP/kernel-results.json"
# Account/component fixture has its own ephemeral self-signed loopback server.
# The certificate exception and privileged test port sysctl never enter production.
check='controlled browser and account fixture'
browser_uid="$(id -u andrew-browser)"
runuser -u andrew-browser -- env HOME=/var/lib/andrew/browser XDG_RUNTIME_DIR="/run/user/$browser_uid" \
 DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$browser_uid/bus" \
 podman --runtime=/usr/bin/crun run --rm --timeout=120 --log-driver=none --pull=never --network=none --sysctl net.ipv4.ip_unprivileged_port_start=0 --read-only \
 --user=1000:1000 --cap-drop=ALL --security-opt=no-new-privileges --memory=2g --cpus=2 --pids-limit=256 \
 --env HOME=/tmp --tmpfs /tmp:rw,nosuid,nodev,size=256m \
 --mount type=bind,src=/opt/andrew/runtime-services/checks/verify-browser-fixture.py,dst=/opt/andrew/verify-browser-fixture.py,ro=true \
 --entrypoint python3 "$IMAGE" /opt/andrew/verify-browser-fixture.py | tee "$BACKUP/browser-fixture.json"
# Dedicated host UIDs must fail direct host-network connections independently of namespaces.
check='dedicated UID network isolation'
for name in andrew-runtime andrew-browser; do
 runuser -u "$name" -- python3 -c 'import socket
try: socket.create_connection(("1.1.1.1",443),2)
except OSError: raise SystemExit(0)
raise SystemExit("UID firewall did not block direct networking")'
done
# Real pinned SDK harness, isolated temporary backend, deterministic unbilled provider.
check='isolated Letta browser callbacks'
ANDREW_LETTA_TEST_BINARY=/var/lib/andrew/letta-app/node_modules/@letta-ai/letta-code/letta.js \
 python3 -m unittest discover -s "$PROJECT/scripts/tests" -p test_browser_protocol.py -q
# Require every installed module and every browser-image source to match this checkout.
for file in "$PROJECT"/prototype/andrewbot/*.py; do cmp -s "$file" "/opt/andrew/runtime-services/andrewbot/${file##*/}"; done
# Snapshot and briefly stop only the controller; do not restart Letta or reset state.
check='controller installation and persistent state'
cp -a /opt/andrew/prototype/andrewbot "$BACKUP/controller-package"
controller_stopped=1
systemctl stop andrew-control.service
python3 "$PROJECT/scripts/verify-runtime-state.py" capture "$BACKUP/controller-state.json"
install -d -o root -g andrew-control -m 0750 /opt/andrew/prototype /opt/andrew/prototype/andrewbot
python3 "$PROJECT/scripts/install-controller-package.py" "$PROJECT" /opt/andrew/prototype/andrewbot --backup "$BACKUP/controller-assets.json"
# Root-owned, code/image/policy-bound proof written only after all acceptance commands succeed.
check='administrator acceptance proof'
PYTHONPATH=/opt/andrew/prototype python3 - "$BACKUP" <<'PY'
import json,os,sys
from datetime import datetime,timezone
from pathlib import Path
from andrewbot.runtime_readiness import bindings,CHECKS
proof={**bindings(),'schema':1,'checks':{k:'passed' for k in CHECKS},'verified_at':datetime.now(timezone.utc).isoformat(),'evidence':sys.argv[1]}
p=Path('/etc/andrew/runtime-proof.json'); temp=p.with_suffix('.tmp')
temp.write_text(json.dumps(proof,indent=2)+'\n'); temp.chmod(0o644); os.replace(temp,p)
PY
printf 'ANDREW_ISOLATED_RUNTIME_ENABLED=1\n' > /etc/andrew/browser-runtime.conf
chmod 0644 /etc/andrew/browser-runtime.conf
install -d -o root -g root -m 0755 /etc/systemd/system/andrew-control.service.d
printf '[Service]\nEnvironmentFile=/etc/andrew/browser-runtime.conf\nReadOnlyPaths=/srv/andrew/workspace/projects\n' > /etc/systemd/system/andrew-control.service.d/99-browser-runtime.conf
chmod 0644 /etc/systemd/system/andrew-control.service.d/99-browser-runtime.conf
check='controller startup validation'
bash "$PROJECT/scripts/validate-and-start-controller.sh" --validate-only
python3 "$PROJECT/scripts/verify-runtime-state.py" verify "$BACKUP/controller-state.json"
systemctl daemon-reload
systemctl restart andrew-control.service
for _attempt in $(seq 1 60); do
 invocation="$(systemctl show andrew-control.service -p InvocationID --value)"
 if systemctl is-active --quiet andrew-control.service && journalctl --quiet "_SYSTEMD_INVOCATION_ID=$invocation" --no-pager -o cat --grep='Andrew Telegram controller started' | grep -q 'Andrew Telegram controller started'; then break; fi
 sleep 1
done
systemctl is-active --quiet andrew-control.service
journalctl --quiet "_SYSTEMD_INVOCATION_ID=$invocation" --no-pager -o cat --grep='Andrew Telegram controller started' | grep -q 'Andrew Telegram controller started'
# Compare all unrelated firewall objects, omitting volatile counters/handles.
check='unrelated firewall preservation'
nft -s -j list ruleset > "$BACKUP/firewall-after.json"
python3 - "$BACKUP" <<'PY'
import json,sys
from pathlib import Path
root=Path(sys.argv[1])
def clean(x):
 if isinstance(x,list): return [clean(v) for v in x if not any(isinstance(o,dict) and (o.get('table')=='andrew_runtime' or (o.get('name')=='andrew_runtime' and 'family' in o)) for o in v.values())]
 if isinstance(x,dict): return {k:clean(v) for k,v in x.items() if k not in ('handle','packets','bytes')}
 return x
assert clean(json.loads((root/'firewall-before.json').read_text()))==clean(json.loads((root/'firewall-after.json').read_text())),'Unrelated firewall state changed'
PY
systemctl enable andrew-runtime-firewall.service andrew-runner.socket andrew-browser.socket andrew-egress.socket >/dev/null
success=1
printf 'Browser/code runtime enabled with owner-authorized logins/signups, no per-action approval prompts.\nPrivate acceptance evidence: %s\nExisting daily schedule, pause, memory and ledger preserved.\n' "$BACKUP"

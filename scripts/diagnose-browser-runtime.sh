#!/usr/bin/env bash
# Targeted Podman startup diagnostics; no credentials loaded or model calls.
set -Eeuo pipefail
umask 077
cd -- /
if [ "$#" -ne 0 ] || [ "$(id -u)" -ne 0 ]; then
  echo 'Run with sudo and no arguments.' >&2; exit 2
fi
read -r -d '' PROBE <<'PY' || true
import json,subprocess,os,signal,sys
from pathlib import Path
status=dict(line.split(':',1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
command=['/usr/bin/podman','ps','-aq','--filter','label=org.andrew.runtime=managed']
process=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,start_new_session=True)
try:
 _,error=process.communicate(timeout=20)
except subprocess.TimeoutExpired:
 os.killpg(process.pid,signal.SIGKILL); _,error=process.communicate()
 error=b'podman startup inspection timed out'
# This fixed local metadata command receives no credentials or request content.
result={'uid':os.getuid(),'no_new_privileges':status.get('NoNewPrivs','').strip(),
 'seccomp':status.get('Seccomp','').strip(),'podman_status':process.returncode,
 'podman_startup_error':error.decode('utf-8',errors='replace')[:3000]}
if sys.argv[1]=='protected':
 try:
  result['other_homes_hidden']=all(not any(Path(p).iterdir()) for p in ('/home','/root'))
  result['only_own_runroot_visible']={p.name for p in Path('/run/user').iterdir()}=={str(os.getuid())}
  result['own_runroot_writable']=os.access(f'/run/user/{os.getuid()}',os.W_OK)
 except OSError:
  result['isolation_inspection_failed']=True
print(json.dumps(result))
raise SystemExit(bool(process.returncode) or (sys.argv[1]=='protected' and
 not all(result.get(k) for k in ('other_homes_hidden','only_own_runroot_visible','own_runroot_writable'))))
PY
for role in runtime browser; do
 name="andrew-$role"
 task_uid="$(id -u "$name")"
 task_home="$(getent passwd "$name" | cut -d: -f6)"
 for mode in baseline protected; do
  printf 'Andrew startup diagnostic: %s / %s\n' "$role" "$mode"
  options=(--uid="$name" --gid="$name" --property=WorkingDirectory=/opt/andrew/runtime-services
   --setenv="HOME=$task_home" --setenv="XDG_RUNTIME_DIR=/run/user/$task_uid"
   --setenv="DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$task_uid/bus"
   --property=UMask=0077 --property=RuntimeMaxSec=30)
  if [ "$mode" = protected ]; then
   writable="$task_home /sys/fs/cgroup /run/user/$task_uid"
   [ "$role" != runtime ] || writable+=" /srv/andrew/workspace/projects"
   options+=(--property=PrivateTmp=yes --property=ProtectSystem=strict --property=ProtectHome=tmpfs
    --property="BindPaths=/run/user/$task_uid"
    --property=ProtectKernelTunables=yes --property=ProtectKernelModules=yes --property=LockPersonality=yes
    --property=Delegate=yes --property="ReadWritePaths=$writable"
    --property=MemoryMax=3G --property=CPUQuota=250% --property=TasksMax=512 --property=LimitNOFILE=2048)
  fi
  systemd-run --wait --pipe --collect --quiet --unit="andrew-startup-diagnostic-$role-$mode" \
    "${options[@]}" /usr/bin/python3 -c "$PROBE" "$mode"
 done
done

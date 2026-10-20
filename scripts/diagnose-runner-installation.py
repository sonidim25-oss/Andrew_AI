#!/usr/bin/env python3
"""Read-only worker inventory in two working directories; never starts a worker."""
import json
import os
from pathlib import Path
import pwd
import subprocess
import sys


def main():
    if os.geteuid() != 0:
        raise SystemExit('Run with sudo to inspect the runtime service account.')
    directory = Path('/var/backups/andrew-runner-recovery-candidate-v2')
    phase = json.loads((directory / 'installation.json').read_text()).get('phase')
    print(json.dumps(dict(phase=phase,
        code_backup_exists=(directory / 'code-before').exists(),
        paired_snapshot_exists=(directory / 'paired-before').exists())), flush=True)
    account = pwd.getpwnam('andrew-runtime')
    environment = {**os.environ, 'HOME': account.pw_dir,
        'XDG_RUNTIME_DIR': '/run/user/' + str(account.pw_uid),
        'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/run/user/' + str(account.pw_uid) + '/bus'}
    command = ['/usr/bin/podman', 'ps', '-aq', '--filter', 'label=org.andrew.runtime=managed']
    for label, cwd in [('caller_directory', os.getcwd()), ('service_directory', '/opt/andrew/runtime-services')]:
        try:
            result = subprocess.run(command, cwd=cwd, env=environment,
                user=account.pw_uid, group=account.pw_gid,
                extra_groups=os.getgrouplist(account.pw_name, account.pw_gid),
                stdin=subprocess.DEVNULL, capture_output=True, timeout=20)
            output = dict(probe=label, cwd=cwd, exit_status=result.returncode,
                worker_count=len(result.stdout.splitlines()),
                error=result.stderr.decode('utf-8', errors='replace')[:2000])
        except (OSError, subprocess.TimeoutExpired) as error:
            output = dict(probe=label, cwd=cwd, error_type=type(error).__name__)
        print(json.dumps(output), flush=True)


if __name__ == '__main__':
    main()

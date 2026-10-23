#!/usr/bin/env python3
"""Preview the selected voice, deploy support, then set the owner's default."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import pwd
import subprocess
import time

import budget_deployment as rollout
from owner_voice_preview import VOICE, MODEL, DIRECTORY

rollout.ARTIFACTS = Path('/home/daniel/andrew-voice-preflight-20261023')
rollout.CANDIDATE = Path('/var/backups/andrew-owner-voice-20261023-candidate')
rollout.BACKEND_SHA = 'cd367258421ab6d4ed44e417c2dcc354b4c032795efb96ad80b0605d8a771e08'
rollout.BACKEND = Path('/opt/andrew-letta-recovery/releases') / rollout.BACKEND_SHA
rollout.BASELINE = {
    'mode': 'enabled',
    'controller_code': '167b77cab9b70b2372a36c066f9bee190757b8202195bf125828889309f923a1',
    'service_code': '5902ee5a385e07b97823c7065208115d0a4d2e347cd308b5e6161ce0636f60c6',
    'backend_release_sha256': rollout.BACKEND_SHA, 'backend_release': str(rollout.BACKEND),
}
rollout.EXPECTED_CONTROLLER = '31bd1654d313c5ff20cd8be5833922a1fbfdbf4cc6570ef55f1539a02b10e581'
rollout.EXPECTED_SERVICE = rollout.BASELINE['service_code']


def paused_owner():
    return rollout.require_pause(rollout.DATABASE, backend_home='/var/lib/andrew/letta',
                                 runner_root='/var/lib/andrew/runtime/runner-receipts')


def restart_controller():
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', 'restart', 'andrew-control.service'], check=True)
    subprocess.run(['systemctl', 'is-active', '--quiet', 'andrew-control.service'], check=True)


def effective_voice():
    # Type=simple may report active before the executor has applied EnvironmentFile.
    # Observe the live process briefly before rejecting and restoring the config.
    value = ''
    for _ in range(20):
        pid = int(subprocess.check_output(['systemctl', 'show', 'andrew-control.service',
                                           '--property=MainPID', '--value'], text=True).strip())
        if pid > 0:
            try:
                environment = dict(item.split(b'=', 1) for item in Path('/proc', str(pid), 'environ').read_bytes().split(b'\0') if b'=' in item)
                value = environment.get(b'ANDREW_TTS_VOICE', b'').decode()
                if value == VOICE:
                    return value
            except FileNotFoundError:
                pass  # The executor or a restarting process has just exited.
        time.sleep(.25)
    return value


def configure_voice(owner, environment_file, dropin, write):
    if paused_owner() != owner:
        raise ValueError('Owner state changed before voice activation')
    paths = [Path(environment_file), Path(dropin)]
    previous = {path: path.read_bytes() if path.exists() else None for path in paths}
    try:
        write(paths[0], ('ANDREW_TTS_VOICE=' + VOICE + '\n').encode(), 0o600)
        write(paths[1], ('[Service]\nEnvironmentFile=' + str(paths[0]) + '\n').encode(), 0o644)
        restart_controller()
        if effective_voice() != VOICE or paused_owner() != owner:
            raise ValueError('Effective voice or owner state differs')
    except Exception:
        for path, content in previous.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                write(path, content, 0o600 if path == paths[0] else 0o644)
        restart_controller()
        raise ValueError('Voice configuration failed; previous configuration restored') from None


def approved_preview(installer):
    stage = Path('/opt/andrew-owner-voice-preview-20261023')
    stage.mkdir(mode=0o755, exist_ok=True)
    installer._trusted_directory(stage)
    script = stage / 'owner_voice_preview.py'
    installer._write(script, (rollout.ROOT / 'scripts/owner_voice_preview.py').read_bytes(), 0o644)
    result = subprocess.run([
        'systemd-run', '--wait', '--pipe', '--collect', '--quiet',
        '--unit=andrew-owner-voice-preview-20261023', '--uid=andrew-control', '--gid=andrew-control',
        '--property=Type=oneshot', '--property=TimeoutStartSec=180',
        '--property=WorkingDirectory=/opt/andrew/prototype',
        '--property=Environment=PYTHONPATH=/opt/andrew/prototype', '--property=UMask=0077',
        '--property=LoadCredentialEncrypted=andrew-vault-key:/etc/credstore.encrypted/andrew-vault-key',
        '/usr/bin/python3', str(script)], check=True, capture_output=True, text=True)
    report = json.loads(result.stdout)
    if (report.get('voice') != VOICE or report.get('model') != MODEL or report.get('state') != 'succeeded'
            or report.get('artifact') != str(DIRECTORY / 'sample.mp3')):
        raise ValueError('Voice preview receipt differs')
    read = installer.helper('inspect_owner_handoff').read
    audio = read(report['artifact'])
    if hashlib.sha256(audio).hexdigest() != report['sha256']:
        raise ValueError('Voice preview audio differs')
    probe = subprocess.run(['runuser', '-u', 'andrew-control', '--', '/usr/bin/ffprobe',
                            '-v', 'error', '-show_entries', 'format=duration', '-of', 'json',
                            report['artifact']], check=True, capture_output=True, text=True)
    duration = float(json.loads(probe.stdout)['format']['duration'])
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('Voice preview contains no playable audio')
    destination = Path('/home/daniel/andrew-selected-voice-20261023.mp3')
    if destination.exists() or destination.is_symlink():
        if hashlib.sha256(read(destination)).hexdigest() != report['sha256']:
            raise ValueError('Owner preview destination contains different audio')
    else:
        account = pwd.getpwnam('daniel')
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'wb') as output:
            os.fchown(output.fileno(), account.pw_uid, account.pw_gid)
            output.write(audio)
            output.flush()
            os.fsync(output.fileno())
    print(json.dumps({'voice_preview': 'verified', 'voice': VOICE, 'sample': str(destination)}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expected-commit', required=True)
    parser.add_argument('--preview-only', action='store_true')
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise ValueError('Run this setup with sudo after pausing Andrew')
    rollout.verify_source(args.expected_commit)
    rollout.verify_baseline(json.loads(rollout.ADOPTION.read_text()))
    owner = paused_owner()
    rollout.require_headroom(rollout.DATABASE, '/var/lib/andrew/letta', '/var/lib/andrew/runtime/runner-receipts')
    installer = rollout.load_installer()
    approved_preview(installer)
    if args.preview_only:
        return
    if paused_owner() != owner:
        raise ValueError('Owner state changed during voice preview')
    rollout.deploy(args.expected_commit)
    environment_file = Path('/etc/andrew/owner-voice.conf')
    dropin = Path('/etc/systemd/system/andrew-control.service.d/zzzz-owner-voice.conf')
    installer._write(rollout.CANDIDATE / 'voice-config-before.json',
                     {str(path): path.read_text() if path.exists() else None for path in (environment_file, dropin)})
    configure_voice(owner, environment_file, dropin, installer._write)
    print(json.dumps({'voice': VOICE, 'model': MODEL, 'configured': True, 'paused': True,
                      'preview': '/home/daniel/andrew-selected-voice-20261023.mp3'}), flush=True)


if __name__ == '__main__':
    main()

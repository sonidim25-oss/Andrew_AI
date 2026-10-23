#!/usr/bin/env python3
"""Deploy flexible diary reflection over the exact inspected paused release."""
from pathlib import Path
import budget_deployment as rollout

rollout.ARTIFACTS = Path('/home/daniel/andrew-diary-preflight-20261023')
rollout.CANDIDATE = Path('/var/backups/andrew-flexible-diary-20261023-candidate')
rollout.BASELINE = {'backend_release': '/opt/andrew-letta-recovery/releases/cd367258421ab6d4ed44e417c2dcc354b4c032795efb96ad80b0605d8a771e08', 'backend_release_sha256': 'cd367258421ab6d4ed44e417c2dcc354b4c032795efb96ad80b0605d8a771e08', 'controller_code': 'a6c974edef7a6a95853b7c1c10bde235d32e7fe725680ec8c4c40cf4e77fe1a0', 'mode': 'enabled', 'service_code': '5902ee5a385e07b97823c7065208115d0a4d2e347cd308b5e6161ce0636f60c6'}
rollout.BACKEND_SHA = rollout.BASELINE['backend_release_sha256']
rollout.BACKEND = Path(rollout.BASELINE['backend_release'])
rollout.EXPECTED_CONTROLLER = '88db21a7152e1996a95d357742d070d3ce13ba008e3b15b4af28b5ae412a13ff'
rollout.EXPECTED_SERVICE = rollout.BASELINE['service_code']

if __name__ == '__main__':
    rollout.main()

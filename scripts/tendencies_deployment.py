#!/usr/bin/env python3
"""Deploy owner-suggested guidance over the installed memory/timezone release."""
from pathlib import Path

import budget_deployment as rollout

rollout.ARTIFACTS = Path('/home/daniel/andrew-tendencies-current-preflight-20261022')
rollout.CANDIDATE = Path('/var/backups/andrew-flexible-tendencies-memory-20261022-candidate')
rollout.BASELINE = {
    'mode': 'enabled',
    'controller_code': '4aee0fc2f9452db9ec643a7e88c55369ea0a4e444b339e55412a9a05d0b08518',
    'service_code': '5902ee5a385e07b97823c7065208115d0a4d2e347cd308b5e6161ce0636f60c6',
    'backend_release_sha256': rollout.BACKEND_SHA,
    'backend_release': str(rollout.BACKEND),
}
rollout.EXPECTED_CONTROLLER = '167b77cab9b70b2372a36c066f9bee190757b8202195bf125828889309f923a1'
rollout.EXPECTED_SERVICE = rollout.BASELINE['service_code']

if __name__ == '__main__':
    rollout.main()

#!/usr/bin/env python3
"""Deploy owner-suggested personality guidance over the installed recovery fix."""
from pathlib import Path

import budget_deployment as rollout

rollout.ARTIFACTS = Path('/home/daniel/andrew-tendencies-preflight-20261022')
rollout.CANDIDATE = Path('/var/backups/andrew-flexible-tendencies-20261022-candidate')
rollout.BASELINE = {
    'mode': 'enabled',
    'controller_code': '680776f2e8d7f0adf75d84605fba866a9b048d3514ebb52e307d6d611271aade',
    'service_code': '5902ee5a385e07b97823c7065208115d0a4d2e347cd308b5e6161ce0636f60c6',
    'backend_release_sha256': rollout.BACKEND_SHA,
    'backend_release': str(rollout.BACKEND),
}
rollout.EXPECTED_CONTROLLER = '2c43ab5d69c4c01d8ead58729ab35d750f67eb634f0868975fa7db3e1c37993b'
rollout.EXPECTED_SERVICE = rollout.BASELINE['service_code']

if __name__ == '__main__':
    rollout.main()

#!/usr/bin/env python3
"""Deploy reviewed pre-grant cancellation handling over the installed P2-10 release."""
from pathlib import Path

import budget_deployment as rollout

rollout.ARTIFACTS = Path('/home/daniel/andrew-pregrant-preflight-20261022')
rollout.CANDIDATE = Path('/var/backups/andrew-pregrant-rejection-20261022-candidate')
rollout.BASELINE = {
    'mode': 'enabled',
    'controller_code': 'db569d8eca2a0da1d44e2a1c465c93de6748f618198716796fe36da703e429ff',
    'service_code': '3b42b8e16b49bca78844221eeccdab18cd9ce5e05984a27b4e26e82932c73267',
    'backend_release_sha256': rollout.BACKEND_SHA,
    'backend_release': str(rollout.BACKEND),
}
rollout.EXPECTED_CONTROLLER = '680776f2e8d7f0adf75d84605fba866a9b048d3514ebb52e307d6d611271aade'
rollout.EXPECTED_SERVICE = '5902ee5a385e07b97823c7065208115d0a4d2e347cd308b5e6161ce0636f60c6'

if __name__ == '__main__':
    rollout.main()

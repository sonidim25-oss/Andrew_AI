"""Startup wiring for the administrator-bound runner receipt contract."""
import hashlib
import json
import os
from pathlib import Path

from .runtime_readiness import runner_adoption


def startup_record(role, *, environment=None):
    environment=os.environ if environment is None else environment
    record=runner_adoption(controller_directory=None) if role=='runner' else runner_adoption()
    enabled=environment.get('ANDREW_RUNNER_RECEIPTS_ENABLED','0')=='1'
    if record is None:
        if enabled:raise ValueError('Runner receipts require coordinated adoption')
        return None
    if enabled != (record['mode']=='enabled'):
        raise ValueError('Runner receipt flag differs from adoption record')
    if role not in {'controller','runner','backend'}:raise ValueError('Unknown runner receipt role')
    if role!='backend' and os.getuid()!=record[role+'_uid']:
        raise ValueError('Runner receipt service UID changed')
    return record


def mount_controller(controller,runner,record):
    from .runner_recovery import RunnerRecoveryAdapter
    from .recovery import LocalRecoveryClient
    from .appserver import AppServerClient
    store=controller.store
    if (store.path.parent/'runner-restore-required.json').exists():
        raise ValueError('Runner restore reconciliation required')
    if (store.get_state('local_recovery_controller_store_id','')!=record['controller_store_id']
            or store.get_state('runner_receipt_store_id','')!=record['runner_store_id']
            or runner is None or not hasattr(controller,'local_recovery')):
        raise ValueError('Runner recovery peers are not configured together')
    enabled=record['mode']=='enabled'
    if (store.get_state('runner_receipts_enabled','0')=='1')!=enabled:
        raise ValueError('Runner receipt controller state changed')
    client=LocalRecoveryClient(AppServerClient(controller.model.url))
    try:
        capability=client.capabilities()
        if capability.get('runner_result_recovery_v1')!=1 or capability.get('held_tool_result_v1')!=1:
            raise ValueError('Runner receipt backend capability missing')
        peers=runner.receipt_capabilities(expected_runner_uid=record['runner_uid'])
        if (peers.get('controller_store_id')!=record['controller_store_id'] or peers.get('runner_store_id')!=record['runner_store_id']
                or peers.get('contract')!='runner_result_recovery_v1' or peers.get('version')!=1
                or peers.get('execution_enabled')!=enabled):
            raise ValueError('Runner receipt receiver capability mismatch')
    finally:client.close()
    store.set_state('runner_receipt_backend_capability','1')
    controller.runner_recovery=RunnerRecoveryAdapter(store,runner,expected_runner_uid=record['runner_uid'],runner_store_id=record['runner_store_id'])


def open_receiver(record,credential_directory):
    from .runner_receipts import RunnerReceiptStore
    key_path=Path(credential_directory)/'andrew-runner-receipts-key'
    if key_path.is_symlink() or not key_path.is_file() or key_path.stat().st_size!=32:
        raise ValueError('Runner receipt credential unavailable')
    key=key_path.read_bytes()
    if hashlib.sha256(key).hexdigest()!=record['key_fingerprint']:
        raise ValueError('Runner receipt credential changed')
    root=Path('/var/lib/andrew/runtime/runner-receipts')
    receipts=RunnerReceiptStore(root,key,expected_uid=record['runner_uid'])
    identity=receipts._identity
    if identity is None or any(identity[k]!=record[k] for k in ('controller_store_id','runner_store_id','key_fingerprint')):
        receipts.close();raise ValueError('Runner receipt store changed')
    return receipts

"""Read pinned Letta recovery journal structure, never recover or certify a turn.

These files contain private tool results. Only fixed labels, counts and equality
checks against controller-owned input identities may leave this module.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from urllib.parse import quote

MAX_BYTES = 1024 * 1024
MAX_NAMESPACES = 16
MAX_RECEIPTS = 5
MAX_ITEMS = 256
SAFE_ID = re.compile(r'[A-Za-z0-9._~:%-]{1,256}\Z')
STOP_REASONS = frozenset({'end_turn', 'error', 'cancelled', 'requires_approval', 'max_steps',
                         'max_tokens_exceeded', 'error_retry_exhausted'})


def _ids(value):
    return isinstance(value, list) and len(value) <= MAX_ITEMS and all(
        isinstance(item, str) and SAFE_ID.fullmatch(item) for item in value)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result


def _open_directory(path):
    # Reject symlinks in every component, including the administrator's base.
    path = Path(os.path.abspath(path))
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read(directory_fd, filename):
    fd = None
    try:
        fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            return 'unsafe_file', None
        if before.st_size > MAX_BYTES:
            return 'too_large', None
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            raw = stream.read(MAX_BYTES + 1)
        after = os.fstat(fd)
        if len(raw) > MAX_BYTES:
            return 'too_large', None
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            return 'changed_during_read', None
        value = json.loads(raw, object_pairs_hook=_unique_object,
                           parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))
        return ('read', value) if isinstance(value, dict) else ('invalid_json', None)
    except FileNotFoundError:
        return 'missing', None
    except (ValueError, UnicodeError, RecursionError):
        return 'invalid_json', None
    except OSError:
        return 'unreadable', None
    finally:
        if fd is not None:
            os.close(fd)


def _interrupted(value, receipt):
    if (value.get('agentId') != receipt['agent_id']
            or value.get('conversationId') != receipt['conversation_id']):
        return {'status': 'scope_mismatch'}
    identities, consumers = value.get('durableInputIdentities', []), value.get('terminalConsumerIds', [])
    tools, results = value.get('toolCallIds'), value.get('results')
    if (not isinstance(identities, list) or len(identities) > MAX_ITEMS or not _ids(consumers)
            or not _ids(tools) or not isinstance(results, list) or len(results) > MAX_ITEMS
            or not all(isinstance(item, dict) and isinstance(item.get('tool_call_id'), str)
                       and SAFE_ID.fullmatch(item['tool_call_id']) for item in results)):
        return {'status': 'invalid_record'}
    if not all(isinstance(item, dict) and isinstance(item.get('domain'), str) and item['domain'] in {'input', 'teleport'}
               and isinstance(item.get('id'), str) and SAFE_ID.fullmatch(item['id']) for item in identities):
        return {'status': 'invalid_record'}
    matched = {'domain': 'input', 'id': receipt['client_message_id']} in identities
    bound = receipt['request_id'] in consumers
    if not matched or not bound:
        return {'status': 'different_input', 'input_matches': matched, 'consumer_matches': bound}
    marker = value.get('recoveryClaimCompletion')
    if marker is not None and (not isinstance(marker, dict) or not isinstance(marker.get('state'), str)
                                   or marker['state'] not in {'running', 'pending'}):
        return {'status': 'invalid_record'}
    return {'status': 'matching_record', 'recorded_tool_count': len(tools),
            'recorded_result_count': len(results),
            'recovery_claim_state': marker['state'] if marker else 'absent',
            'revision_present': isinstance(value.get('revision'), str) and bool(value['revision']),
            'authority_verified': False}


def _terminals(value, receipt):
    if (value.get('agentId') != receipt['agent_id']
            or value.get('conversationId') != receipt['conversation_id']):
        return {'status': 'scope_mismatch'}
    terminals = value.get('terminals')
    if not isinstance(terminals, list) or len(terminals) > MAX_ITEMS:
        return {'status': 'invalid_record'}
    matches = []
    for terminal in terminals:
        if not isinstance(terminal, dict):
            return {'status': 'invalid_record'}
        message, owner = terminal.get('message'), terminal.get('owner')
        if (not isinstance(message, dict) or message.get('type') != 'turn_finished'
                or not isinstance(owner, dict) or not _ids(message.get('terminal_consumer_ids', []))
                or not _ids(terminal.get('requiredConsumerIds')) or not _ids(terminal.get('acknowledgedConsumerIds'))):
            return {'status': 'invalid_record'}
        if receipt['request_id'] not in message.get('terminal_consumer_ids', []):
            continue
        reason = message.get('stop_reason')
        matches.append({'stop_reason': reason if isinstance(reason, str) and reason in STOP_REASONS else 'other',
            'required_consumer_matches': receipt['request_id'] in terminal['requiredConsumerIds'],
            'consumer_acknowledged': receipt['request_id'] in terminal['acknowledgedConsumerIds'],
            'run_matches': bool(receipt.get('run_id')) and message.get('run_id') == receipt['run_id'],
            'recovery_lineage_present': isinstance(owner.get('recoveryLineageId'), str) and bool(owner['recoveryLineageId']),
            'authority_verified': False})
    return {'status': 'matching_records' if matches else 'different_input', 'matches': matches[:8],
            'match_limit': len(matches) > 8}


def _input_disposition(value, receipt):
    if value.get('version') != 4 or not isinstance(value.get('entries'), dict):
        return {'status': 'unsupported_record'}
    runtime_key = 'agent:' + receipt['agent_id'] + '::conversation:' + receipt['conversation_id']
    key = json.dumps([runtime_key, 'input', receipt['client_message_id']], separators=(',', ':'))
    entry = value['entries'].get(key)
    if entry is None:
        return {'status': 'different_input'}
    if (not isinstance(entry, dict) or entry.get('runtimeKey') != runtime_key
            or not isinstance(entry.get('disposition'), str) or entry['disposition'] not in {'started', 'queued'}):
        return {'status': 'invalid_record'}
    result = {'status': 'matching_record', 'disposition': entry['disposition'],
              'replay_payload_present': isinstance(entry.get('queuedInput'), dict),
              'replay_completed_marker': entry.get('replayCompleted') is True,
              'completion_authority_present': isinstance(entry.get('completedTerminalAuthority'), dict),
              'prepared_terminal_present': isinstance(entry.get('preparedTerminal'), dict),
              'authority_verified': False}
    prepared = entry.get('preparedTerminal')
    if isinstance(prepared, dict):
        scope, message = prepared.get('scope'), prepared.get('message')
        if (not isinstance(scope, dict) or scope.get('agentId') != receipt['agent_id']
                or scope.get('conversationId') != receipt['conversation_id']
                or not isinstance(message, dict) or message.get('type') != 'turn_finished'
                or not _ids(message.get('terminal_consumer_ids', []))):
            return {'status': 'invalid_record'}
        reason = message.get('stop_reason')
        result['prepared_stop_reason'] = reason if isinstance(reason, str) and reason in STOP_REASONS else 'other'
        result['terminal_consumer_matches'] = receipt['request_id'] in message.get('terminal_consumer_ids', [])
    return result


def collect_recovery_metadata(home, receipts):
    report = {'format': 'letta-code-0.34.13', 'evidence_only': True, 'automatic_resume': False,
              'authority_verified': False, 'snapshot_atomic': False,
              'controller_receipt_count': min(len(receipts), MAX_RECEIPTS), 'records': []}
    if not receipts:
        return report | {'status': 'no_controller_receipts', 'receipt_limit': False,
                         'interrupted_scan': 'not_attempted', 'terminal_scan': 'not_attempted',
                         'input_scan': 'not_attempted'}
    try:
        home_fd = _open_directory(home)
    except OSError:
        return report | {'status': 'home_unavailable'}
    try:
        for kind, folder in (('interrupted', 'listener-state'), ('terminal', 'listener-terminal-state'),
                             ('input', 'state')):
            base_fd = None
            try:
                letta_fd = os.open('.letta', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=home_fd)
                try:
                    base_fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=letta_fd)
                finally:
                    os.close(letta_fd)
                # Inspect only namespace directories, never arbitrary journal files.
                with os.scandir(base_fd) as entries:
                    namespaces = []
                    for entry in entries:
                        if len(namespaces) == MAX_NAMESPACES:
                            report[kind + '_scan'] = 'namespace_limit'
                            break
                        namespaces.append(entry.name)
                for namespace in sorted(namespaces):
                    namespace_fd = None
                    try:
                        namespace_fd = os.open(namespace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=base_fd)
                        for receipt in receipts[:MAX_RECEIPTS]:
                            if not all(isinstance(receipt.get(key), str) and SAFE_ID.fullmatch(receipt[key])
                                       for key in ('agent_id', 'conversation_id', 'request_id', 'client_message_id')):
                                continue
                            agent, conversation = receipt['agent_id'], receipt['conversation_id']
                            if kind == 'interrupted':
                                filename = quote(agent, safe="~!*'()-._") + '_' + quote(conversation, safe="~!*'()-._") + '.json'
                            elif kind == 'terminal':
                                filename = base64.urlsafe_b64encode(json.dumps([agent, conversation],
                                    separators=(',', ':')).encode()).decode().rstrip('=') + '.json'
                            else:
                                filename = 'input-dispositions-v2.json'
                            status, value = _read(namespace_fd, filename)
                            if status == 'missing':
                                continue
                            reader = {'interrupted': _interrupted, 'terminal': _terminals,
                                      'input': _input_disposition}[kind]
                            metadata = reader(value, receipt) if status == 'read' else {'status': status}
                            report['records'].append({'kind': kind, 'cycle_id': receipt.get('cycle_id'),
                                'namespace_fingerprint': hashlib.sha256(os.fsencode(namespace)).hexdigest()[:16], **metadata})
                    except OSError:
                        report[kind + '_scan'] = 'some_namespaces_unavailable'
                    finally:
                        if namespace_fd is not None:
                            os.close(namespace_fd)
                report.setdefault(kind + '_scan', 'finished')
            except FileNotFoundError:
                report[kind + '_scan'] = 'missing'
            except OSError:
                report[kind + '_scan'] = 'unavailable'
            finally:
                if base_fd is not None:
                    os.close(base_fd)
        return report | {'status': 'metadata_only', 'receipt_limit': len(receipts) > MAX_RECEIPTS}
    finally:
        os.close(home_fd)

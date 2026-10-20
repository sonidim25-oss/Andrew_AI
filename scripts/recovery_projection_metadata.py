"""Offline structural evidence for transcript conflicts; never prints content."""
from collections import Counter
import hashlib
import json
import os
from pathlib import Path

from letta_recovery_metadata import _open_directory, _read

KNOWN_TOOLS=frozenset({'sleep','wake_at','continue_activity','check_budget','recent_activity',
    'save_diary_entry','memory_search','memory_add_experience','memory_propose_personality_edit',
    'python.run','node.run','shell.run','browser.open','browser.control','browser.account',
    'session_search','session_read','gmail.status','gmail.read','gmail.search','gmail.send',
    'gmail.draft','gmail.delete','gmail.reconcile','research.list','research.get',
    'research.observations','research.create','research.update','research.focus',
    'skills.list','skills.load','media.status','media.get','image.generate','image.edit',
    'voice.generate','profile.propose_avatar'})
PHASES=frozenset({'input_committed','provider_admitted','provider_backoff','provider_confirmed',
    'tool_prepared','tool_admitted','tool_confirmed','continuation_ready','terminal_committed','held'})
REASONS=frozenset({'legacy_unproven','binding_mismatch','unsupported_contract','provider_uncertain',
    'tool_uncertain','authority_lost','state_corrupt','storage_limit','owner_blocked',
    'configuration_changed','restore_required','projection_conflict'})

def checkpoint_structure(snapshot,receipt):
    def label(value,known):return value if value in known else 'other'
    states={'prepared','admitted','rejected','confirmed','cancelled'}
    tools=snapshot.get('tools',[]);steps=snapshot.get('steps',[])
    if not isinstance(tools,list) or not isinstance(steps,list) or len(tools)>256 or len(steps)>256:
        raise ValueError('invalid checkpoint arrays')
    return {'status':'read','input_matches':snapshot.get('binding',{}).get('client_message_id')==receipt.get('client_message_id'),
        'phase':label(snapshot.get('phase'),PHASES),
        'held_phase':label(snapshot.get('held_phase'),PHASES) if snapshot.get('held_phase') else None,
        'hold_reason':label(snapshot.get('hold_reason'),REASONS) if snapshot.get('hold_reason') else None,
        'revision':snapshot.get('revision') if type(snapshot.get('revision')) is int else None,
        'terminal_present':isinstance(snapshot.get('terminal'),dict),
        'provider_states':dict(Counter(label(item.get('state'),states) for item in steps)),
        'tool_states':dict(Counter(label(item.get('state'),states) for item in tools)),
        'tools':[{'name':label(item.get('name'),KNOWN_TOOLS),'state':label(item.get('state'),states)} for item in tools]}

def collect_checkpoint_metadata(home,receipts):
    records=[]
    for receipt in receipts[:6]:
        identity={key:receipt[key] for key in ('agent_id','conversation_id')}
        key=hashlib.sha256(json.dumps(identity,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
        for root in (Path(home)/'.letta',Path(home)/'.letta/lc-local-backend'):
            fd=None
            try:
                fd=_open_directory(root/'local-recovery-v1/conversations')
                status,snapshot=_read(fd,key+'.json')
                if status=='read':records.append(checkpoint_structure(snapshot,receipt))
            except (OSError,ValueError,TypeError,AttributeError):
                pass
            finally:
                if fd is not None:os.close(fd)
    return {'evidence_only':True,'records':records,'status':'read' if records else 'not_found'}


def transcript_structure(conversation, rows):
    messages = [row['message'] for row in rows if isinstance(row, dict)
                and isinstance(row.get('message'), dict)]
    latest = {message.get('id'): message for message in messages}
    active_ids = conversation.get('in_context_message_ids', [])
    active = [latest[key] for key in active_ids if key in latest] if active_ids else list(latest.values())
    prefix = messages[:len(active)]
    known_fields = {'id', 'role', 'content', 'timestamp', 'metadata', 'usage', 'stopReason',
                    'model', 'provider', 'api', 'toolCallId', 'toolName', 'isError'}
    changed = set()
    for old, current in zip(prefix, active):
        changed.update(key for key in known_fields if old.get(key) != current.get(key))
    return {'status': 'read', 'message_entries': len(messages), 'unique_message_ids': len(latest),
            'active_ids': len(active_ids), 'active_messages': len(active),
            'missing_active_ids': sum(key not in latest for key in active_ids),
            'compaction_entries': sum(row.get('type') == 'compaction' for row in rows),
            'recovery_entries': sum(bool(message.get('metadata', {}).get('andrew_recovery')) for message in messages),
            'raw_prefix_matches_active': prefix == active, 'prefix_changed_fields': sorted(changed)}


def collect_projection_metadata(home, receipts):
    """Inspect the two standard storage layouts without loading the SDK."""
    scopes = {(row['agent_id'], row['conversation_id']) for row in receipts
              if isinstance(row.get('agent_id'), str) and isinstance(row.get('conversation_id'), str)}
    records = []
    for root in (Path(home)/'.letta', Path(home)/'.letta/lc-local-backend'):
        base_fd = None
        try:
            base_fd = _open_directory(root/'conversations')
            with os.scandir(base_fd) as entries:
                names = [entry.name for _, entry in zip(range(256), entries)]
            for name in names:
                directory_fd = None
                try:
                    directory_fd = os.open(name, os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW, dir_fd=base_fd)
                    status, conversation = _read(directory_fd, 'conversation.json')
                    if status != 'read' or (conversation.get('agent_id'), conversation.get('id')) not in scopes:
                        continue
                    fd = os.open('messages.jsonl', os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK, dir_fd=directory_fd)
                    with os.fdopen(fd, 'rb') as stream:
                        raw = stream.read(32*1024*1024+1)
                    if len(raw) > 32*1024*1024:
                        records.append({'status': 'too_large'});continue
                    rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
                    records.append(transcript_structure(conversation, rows))
                except (OSError, ValueError, TypeError, AttributeError):
                    records.append({'status': 'unavailable_or_invalid'})
                finally:
                    if directory_fd is not None:os.close(directory_fd)
        except OSError:
            pass
        finally:
            if base_fd is not None:os.close(base_fd)
    return {'evidence_only': True, 'records': records, 'status': 'read' if records else 'not_found'}

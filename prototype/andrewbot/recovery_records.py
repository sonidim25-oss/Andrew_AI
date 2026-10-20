"""Private, exact local recovery admissions; never inferred from transcript text."""
import hashlib
import hmac
import json
import math
import secrets
import uuid
import time
from pathlib import Path

from .recovery_identity import FIELDS, ID, HASH, canonical, digest, valid_id, validate_binding

MAX_PAYLOAD=16*1024*1024


def initialize_schema(store):
    store.connection.executescript('''
        CREATE TABLE IF NOT EXISTS recovery_attempts(
            admission_id TEXT PRIMARY KEY,binding_sha TEXT NOT NULL,binding_json TEXT NOT NULL,
            token_sha TEXT NOT NULL,state TEXT NOT NULL,fence INTEGER NOT NULL DEFAULT 0,
            backend_attempt_id TEXT,backend_revision INTEGER,created_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS recovery_scope_binding ON recovery_attempts(binding_sha);
        CREATE TABLE IF NOT EXISTS recovery_effects(
            admission_id TEXT PRIMARY KEY,scope_id TEXT NOT NULL,binding_sha TEXT NOT NULL,
            kind TEXT NOT NULL,effect_id TEXT NOT NULL,request_sha TEXT NOT NULL,
            token_sha TEXT NOT NULL,fence INTEGER NOT NULL,state TEXT NOT NULL,
            reservation_id TEXT,created_at TEXT NOT NULL,UNIQUE(binding_sha,kind,effect_id));
        CREATE TABLE IF NOT EXISTS recovery_results(
            admission_id TEXT PRIMARY KEY REFERENCES recovery_effects(admission_id),
            result_sha TEXT NOT NULL,result_json TEXT NOT NULL,usage_json TEXT,created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS recovery_provider_rejections(
            admission_id TEXT NOT NULL REFERENCES recovery_effects(admission_id),
            attempt INTEGER NOT NULL,http_status INTEGER NOT NULL CHECK(http_status=429),
            rejected_at_ms INTEGER NOT NULL,retry_at_ms INTEGER NOT NULL,
            PRIMARY KEY(admission_id,attempt));
        CREATE TABLE IF NOT EXISTS recovery_owner_yields(
            binding_sha TEXT PRIMARY KEY,receipt_id TEXT NOT NULL UNIQUE,
            admission_ids_json TEXT NOT NULL,created_at TEXT NOT NULL,
            cancelled_ids_json TEXT NOT NULL DEFAULT '[]');
    ''')
    if 'cancelled_ids_json' not in {row[1] for row in store.connection.execute('PRAGMA table_info(recovery_owner_yields)')}:
        store.connection.execute("ALTER TABLE recovery_owner_yields ADD COLUMN cancelled_ids_json TEXT NOT NULL DEFAULT '[]'")
    columns={row[1] for row in store.connection.execute('PRAGMA table_info(recovery_attempts)')}
    for name,declaration in (('origin', "TEXT NOT NULL DEFAULT 'autonomous'"),('terminal_json','TEXT'),('untrusted','INTEGER NOT NULL DEFAULT 0'),('hold_reason','TEXT')):
        if name not in columns:store.connection.execute('ALTER TABLE recovery_attempts ADD COLUMN '+name+' '+declaration)


def paused_owner_yield(state,binding):
    """New owner authority may cancel old work, never dispatch it."""
    try:newer=int(state.get('owner_control_revision','0'))>int(binding['owner_revision'])
    except (ValueError,TypeError):return False
    return (newer and state.get('paused')=='1' and state.get('sleeping','0')=='0'
        and state.get('autonomy_error')=='local_recovery_interrupted'
        and not any(state.get(key) for key in ('active_session_id','active_cycle_id','model_turn_origin','model_turn_progress','autonomous_owner_id','session_start_requested'))
        and not any(state.get(key)=='1' for key in ('restore_recovery_required','local_recovery_restore_required'))
        and all(state.get('letta_'+key)==binding[key] for key in ('agent_id','conversation_id'))
        and state.get('local_recovery_config_sha')==binding['config_sha'])


class RecoveryRecords:
    def __init__(self,store):self.store=store

    def _guard(self,binding,origin='autonomous',*,allow_owner_pending=False):
        validate_binding(binding)
        if (self.store.path.parent / 'runner-restore-required.json').exists():raise ValueError('Recovery restore_required')
        if origin=='owner':
            if (self.store.get_state('model_turn_origin','')!='owner'
                    or self.store.get_state('owner_control_revision','0')!=binding['owner_revision']
                    or self.store.get_state('letta_agent_id','')!=binding['agent_id']
                    or self.store.get_state('letta_conversation_id','')!=binding['conversation_id']
                    or self.store.get_state('local_recovery_config_sha','')!=binding['config_sha']
                    or any(self.store.get_state(key,'0')=='1' for key in ('paused','restore_recovery_required','local_recovery_restore_required'))):raise ValueError('Recovery owner_blocked')
            return
        if origin!='autonomous':raise ValueError('Recovery unsupported_contract')
        if any(self.store.get_state(key,'0')=='1' for key in ('paused','sleeping','restore_recovery_required','local_recovery_restore_required')):raise ValueError('Recovery owner_blocked')
        if (self.store.get_state('owner_control_revision','0')!=binding['owner_revision']
                or self.store.get_state('active_session_id','')!=binding['session_id']
                or self.store.get_state('active_cycle_id','')!=binding['cycle_id']
                or self.store.get_state('local_recovery_config_sha','')!=binding['config_sha']):raise ValueError('Recovery binding_mismatch')
        receipt=self.store.connection.execute('SELECT * FROM model_turn_receipts WHERE cycle_id=? AND session_id=?',(binding['cycle_id'],binding['session_id'])).fetchone()
        if receipt is None or any(receipt[key]!=binding[key] for key in ('agent_id','conversation_id','client_message_id','owner_revision')) or receipt['request_id']!=binding['consumer_id']:raise ValueError('Recovery binding_mismatch')
        if not allow_owner_pending and self.store.pending_owner_input(int(self.store.get_state('local_recovery_owner_id','') or self.store.get_state('autonomous_owner_id','0') or '0')):raise ValueError('Recovery owner_blocked')
        if self.store.get_state('autonomy_error','') not in ('','local_recovery_interrupted'):raise ValueError('Recovery owner_blocked')

    def _guard_owner_yield(self,binding,origin):
        validate_binding(binding)
        if origin=='autonomous':
            state=dict(self.store.connection.execute('SELECT key,value FROM state'))
            if paused_owner_yield(state,binding):
                other=self.store.connection.execute("SELECT 1 FROM recovery_attempts WHERE state IN ('active','held','evidence','yielding') AND binding_sha!=? LIMIT 1",(digest(binding),)).fetchone()
                if other is not None:raise ValueError('Recovery binding_mismatch')
                return
        self._guard(binding,origin,allow_owner_pending=True)

    def admit_scope(self,binding,*,origin='autonomous',evidence_only=False,yield_only=False):
        with self.store._lock,self.store.connection:
            if evidence_only:
                validate_binding(binding)
                old=self.store.connection.execute('SELECT origin FROM recovery_attempts WHERE binding_sha=? ORDER BY rowid DESC LIMIT 1',(digest(binding),)).fetchone()
                if old is None or old['origin']!=origin:raise ValueError('Recovery legacy_unproven')
            else:
                if yield_only and origin!='autonomous':raise ValueError('Recovery owner_blocked')
                if yield_only:self._guard_owner_yield(binding,origin)
                else:self._guard(binding,origin)
            previous_attempt=self.store.connection.execute('SELECT backend_attempt_id FROM recovery_attempts WHERE binding_sha=? ORDER BY rowid DESC LIMIT 1',(digest(binding),)).fetchone()
            tainted=self.store.connection.execute('SELECT coalesce(max(untrusted),0) FROM recovery_attempts WHERE binding_sha=?',(digest(binding),)).fetchone()[0]
            token=secrets.token_hex(32);scope_id=uuid.uuid4().hex;binding_sha=digest(binding)
            self.store.connection.execute("UPDATE recovery_attempts SET state='revoked' WHERE binding_sha=? AND state NOT IN ('complete','revoked')",(binding_sha,))
            self.store.connection.execute("INSERT INTO recovery_attempts(admission_id,binding_sha,binding_json,token_sha,state,created_at,origin) VALUES(?,?,?,?,'active',?,?)",(scope_id,binding_sha,canonical(binding),hashlib.sha256(token.encode()).hexdigest(),self.store.now(),origin))
            self.store.connection.execute('UPDATE recovery_attempts SET untrusted=?,backend_attempt_id=? WHERE admission_id=?',(tainted,previous_attempt[0] if previous_attempt else None,scope_id))
            if evidence_only:self.store.connection.execute("UPDATE recovery_attempts SET state='evidence' WHERE admission_id=?",(scope_id,))
            elif yield_only:self.store.connection.execute("UPDATE recovery_attempts SET state='yielding' WHERE admission_id=?",(scope_id,))
            return {'admission_id':scope_id,'scope_token':token}

    def _scope(self,binding,claim,token,*,dispatch=True,owner_yield=False):
        validate_binding(binding)
        if not isinstance(claim,dict) or not valid_id(claim.get('admission_id')) or type(claim.get('fence')) is not int or claim['fence']<1 or not isinstance(token,str):raise ValueError('Recovery authority_lost')
        row=self.store.connection.execute('SELECT * FROM recovery_attempts WHERE admission_id=?',(claim['admission_id'],)).fetchone()
        if row is None or row['binding_sha']!=digest(binding) or not hmac.compare_digest(row['token_sha'],hashlib.sha256(token.encode()).hexdigest()):raise ValueError('Recovery authority_lost')
        if dispatch:
            expiry=claim.get('expires_monotonic_ms')
            boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
            if (type(expiry) not in (int,float) or not math.isfinite(expiry)
                    or expiry<=time.monotonic()*1000 or expiry>time.monotonic()*1000+15500
                    or claim.get('boot_id')!=boot):raise ValueError('Recovery authority_lost')
            if owner_yield:self._guard_owner_yield(binding,row['origin'])
            else:self._guard(binding,row['origin'])
            if row['state'] not in (('active','yielding') if owner_yield else ('active',)) or claim['fence']<row['fence']:raise ValueError('Recovery authority_lost')
        return row

    def prepare_owner_yield(self,binding,claim,token):
        """Fence future dispatch before acknowledging a clean owner handoff."""
        with self.store._lock,self.store.connection:
            if not self.store.connection.in_transaction:self.store.connection.execute('BEGIN IMMEDIATE')
            scope=self._scope(binding,claim,token,owner_yield=True)
            owner=int(self.store.get_state('local_recovery_owner_id','0'))
            if scope['origin']!='autonomous' or not self.store.pending_owner_input(owner):raise ValueError('Recovery owner_blocked')
            effects=self.store.connection.execute('SELECT * FROM recovery_effects WHERE binding_sha=?',(digest(binding),)).fetchall()
            old=self.store.connection.execute('SELECT * FROM recovery_owner_yields WHERE binding_sha=?',(digest(binding),)).fetchone()
            cancelled=sorted(effect['admission_id'] for effect in effects if effect['state'] in ('prepared','cancelled'))
            if old and old['cancelled_ids_json']!=canonical(cancelled):raise ValueError('Recovery binding_mismatch')
            for effect in effects:
                if effect['state']=='prepared':
                    if effect['reservation_id'] is not None:raise ValueError('Recovery provider_uncertain')
                    from .runner_recovery import cancel_prepared_mapping_locked
                    cancel_prepared_mapping_locked(self.store,effect['admission_id'])
                elif effect['state']=='cancelled':
                    if old is None or effect['admission_id'] not in json.loads(old['cancelled_ids_json']):
                        from .runner_recovery import prepared_cancellation_proven
                        if not prepared_cancellation_proven(self.store,effect['admission_id']):raise ValueError('Recovery state_corrupt')
                elif effect['state']=='confirmed':
                    if self.store.connection.execute('SELECT 1 FROM recovery_results WHERE admission_id=?',(effect['admission_id'],)).fetchone() is None:raise ValueError('Recovery state_corrupt')
                elif effect['state']!='rejected' or self._provider_rejection(effect['admission_id']) is None:raise ValueError('Recovery tool_uncertain')
                if effect['reservation_id'] and self.store.connection.execute('SELECT 1 FROM budget_reservations WHERE reservation_id=?',(effect['reservation_id'],)).fetchone():raise ValueError('Recovery provider_uncertain')
            ids=sorted(effect['admission_id'] for effect in effects);encoded=canonical(ids)
            old=self.store.connection.execute('SELECT * FROM recovery_owner_yields WHERE binding_sha=?',(digest(binding),)).fetchone()
            if old and old['admission_ids_json']!=encoded:raise ValueError('Recovery binding_mismatch')
            receipt_id=old['receipt_id'] if old else uuid.uuid4().hex
            if old is None:self.store.connection.execute('INSERT INTO recovery_owner_yields VALUES(?,?,?,?,?)',(digest(binding),receipt_id,encoded,self.store.now(),canonical(cancelled)))
            self.store.connection.execute("UPDATE recovery_effects SET state='cancelled' WHERE binding_sha=? AND state='prepared'",(digest(binding),))
            self.store.connection.execute("UPDATE recovery_attempts SET state='yielding' WHERE admission_id=?",(scope['admission_id'],))
            return {'state':'owner_yield','binding':binding,'receipt_id':receipt_id,'admission_ids':ids,'cancelled_admission_ids':cancelled}

    def _complete_owner_yield(self,admission_id,binding,terminal):
        required={'id','run_id','consumer_id','stop_reason','assistant_ids','yield_receipt_id','cancelled_admission_ids'}
        if (set(terminal)-required-{'acknowledged','settled'} or not required<=set(terminal)
                or not all(valid_id(terminal[key]) for key in ('id','run_id','yield_receipt_id'))
                or terminal['consumer_id']!=binding['consumer_id'] or terminal['assistant_ids']!=[]
                or any(key in terminal and type(terminal[key]) is not bool for key in ('acknowledged','settled'))):raise ValueError('Recovery binding_mismatch')
        encoded=canonical({key:value for key,value in terminal.items() if key not in {'acknowledged','settled'}})
        with self.store._lock,self.store.connection:
            proof=self.store.connection.execute('SELECT * FROM recovery_owner_yields WHERE binding_sha=?',(digest(binding),)).fetchone()
            row=self.store.connection.execute('SELECT * FROM recovery_attempts WHERE admission_id=?',(admission_id,)).fetchone()
            ids=sorted(effect[0] for effect in self.store.connection.execute('SELECT admission_id FROM recovery_effects WHERE binding_sha=?',(digest(binding),)))
            if (proof is None or proof['receipt_id']!=terminal['yield_receipt_id'] or proof['admission_ids_json']!=canonical(ids)
                    or proof['cancelled_ids_json']!=canonical(terminal['cancelled_admission_ids'])
                    or row is None or row['binding_sha']!=digest(binding) or row['terminal_json'] not in (None,encoded)):
                raise ValueError('Recovery binding_mismatch')
            self.store.connection.execute('UPDATE recovery_attempts SET terminal_json=? WHERE admission_id=?',(encoded,admission_id))

    def finish_owner_yield(self,admission_id,binding):
        with self.store._lock,self.store.connection:
            row=self.store.connection.execute('SELECT * FROM recovery_attempts WHERE admission_id=?',(admission_id,)).fetchone()
            terminal=json.loads(row['terminal_json']) if row and row['terminal_json'] else None
            if row is None or row['binding_sha']!=digest(binding) or not terminal or terminal.get('stop_reason')!='owner_input_pending':raise ValueError('Recovery binding_mismatch')
            self.store.connection.execute("UPDATE recovery_attempts SET state='yielded' WHERE admission_id=?",(admission_id,))
            changed=self.store.connection.execute("UPDATE cycles SET status='stopped',ended_at=? WHERE cycle_id=? AND session_id=? AND status='running'",(self.store.now(),binding['cycle_id'],binding['session_id'])).rowcount
            if changed:self.store._research_event('cycle_finished',outcome='stopped',session_id=binding['session_id'],cycle_id=binding['cycle_id'])
            self.store.connection.execute("DELETE FROM state WHERE key='active_cycle_id' AND value=?",(binding['cycle_id'],))
            receipt={'cycle_id':binding['cycle_id'],'phase':'owner_input_pending','interrupted':True,
                     'uncertain_tools_preserved':bool(self.store.cycle_uncertain_effects(binding['cycle_id']))}
            self.store.connection.execute("INSERT INTO state VALUES('session_recovery_checkpoint',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(canonical(receipt),))
            if (self.store.get_state('active_session_id','')==binding['session_id']
                    and self.store.get_state('owner_control_revision','0')==binding['owner_revision']
                    and self.store.get_state('autonomy_error','')=='local_recovery_interrupted'):
                self.store.connection.execute("UPDATE state SET value='' WHERE key='autonomy_error'")

    def admit_effect(self,binding,claim,effect,scope_token):
        with self.store._lock,self.store.connection:
            scope=self._scope(binding,claim,scope_token)
            if not isinstance(effect,dict) or effect.get('kind') not in {'provider','compaction','tool'} or not valid_id(effect.get('effect_id')) or not isinstance(effect.get('request_sha'),str) or not HASH.fullmatch(effect['request_sha']):raise ValueError('Invalid recovery effect')
            effect_id=digest({'binding':binding,'kind':effect['kind'],'effect_id':effect['effect_id']})
            old=self.store.connection.execute('SELECT * FROM recovery_effects WHERE admission_id=?',(effect_id,)).fetchone()
            token=hmac.new(scope_token.encode(),canonical({'effect':effect_id,'fence':claim['fence']}).encode(),hashlib.sha256).hexdigest()
            if old:
                if old['request_sha']!=effect['request_sha']:raise ValueError('Recovery binding_mismatch')
                if old['state']=='confirmed':return {'admission_id':effect_id,'state':'confirmed','token':None}
                if old['scope_id']!=scope['admission_id'] or old['fence']!=claim['fence']:
                    if old['state'] in ('prepared','rejected') and old['reservation_id'] is None and claim['fence']>old['fence']:
                        self.store.connection.execute("UPDATE recovery_effects SET scope_id=?,fence=?,token_sha=? WHERE admission_id=? AND state IN ('prepared','rejected')",
                            (scope['admission_id'],claim['fence'],hashlib.sha256(token.encode()).hexdigest(),effect_id))
                    else:return {'admission_id':effect_id,'state':'uncertain','token':None}
            else:
                self.store.connection.execute("INSERT INTO recovery_effects(admission_id,scope_id,binding_sha,kind,effect_id,request_sha,token_sha,fence,state,created_at) VALUES(?,?,?,?,?,?,?,?,'prepared',?)",(effect_id,scope['admission_id'],digest(binding),effect['kind'],effect['effect_id'],effect['request_sha'],hashlib.sha256(token.encode()).hexdigest(),claim['fence'],self.store.now()))
            self.store.connection.execute('UPDATE recovery_attempts SET fence=? WHERE admission_id=?',(claim['fence'],scope['admission_id']))
            return {'admission_id':effect_id,'state':old['state'] if old else 'prepared','token':token}

    def _validate_dispatch(self,metadata,request,kind):
        return self._validate_dispatch_sha(metadata,digest(request),kind)

    def _validate_dispatch_sha(self,metadata,request_sha,kind):
        binding,claim=metadata['binding'],metadata['claim']
        self._scope(binding,claim,metadata['scope_token'])
        row=self.store.connection.execute('SELECT * FROM recovery_effects WHERE admission_id=?',(metadata['admission_id'],)).fetchone()
        if row is None or row['binding_sha']!=digest(binding) or row['scope_id']!=claim['admission_id'] or row['fence']!=claim['fence'] or row['kind'] not in kind or row['request_sha']!=request_sha:raise ValueError('Recovery binding_mismatch')
        if row['state']=='confirmed':return row
        token=metadata.get('token')
        if not isinstance(token,str) or not hmac.compare_digest(row['token_sha'],hashlib.sha256(token.encode()).hexdigest()):raise ValueError('Recovery authority_lost')
        return row

    def begin_provider(self,metadata,request,month,day,reserved):
        with self.store._lock,self.store.connection:
            self.store.connection.execute('BEGIN IMMEDIATE')
            row=self._validate_dispatch(metadata,request,{'provider','compaction'})
            if row['state']=='rejected':
                rejection=self._provider_rejection(row['admission_id'])
                if rejection is None or rejection['retry_at_ms']>int(time.time()*1000):return None
            elif row['state']!='prepared':return None
            reservation_id='local-recovery-'+row['admission_id']
            self.store.connection.execute('INSERT INTO budget_reservations VALUES(?,?,?,?,?)',(reservation_id,month,day,reserved,self.store.now()))
            self.store.connection.execute("UPDATE recovery_effects SET state='admitted',reservation_id=? WHERE admission_id=?",(reservation_id,row['admission_id']))
            return reservation_id

    def _provider_rejection(self,admission_id):
        row=self.store.connection.execute('SELECT * FROM recovery_provider_rejections WHERE admission_id=? ORDER BY attempt DESC LIMIT 1',(admission_id,)).fetchone()
        return dict(row) if row else None

    def waiting_provider(self):
        with self.store._lock:
            row=self.store.connection.execute("SELECT e.admission_id FROM recovery_effects e WHERE e.state='rejected' AND EXISTS (SELECT 1 FROM recovery_attempts a WHERE a.binding_sha=e.binding_sha AND a.state='active') ORDER BY e.rowid DESC LIMIT 1").fetchone()
            return self.effect_result(row['admission_id']) if row else None

    def reject_provider(self,admission_id,*,http_status,retry_after=None):
        """Atomically record an explicit HTTP 429 and release only its reservation.

        A later dispatch moves the effect back to admitted BEFORE network I/O.
        Old rejection receipts can therefore never authorize replay of a lost retry.
        """
        if type(http_status) is not int or http_status!=429:raise ValueError('Not a retryable provider rejection')
        if retry_after is not None and (type(retry_after) not in (int,float) or not math.isfinite(retry_after) or not 0<=retry_after<=315360000):raise ValueError('Invalid retry delay')
        with self.store._lock,self.store.connection:
            self.store.connection.execute('BEGIN IMMEDIATE')
            row=self.store.connection.execute('SELECT * FROM recovery_effects WHERE admission_id=?',(admission_id,)).fetchone()
            if row is None or row['kind'] not in ('provider','compaction') or row['state']!='admitted':raise ValueError('Recovery provider_uncertain')
            reservation=self.store.connection.execute('SELECT 1 FROM budget_reservations WHERE reservation_id=?',(row['reservation_id'],)).fetchone()
            if reservation is None:raise ValueError('Recovery provider_uncertain')
            previous=self._provider_rejection(admission_id)
            attempt=previous['attempt']+1 if previous else 1
            # Exponential backoff is capped; an explicit provider delay is never shortened.
            delay=max(5*2**min(attempt-1,6),retry_after or 0)
            now=int(time.time()*1000);retry_at=now+math.ceil(delay*1000)
            self.store.connection.execute('INSERT INTO recovery_provider_rejections VALUES(?,?,?,?,?)',(admission_id,attempt,429,now,retry_at))
            self.store.connection.execute('DELETE FROM budget_reservations WHERE reservation_id=?',(row['reservation_id'],))
            self.store.connection.execute("UPDATE recovery_effects SET state='rejected',reservation_id=NULL WHERE admission_id=?",(admission_id,))
            return self.effect_result(admission_id)

    def begin_tool(self,metadata,name,arguments):
        with self.store._lock,self.store.connection:
            self.store.connection.execute('BEGIN IMMEDIATE')
            return self._begin_tool_sha_locked(metadata,digest({'name':name,'arguments':arguments}))

    def _begin_tool_sha_locked(self,metadata,request_sha):
        if not self.store.connection.in_transaction:raise ValueError('Recovery transaction required')
        row=self._validate_dispatch_sha(metadata,request_sha,{'tool'})
        if row['state']!='prepared':raise ValueError('Recovery tool_uncertain')
        self.store.connection.execute("UPDATE recovery_effects SET state='admitted' WHERE admission_id=?",(row['admission_id'],))
        return row['admission_id']

    def confirm_provider(self,admission_id,response,usage):
        encoded=canonical(response)
        if len(encoded.encode())>MAX_PAYLOAD:raise ValueError('Recovery storage_limit')
        with self.store._lock,self.store.connection:
            self.store.connection.execute('BEGIN IMMEDIATE')
            row=self.store.connection.execute('SELECT * FROM recovery_effects WHERE admission_id=?',(admission_id,)).fetchone()
            if row is None or row['state']!='admitted':raise ValueError('Recovery provider_uncertain')
            reservation=self.store.connection.execute('SELECT * FROM budget_reservations WHERE reservation_id=?',(row['reservation_id'],)).fetchone()
            if reservation is None:raise ValueError('Recovery provider_uncertain')
            self.store.connection.execute('INSERT INTO usage(month,day,cost_usd,input_tokens,output_tokens,created_at) VALUES(?,?,?,?,?,?)',(reservation['month'],reservation['day'],usage['cost'],usage['input_tokens'],usage['output_tokens'],self.store.now()))
            self.store.connection.execute('DELETE FROM budget_reservations WHERE reservation_id=?',(row['reservation_id'],))
            self._confirm(admission_id,encoded,{'settled':True,**usage})

    def _confirm(self,admission_id,encoded,usage):
        self.store.connection.execute('INSERT INTO recovery_results VALUES(?,?,?,?,?)',(admission_id,hashlib.sha256(encoded.encode()).hexdigest(),encoded,canonical(usage) if usage is not None else None,self.store.now()))
        self.store.connection.execute("UPDATE recovery_effects SET state='confirmed' WHERE admission_id=?",(admission_id,))

    def confirm_tool(self,admission_id,result,*,terminal_stop=None):
        encoded=canonical(result)
        if len(encoded.encode())>MAX_PAYLOAD:raise ValueError('Recovery storage_limit')
        with self.store._lock,self.store.connection:
            row=self.store.connection.execute('SELECT state FROM recovery_effects WHERE admission_id=?',(admission_id,)).fetchone()
            if row is None or row['state']!='admitted':raise ValueError('Recovery tool_uncertain')
            self._confirm(admission_id,encoded,{'terminal_stop':terminal_stop} if terminal_stop in {'sleep','wake_at'} else None)

    def effect_result(self,admission_id):
        with self.store._lock:
            effect=self.store.connection.execute('SELECT state,request_sha,kind FROM recovery_effects WHERE admission_id=?',(admission_id,)).fetchone()
            if effect is not None and effect['state']=='rejected':
                rejection=self._provider_rejection(admission_id)
                if rejection is None:raise ValueError('Recovery state_corrupt')
                return {'state':'rejected','admission_id':admission_id,'request_sha':effect['request_sha'],**rejection}
            row=self.store.connection.execute('SELECT r.* FROM recovery_results r JOIN recovery_effects e ON e.admission_id=r.admission_id WHERE r.admission_id=? AND e.state=\'confirmed\'',(admission_id,)).fetchone()
            if row is None:return {'state':'uncertain','admission_id':admission_id}
            usage=json.loads(row['usage_json']) if row['usage_json'] else None
            runner={}
            if isinstance(usage,dict) and 'runner_receipt' in usage:
                from .runner_recovery import confirmed_runner_metadata
                runner['runner_receipt']=confirmed_runner_metadata(self.store,admission_id,row['result_sha'],json.loads(row['result_json']),usage)
            return {**runner,'state':'confirmed','admission_id':admission_id,'request_sha':effect['request_sha'],'kind':effect['kind'],'result_sha':row['result_sha'],'result':json.loads(row['result_json']),'usage':usage,
                **({'terminal_stop':usage['terminal_stop']} if isinstance(usage,dict) and usage.get('terminal_stop') in {'sleep','wake_at'} else {})}

    def complete_scope(self,admission_id,binding,terminal):
        validate_binding(binding)
        if isinstance(terminal,dict) and terminal.get('stop_reason')=='owner_input_pending':
            return self._complete_owner_yield(admission_id,binding,terminal)
        maintenance=isinstance(terminal,dict) and terminal.get('stop_reason')=='error' and terminal.get('error_category')=='diary_not_committed'
        if (not isinstance(terminal,dict) or not valid_id(terminal.get('id')) or not valid_id(terminal.get('run_id'))
                or set(terminal)-{'id','run_id','consumer_id','stop_reason','assistant_ids','result_admission_id','lifecycle_stop','acknowledged','settled'}-({'error_category','maintenance_receipt_sha'} if maintenance else set())
                or ('lifecycle_stop' in terminal and terminal['lifecycle_stop'] not in {'sleep','wake_at'})
                or any(key in terminal and type(terminal[key]) is not bool for key in ('acknowledged','settled'))
                or terminal.get('consumer_id')!=binding['consumer_id'] or terminal.get('stop_reason')!=('error' if maintenance else 'end_turn')
                or not isinstance(terminal.get('assistant_ids'),list) or (not maintenance and not terminal['assistant_ids'])
                or len(terminal['assistant_ids'])>256 or not all(valid_id(item) for item in terminal['assistant_ids'])
                or not valid_id(terminal.get('result_admission_id'))):raise ValueError('Recovery binding_mismatch')
        terminal={key:value for key,value in terminal.items() if key not in {'acknowledged','settled'}}
        encoded=canonical(terminal)
        with self.store._lock,self.store.connection:
            row=self.store.connection.execute('SELECT * FROM recovery_attempts WHERE admission_id=?',(admission_id,)).fetchone()
            if row is None or row['binding_sha']!=digest(binding):raise ValueError('Recovery binding_mismatch')
            if row['terminal_json'] is not None:
                if row['terminal_json']!=encoded:raise ValueError('Recovery binding_mismatch')
                return
            effects=self.store.connection.execute('SELECT * FROM recovery_effects WHERE binding_sha=?',(digest(binding),)).fetchall()
            if maintenance:
                proof=self.effect_result(terminal['result_admission_id'])
                receipt=(proof.get('usage') or {}).get('local_diary_reconciliation',{})
                result=json.dumps({'status':'error','error':'diary_not_committed','committed':False,'evidence':'quiescent_local_diary_ledger'},separators=(',',':'))
                if (row['origin']!='owner' or terminal['assistant_ids'] or len(effects)!=2
                        or sorted(effect['kind'] for effect in effects)!=['provider','tool']
                        or proof.get('kind')!='tool' or proof.get('state')!='confirmed' or proof.get('result')!=result
                        or receipt.get('contract')!='local_diary_destination_reconciliation_v1' or receipt.get('committed') is not False
                        or not isinstance(receipt.get('evidence_sha'),str) or not HASH.fullmatch(receipt['evidence_sha'])
                        or terminal.get('maintenance_receipt_sha')!=receipt['evidence_sha']):raise ValueError('Recovery binding_mismatch')
            if (not effects or any(effect['state']!='confirmed' for effect in effects)
                    or terminal['result_admission_id'] not in {effect['admission_id'] for effect in effects}
                    or self.store.cycle_uncertain_effects(binding['cycle_id'])):raise ValueError('Recovery provider_uncertain')
            for effect in effects:
                if effect['reservation_id'] and self.store.connection.execute('SELECT 1 FROM budget_reservations WHERE reservation_id=?',(effect['reservation_id'],)).fetchone():raise ValueError('Recovery provider_uncertain')
            self.store.connection.execute("UPDATE recovery_attempts SET terminal_json=? WHERE admission_id=?",(encoded,admission_id))

    def settlement(self,binding,claim,token,terminal_id):
        with self.store._lock:
            row=self._scope(binding,claim,token,dispatch=False)
            terminal=json.loads(row['terminal_json']) if row['terminal_json'] else None
            if not terminal or terminal['id']!=terminal_id:raise ValueError('Recovery binding_mismatch')
            effects=self.store.connection.execute('SELECT admission_id,state FROM recovery_effects WHERE binding_sha=?',(digest(binding),)).fetchall()
            yielded=terminal.get('stop_reason')=='owner_input_pending'
            proof=self.store.connection.execute('SELECT * FROM recovery_owner_yields WHERE binding_sha=?',(digest(binding),)).fetchone() if yielded else None
            cancelled=json.loads(proof['cancelled_ids_json']) if proof and proof['receipt_id']==terminal.get('yield_receipt_id') else []
            if any(effect['state']!='confirmed' and not (yielded and ((effect['state']=='rejected' and self._provider_rejection(effect['admission_id'])) or (effect['state']=='cancelled' and effect['admission_id'] in cancelled))) for effect in effects):raise ValueError('Recovery provider_uncertain')
            return {'settled':True,'binding':binding,'terminal_id':terminal_id,'admission_ids':sorted(effect['admission_id'] for effect in effects)}

    def revoke(self,admission_id):
        with self.store._lock,self.store.connection:self.store.connection.execute("UPDATE recovery_attempts SET state='revoked' WHERE admission_id=?",(admission_id,))

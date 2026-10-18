"""Private, exact local recovery admissions; never inferred from transcript text."""
import hashlib
import hmac
import json
import math
import re
import secrets
import uuid
import time
from pathlib import Path
from decimal import Decimal

FIELDS={'store_id','agent_id','conversation_id','session_id','cycle_id','client_message_id','consumer_id','input_sha','owner_revision','config_sha'}
ID=re.compile(r'[A-Za-z0-9_-][A-Za-z0-9._:-]{0,255}\Z')
HASH=re.compile(r'[a-f0-9]{64}\Z')
MAX_PAYLOAD=16*1024*1024


def canonical(value):
    if value is None:return 'null'
    if value is True:return 'true'
    if value is False:return 'false'
    if isinstance(value,str):return json.dumps(value,ensure_ascii=False)
    if isinstance(value,int):
        if abs(value)>2**53-1:raise ValueError('Invalid recovery number')
        return str(value)
    if isinstance(value,float):
        if not math.isfinite(value):raise ValueError('Invalid recovery number')
        if value==0:return '0'
        if 1e-6<=abs(value)<1e21:return format(Decimal(repr(value)),'f').rstrip('0').rstrip('.') if '.' in format(Decimal(repr(value)),'f') else format(Decimal(repr(value)),'f')
        result=repr(value).lower();mantissa,exponent=result.split('e')
        return mantissa.rstrip('0').rstrip('.')+'e'+('+' if int(exponent)>=0 else '-')+str(abs(int(exponent)))
    if isinstance(value,list):return '['+','.join(map(canonical,value))+']'
    if isinstance(value,dict) and all(isinstance(key,str) for key in value):return '{'+','.join(json.dumps(key,ensure_ascii=False)+':'+canonical(value[key]) for key in sorted(value,key=lambda item:item.encode('utf-16-be',errors='surrogatepass')))+'}'
    raise ValueError('Invalid recovery value')


def digest(value):return hashlib.sha256(canonical(value).encode()).hexdigest()
def valid_id(value):return isinstance(value,str) and bool(ID.fullmatch(value)) and '..' not in value


def validate_binding(value):
    if not isinstance(value,dict) or set(value)!=FIELDS:raise ValueError('Invalid recovery binding')
    for key,item in value.items():
        if key.endswith('_sha'):
            if not isinstance(item,str) or not HASH.fullmatch(item):raise ValueError('Invalid recovery binding')
        elif not valid_id(item):raise ValueError('Invalid recovery binding')
    return value


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
    ''')
    columns={row[1] for row in store.connection.execute('PRAGMA table_info(recovery_attempts)')}
    for name,declaration in (('origin', "TEXT NOT NULL DEFAULT 'autonomous'"),('terminal_json','TEXT'),('untrusted','INTEGER NOT NULL DEFAULT 0'),('hold_reason','TEXT')):
        if name not in columns:store.connection.execute('ALTER TABLE recovery_attempts ADD COLUMN '+name+' '+declaration)


class RecoveryRecords:
    def __init__(self,store):self.store=store

    def _guard(self,binding,origin='autonomous'):
        validate_binding(binding)
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
        if self.store.pending_owner_input(int(self.store.get_state('local_recovery_owner_id','') or self.store.get_state('autonomous_owner_id','0') or '0')):raise ValueError('Recovery owner_blocked')
        if self.store.get_state('autonomy_error','') not in ('','local_recovery_interrupted'):raise ValueError('Recovery owner_blocked')

    def admit_scope(self,binding,*,origin='autonomous',evidence_only=False):
        with self.store._lock,self.store.connection:
            if evidence_only:
                validate_binding(binding)
                old=self.store.connection.execute('SELECT origin FROM recovery_attempts WHERE binding_sha=? ORDER BY rowid DESC LIMIT 1',(digest(binding),)).fetchone()
                if old is None or old['origin']!=origin:raise ValueError('Recovery legacy_unproven')
            else:self._guard(binding,origin)
            previous_attempt=self.store.connection.execute('SELECT backend_attempt_id FROM recovery_attempts WHERE binding_sha=? ORDER BY rowid DESC LIMIT 1',(digest(binding),)).fetchone()
            tainted=self.store.connection.execute('SELECT coalesce(max(untrusted),0) FROM recovery_attempts WHERE binding_sha=?',(digest(binding),)).fetchone()[0]
            token=secrets.token_hex(32);scope_id=uuid.uuid4().hex;binding_sha=digest(binding)
            self.store.connection.execute("UPDATE recovery_attempts SET state='revoked' WHERE binding_sha=? AND state NOT IN ('complete','revoked')",(binding_sha,))
            self.store.connection.execute("INSERT INTO recovery_attempts(admission_id,binding_sha,binding_json,token_sha,state,created_at,origin) VALUES(?,?,?,?,'active',?,?)",(scope_id,binding_sha,canonical(binding),hashlib.sha256(token.encode()).hexdigest(),self.store.now(),origin))
            self.store.connection.execute('UPDATE recovery_attempts SET untrusted=?,backend_attempt_id=? WHERE admission_id=?',(tainted,previous_attempt[0] if previous_attempt else None,scope_id))
            if evidence_only:self.store.connection.execute("UPDATE recovery_attempts SET state='evidence' WHERE admission_id=?",(scope_id,))
            return {'admission_id':scope_id,'scope_token':token}

    def _scope(self,binding,claim,token,*,dispatch=True):
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
            self._guard(binding,row['origin'])
            if row['state']!='active' or claim['fence']<row['fence']:raise ValueError('Recovery authority_lost')
        return row

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
                    if old['state']=='prepared' and old['reservation_id'] is None and claim['fence']>old['fence']:
                        self.store.connection.execute("UPDATE recovery_effects SET scope_id=?,fence=?,token_sha=? WHERE admission_id=? AND state='prepared'",
                            (scope['admission_id'],claim['fence'],hashlib.sha256(token.encode()).hexdigest(),effect_id))
                    else:return {'admission_id':effect_id,'state':'uncertain','token':None}
            else:
                self.store.connection.execute("INSERT INTO recovery_effects(admission_id,scope_id,binding_sha,kind,effect_id,request_sha,token_sha,fence,state,created_at) VALUES(?,?,?,?,?,?,?,?,'prepared',?)",(effect_id,scope['admission_id'],digest(binding),effect['kind'],effect['effect_id'],effect['request_sha'],hashlib.sha256(token.encode()).hexdigest(),claim['fence'],self.store.now()))
            self.store.connection.execute('UPDATE recovery_attempts SET fence=? WHERE admission_id=?',(claim['fence'],scope['admission_id']))
            return {'admission_id':effect_id,'state':old['state'] if old else 'prepared','token':token}

    def _validate_dispatch(self,metadata,request,kind):
        binding,claim=metadata['binding'],metadata['claim']
        self._scope(binding,claim,metadata['scope_token'])
        row=self.store.connection.execute('SELECT * FROM recovery_effects WHERE admission_id=?',(metadata['admission_id'],)).fetchone()
        if row is None or row['binding_sha']!=digest(binding) or row['scope_id']!=claim['admission_id'] or row['fence']!=claim['fence'] or row['kind'] not in kind or row['request_sha']!=digest(request):raise ValueError('Recovery binding_mismatch')
        if row['state']=='confirmed':return row
        token=metadata.get('token')
        if not isinstance(token,str) or not hmac.compare_digest(row['token_sha'],hashlib.sha256(token.encode()).hexdigest()):raise ValueError('Recovery authority_lost')
        return row

    def begin_provider(self,metadata,request,month,day,reserved):
        with self.store._lock,self.store.connection:
            self.store.connection.execute('BEGIN IMMEDIATE')
            row=self._validate_dispatch(metadata,request,{'provider','compaction'})
            if row['state']!='prepared':return None
            reservation_id='local-recovery-'+row['admission_id']
            self.store.connection.execute('INSERT INTO budget_reservations VALUES(?,?,?,?,?)',(reservation_id,month,day,reserved,self.store.now()))
            self.store.connection.execute("UPDATE recovery_effects SET state='admitted',reservation_id=? WHERE admission_id=?",(reservation_id,row['admission_id']))
            return reservation_id

    def begin_tool(self,metadata,name,arguments):
        with self.store._lock,self.store.connection:
            self.store.connection.execute('BEGIN IMMEDIATE')
            row=self._validate_dispatch(metadata,{'name':name,'arguments':arguments},{'tool'})
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
            row=self.store.connection.execute('SELECT r.* FROM recovery_results r JOIN recovery_effects e ON e.admission_id=r.admission_id WHERE r.admission_id=? AND e.state=\'confirmed\'',(admission_id,)).fetchone()
            if row is None:return {'state':'uncertain','admission_id':admission_id}
            usage=json.loads(row['usage_json']) if row['usage_json'] else None
            return {'state':'confirmed','admission_id':admission_id,'result_sha':row['result_sha'],'result':json.loads(row['result_json']),'usage':usage,
                **({'terminal_stop':usage['terminal_stop']} if isinstance(usage,dict) and usage.get('terminal_stop') in {'sleep','wake_at'} else {})}

    def complete_scope(self,admission_id,binding,terminal):
        validate_binding(binding)
        if (not isinstance(terminal,dict) or not valid_id(terminal.get('id')) or not valid_id(terminal.get('run_id'))
                or set(terminal)-{'id','run_id','consumer_id','stop_reason','assistant_ids','result_admission_id','lifecycle_stop','acknowledged','settled'}
                or ('lifecycle_stop' in terminal and terminal['lifecycle_stop'] not in {'sleep','wake_at'})
                or any(key in terminal and type(terminal[key]) is not bool for key in ('acknowledged','settled'))
                or terminal.get('consumer_id')!=binding['consumer_id'] or terminal.get('stop_reason')!='end_turn'
                or not isinstance(terminal.get('assistant_ids'),list) or not terminal['assistant_ids']
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
            if any(effect['state']!='confirmed' for effect in effects):raise ValueError('Recovery provider_uncertain')
            return {'settled':True,'binding':binding,'terminal_id':terminal_id,'admission_ids':sorted(effect['admission_id'] for effect in effects)}

    def revoke(self,admission_id):
        with self.store._lock,self.store.connection:self.store.connection.execute("UPDATE recovery_attempts SET state='revoked' WHERE admission_id=?",(admission_id,))

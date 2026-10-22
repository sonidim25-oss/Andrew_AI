"""Explicit local continuation on the existing serialized model worker."""
import json
import re
import threading
import time
import uuid
from dataclasses import asdict

from .appserver import AppServerClient, LettaCommandError, LettaTurnCancelled, LettaTurnYielded
from .recovery_records import RecoveryRecords, canonical, digest, validate_binding

CONTRACT='local_durable_recovery_v1'
HOLD_REASONS=frozenset({'legacy_unproven','binding_mismatch','unsupported_contract','provider_uncertain',
    'tool_uncertain','authority_lost','state_corrupt','storage_limit','owner_blocked',
    'configuration_changed','restore_required','projection_conflict','revision_conflict'})


class RecoveryInterrupted(RuntimeError):
    def __init__(self,category='authority_lost'):
        self.category=category if category in HOLD_REASONS else 'state_corrupt'
        super().__init__('Local recovery held: '+self.category)


def check_recovery_outcomes(store,binding,*,fresh=False,ignore_runner_attempt_id=None):
    with store._lock:
        if ignore_runner_attempt_id is not None:
            mapped=store.connection.execute("SELECT 1 FROM runner_operations o JOIN recovery_effects e ON e.admission_id=o.admission_id JOIN cycle_tool_attempts t ON t.attempt_id=o.legacy_attempt_id WHERE o.legacy_attempt_id=? AND o.binding_sha=? AND o.state='prepared' AND e.state='prepared' AND t.outcome='started'",(ignore_runner_attempt_id,digest(binding))).fetchone()
            if mapped is None:raise RecoveryInterrupted('binding_mismatch')
        # Never resolve an admitted effect from its absence in a transcript.
        unknown=store.connection.execute("SELECT kind FROM recovery_effects WHERE state='admitted' LIMIT 1").fetchone()
        if unknown:raise RecoveryInterrupted('tool_uncertain' if unknown['kind']=='tool' else 'provider_uncertain')
        # A durable rejection or confirmed provider result proves that this
        # binding already passed admission despite unrelated legacy costs.
        # Neither excuses a newer admitted effect checked above.
        admitted=store.connection.execute("SELECT 1 FROM recovery_effects e WHERE e.binding_sha=? AND (EXISTS (SELECT 1 FROM recovery_provider_rejections r WHERE r.admission_id=e.admission_id) OR (e.kind IN ('provider','compaction') AND e.state='confirmed' AND EXISTS (SELECT 1 FROM recovery_results r WHERE r.admission_id=e.admission_id))) LIMIT 1",(digest(binding),)).fetchone()
        for reservation in store.connection.execute('SELECT reservation_id FROM budget_reservations'):
            reservation_id=reservation['reservation_id']
            # A distinct newly journaled input is not a replay of a legacy
            # request. Keep its reserved cost and duplicate guard intact.
            # Resuming interrupted work still requires global reconciliation.
            legacy=bool(re.fullmatch(r'(?:provider-[a-f0-9]{64}|[a-f0-9]{32})',reservation_id))
            linked=store.connection.execute('SELECT 1 FROM recovery_effects WHERE reservation_id=? LIMIT 1',(reservation_id,)).fetchone()
            if (not fresh and not admitted) or not legacy or linked:raise RecoveryInterrupted('provider_uncertain')
        if store.cycle_uncertain_effects(binding['cycle_id'],ignore_attempt_id=ignore_runner_attempt_id):raise RecoveryInterrupted('tool_uncertain')
        if store.connection.execute("SELECT 1 FROM external_action_receipts WHERE state='uncertain' LIMIT 1").fetchone():raise RecoveryInterrupted('tool_uncertain')
        tables={row[0] for row in store.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table,condition in (('media_jobs',"state IN ('running','uncertain') OR delivery IN ('sending','uncertain')"),
                                ('media_avatar_proposals',"state IN ('applying','uncertain')")):
            if table in tables and store.connection.execute('SELECT 1 FROM '+table+' WHERE '+condition+' LIMIT 1').fetchone():raise RecoveryInterrupted('tool_uncertain')



class LocalRecoveryClient:
    def __init__(self,client):self.client=client
    def command(self,kind,**fields):
        try:return self.client.request({'type':kind,'contract':CONTRACT,'version':1,**fields},kind+'_response')
        except LettaCommandError as error:raise RecoveryInterrupted(error.safe_category) from None
        except (OSError,TimeoutError,RuntimeError):raise RecoveryInterrupted('authority_lost') from None
    def capabilities(self):
        result=self.command('recovery_capabilities')
        if result.get('contract')!=CONTRACT or result.get('version')!=1:raise RecoveryInterrupted('unsupported_contract')
        return result
    def inspect(self,binding):return self.command('recovery_inspect',binding=binding)
    def claim(self,binding,revision,scope):return self.command('recovery_claim',binding=binding,expected_revision=revision,**scope)
    def resume(self,intent,scope):return self.command('recovery_resume',**intent,**scope)
    def status(self,binding,attempt_id):return self.command('recovery_status',binding=binding,attempt_id=attempt_id)
    def subscribe(self,binding,attempt_id):return self.command('recovery_subscribe',binding=binding,attempt_id=attempt_id)
    def result_read(self,binding,scope,claim,effect_id):
        return self.command('recovery_result_read',binding=binding,**scope,claim_id=claim['claim_id'],fence=claim['fence'],effect_admission_id=effect_id)
    def reconcile_tool(self,binding,scope,claim,admission_id,expected_revision):
        return self.command('recovery_reconcile_tool',binding=binding,**scope,claim_id=claim['claim_id'],
            fence=claim['fence'],expected_revision=expected_revision,effect_admission_id=admission_id)
    def revoke(self,binding,scope,claim):
        return self.command('recovery_revoke',binding=binding,**scope,claim_id=claim['claim_id'],fence=claim['fence'])
    def release(self,binding,scope,claim):
        return self.command('recovery_release',binding=binding,**scope,claim_id=claim['claim_id'],fence=claim['fence'])
    def ack(self,binding,scope,claim,terminal):
        current=self.inspect(binding)
        return self.command('recovery_ack',binding=binding,**scope,claim_id=claim['claim_id'],fence=claim['fence'],
            expected_revision=current['revision'],terminal_id=terminal['id'],consumer_id=binding['consumer_id'])
    def close(self):self.client.close()


class RecoveryCoordinator:
    def __init__(self,store,controller,client_factory=None,*,enabled=False,config_sha=None):
        self.store,self.controller,self.enabled=store,controller,bool(enabled)
        self.records=RecoveryRecords(store)
        self.client_factory=client_factory or (lambda:LocalRecoveryClient(AppServerClient(controller.model.url)))
        self.config_sha=config_sha
        self._active_lock=threading.Lock();self._active_client=None;self._active_scope=None;self._active_cancel=None
        if self.enabled:
            self.store.set_state('local_recovery_owner_id',str(controller.owner_id))
            if not self.store.get_state('local_recovery_controller_store_id',''):self.store.set_state('local_recovery_controller_store_id',uuid.uuid4().hex)

    def configuration_digest(self,client):
        if self.config_sha:return self.config_sha
        agent=self.controller.model
        return digest({'contract':CONTRACT,'model':asdict(agent.model_config),'system':agent.system_prompt,
            'tools':self.tools(client)})

    @staticmethod
    def tools(client):
        return [{'type':'function','function':{key:tool[key] for key in ('name','description','parameters')}}
            for group in client.CONTROLLER_TOOLS for tool in group['tools'] if tool['name'] in client.tool_names]

    def _pending(self):
        with self.store._lock:
            return [dict(row) for row in self.store.connection.execute(
                "SELECT * FROM recovery_attempts WHERE state IN ('active','evidence','held','yielding') ORDER BY rowid LIMIT 64")]

    def has_pending(self):return self.enabled and bool(self._pending())

    def _state(self,scope_id,state,*,attempt_id=None,revision=None):
        with self.store._lock,self.store.connection:
            self.store.connection.execute('UPDATE recovery_attempts SET state=?,backend_attempt_id=coalesce(?,backend_attempt_id),backend_revision=coalesce(?,backend_revision) WHERE admission_id=?',
                (state,attempt_id,revision,scope_id))

    def _activate(self,client,scope,cancelled=None):
        with self._active_lock:
            if self._active_client is not None:raise RecoveryInterrupted('authority_lost')
            self._active_client=client;self._active_scope=scope;self._active_cancel=cancelled or threading.Event()

    def cancel_active(self):
        with self._active_lock:
            client,scope=self._active_client,self._active_scope
            if self._active_cancel:self._active_cancel.set()
        if scope:self.records.revoke(scope['admission_id'])
        if client:client.close()

    def _deactivate(self):
        with self._active_lock:self._active_client=self._active_scope=self._active_cancel=None

    def _check_outcomes(self,binding,*,fresh=False):
        return check_recovery_outcomes(self.store,binding,fresh=fresh)

    def _previous(self,runtime):
        with self.store._lock:
            rows=self.store.connection.execute("SELECT binding_json FROM recovery_attempts WHERE state IN ('complete','yielded') ORDER BY rowid DESC LIMIT 64").fetchall()
        for row in rows:
            binding=json.loads(row['binding_json'])
            if all(binding[key]==runtime[key] for key in ('agent_id','conversation_id')):return binding
        return None

    def _accepted(self,binding,origin):
        if origin=='autonomous':self.store.record_model_turn_receipt(binding['session_id'],binding['cycle_id'],binding,
            {'kind':'accepted','request_id':binding['consumer_id']})

    def _finish(self,client,binding,scope,claim,state,origin):
        if state.get('binding')!=binding or state.get('phase')!='terminal_committed':raise RecoveryInterrupted('binding_mismatch')
        terminal=state.get('terminal')
        if not isinstance(terminal,dict):raise RecoveryInterrupted('state_corrupt')
        self.records.complete_scope(scope['admission_id'],binding,terminal)
        yielded=terminal.get('stop_reason')=='owner_input_pending'
        if not yielded:
            proof=self.records.effect_result(terminal['result_admission_id'])
            if proof['state']!='confirmed':raise RecoveryInterrupted('provider_uncertain')
            self._accepted(binding,origin)
        if origin=='autonomous' and not yielded:
            event={'kind':'completed','request_id':binding['consumer_id'],'assistant_ids':terminal['assistant_ids'],
                'terminal':{'type':'turn_finished','stop_reason':'end_turn','run_id':terminal['run_id'],
                    'turn_id':terminal['id'],'terminal_consumer_ids':[binding['consumer_id']]}}
            with self.store._lock:
                receipt=self.store.connection.execute('SELECT state FROM model_turn_receipts WHERE cycle_id=?',(binding['cycle_id'],)).fetchone()
            if receipt is None:raise RecoveryInterrupted('binding_mismatch')
            if receipt['state']!='completed' and not self.store.record_model_turn_receipt(binding['session_id'],binding['cycle_id'],binding,event):raise RecoveryInterrupted('binding_mismatch')
        from .memory import SessionHistory
        history=SessionHistory(self.store,self.controller.owner_id,session_id=binding['session_id'],cycle_id=binding['cycle_id'],
            conversation_id=binding['conversation_id'],turn_id=binding['client_message_id'])
        with self.store._lock:
            results=self.store.connection.execute("SELECT e.effect_id,e.kind,r.result_json FROM recovery_effects e JOIN recovery_results r ON r.admission_id=e.admission_id WHERE e.binding_sha=? ORDER BY e.rowid",(digest(binding),)).fetchall()
        for result in results:
            value=json.loads(result['result_json'])
            if result['kind']=='provider':
                content=value['choices'][0]['message'].get('content')
                if content:history.capture({'kind':'assistant_final','source_id':result['effect_id'],'content':content})
            elif result['kind']=='tool':history.capture({'kind':'tool_result','source_id':result['effect_id'],'content':str(value)})
        # Acknowledgement does not acknowledge Telegram delivery or clear any other hold.
        client.ack(binding,scope,claim,terminal)
        client.release(binding,scope,claim)
        if yielded:
            self.records.finish_owner_yield(scope['admission_id'],binding)
            raise LettaTurnYielded('Owner input is waiting')
        self._state(scope['admission_id'],'complete',revision=client.inspect(binding)['revision'])
        if terminal.get('lifecycle_stop') in {'sleep','wake_at'}:return str(proof['result'])
        response=proof['result']
        try:text=response['choices'][0]['message']['content']
        except (KeyError,IndexError,TypeError):raise RecoveryInterrupted('state_corrupt') from None
        if not isinstance(text,str) or not text.strip():raise RecoveryInterrupted('state_corrupt')
        return text

    def _drive(self,client,binding,scope,state,origin,cancelled,*,old_attempt=None,fresh=False):
        if old_attempt:
            state=client.status(binding,old_attempt)
            if state.get('state')=='running':raise RecoveryInterrupted('authority_lost')
        if state.get('status') not in {'found','held'} or state.get('binding')!=binding:raise RecoveryInterrupted(state.get('hold_reason','binding_mismatch'))
        if state.get('phase')=='held':raise RecoveryInterrupted(state.get('hold_reason'))
        if state.get('phase')!='terminal_committed':
            if fresh and (old_attempt or state.get('phase')!='input_committed'):raise RecoveryInterrupted('binding_mismatch')
            self._check_outcomes(binding,fresh=fresh)
        if cancelled.is_set():raise LettaTurnCancelled('Owner cancelled this turn')
        claim=client.claim(binding,state['revision'],scope)
        if state.get('phase')=='terminal_committed':return self._finish(client,binding,scope,claim,client.inspect(binding),origin)
        attempt_id=uuid.uuid4().hex
        intent={'binding':binding,'attempt_id':attempt_id,'claim_id':claim['claim_id'],'fence':claim['fence'],'expected_revision':claim['revision']}
        self._state(scope['admission_id'],'active',attempt_id=attempt_id,revision=claim['revision'])
        client.resume(intent,scope)
        while True:
            if cancelled.is_set():self.cancel_active();raise LettaTurnCancelled('Owner cancelled this turn')
            state=client.status(binding,attempt_id)
            if state.get('phase')=='held':
                raise RecoveryInterrupted(state.get('hold_reason'))
            if state.get('phase')=='terminal_committed':return self._finish(client,binding,scope,claim,state,origin)
            if state.get('state')=='interrupted':raise RecoveryInterrupted('authority_lost')
            cancelled.wait(.05)

    def run_turn(self,transport,runtime,text,*,origin='autonomous',cancelled=None):
        if not self.enabled:raise RecoveryInterrupted('unsupported_contract')
        cancelled=cancelled or threading.Event()
        client=LocalRecoveryClient(transport);capability=client.capabilities()
        if self.has_pending():raise RecoveryInterrupted('owner_blocked')
        config_sha=self.configuration_digest(transport);self.store.set_state('local_recovery_config_sha',config_sha)
        input={'model':self.controller.model.model_config.model_id,'messages':[{'role':'user','content':text}],
            'tools':self.tools(transport),'parallel_tool_calls':False}
        turn_id=uuid.uuid4().hex
        binding={'store_id':capability['store_id'],**runtime,'session_id':self.store.get_state('active_session_id','') if origin=='autonomous' else 'owner-'+turn_id,
            'cycle_id':self.store.get_state('active_cycle_id','') if origin=='autonomous' else 'owner-'+turn_id,
            'client_message_id':uuid.uuid4().hex,'consumer_id':uuid.uuid4().hex,'input_sha':digest(input),
            'owner_revision':self.store.get_state('owner_control_revision','0'),'config_sha':config_sha}
        validate_binding(binding)
        if origin=='autonomous':self.store.record_model_turn_receipt(binding['session_id'],binding['cycle_id'],runtime,
            {'kind':'prepared','request_id':binding['consumer_id'],'client_message_id':binding['client_message_id']})
        self._check_outcomes(binding,fresh=True);scope=self.records.admit_scope(binding,origin=origin);self._activate(client,scope,cancelled)
        try:
            previous=self._previous(runtime);extra={}
            if previous:
                state=client.inspect(previous)
                extra={'previous_binding':previous,'expected_revision':state['revision']}
            state=client.command('recovery_admit',binding=binding,input=input,**scope,**extra)
            self._accepted(binding,origin)
            return self._drive(client,binding,scope,state,origin,cancelled,fresh=True)
        except (OSError,TimeoutError,ConnectionError):raise RecoveryInterrupted('authority_lost') from None
        finally:self._deactivate()

    def inspect_pending(self):
        if not self.enabled:return []
        client=self.client_factory()
        try:
            capability=client.capabilities();result=[]
            for row in self._pending():
                binding=validate_binding(json.loads(row['binding_json']))
                state=client.inspect(binding) if binding['store_id']==capability['store_id'] else {'status':'held','hold_reason':'binding_mismatch'}
                result.append({'admission_id':row['admission_id'],'cycle_id':binding['cycle_id'],
                    'phase':state.get('phase'),'hold_reason':state.get('hold_reason'),'revision':state.get('revision')})
            return result
        finally:client.close()

    def _late_provider_receipt(self,client,binding,row,state,capability):
        if (capability.get('late_provider_receipt')!=1 or state.get('phase')!='held'
                or state.get('hold_reason')!='provider_uncertain' or state.get('state')=='running'):return False
        ids=state.get('effect_admission_ids')
        if not isinstance(ids,list) or not ids:return False
        # Effect IDs come from the bound backend checkpoint; the protocol still
        # verifies the exact last admitted provider, digest and settled receipt.
        with self.store._lock:
            effect=self.store.connection.execute("SELECT * FROM recovery_effects WHERE binding_sha=? AND kind IN ('provider','compaction') ORDER BY rowid DESC LIMIT 1",(digest(binding),)).fetchone()
            if effect is None or effect['admission_id'] not in ids:return False
            proof=self.records.effect_result(effect['admission_id'])
            if not (proof['state']=='rejected' or (proof['state']=='confirmed' and isinstance(proof.get('usage'),dict) and proof['usage'].get('settled') is True)):return False
        scope=self.records.admit_scope(binding,origin=row['origin'],evidence_only=True)
        self._activate(client,scope)
        claim=client.command('recovery_reconcile_provider',binding=binding,expected_revision=state['revision'],effect_admission_id=effect['admission_id'],**scope)
        client.release(binding,scope,claim)
        self._state(scope['admission_id'],'active',revision=client.inspect(binding)['revision'])
        self.store.add_activity('Late provider receipt recovered without request or tool dispatch')
        return True

    def _runner_tool_receipt(self,client,binding,row,state,capability):
        if (capability.get('runner_result_recovery_v1')!=1 or capability.get('held_tool_result_v1')!=1
                or state.get('state')=='running' or not self.controller.autonomous_enabled):return False
        held=state.get('phase')=='held' and state.get('hold_reason')=='tool_uncertain'
        repeated=state.get('phase')=='tool_confirmed' and row['state']=='held'
        if not held and not repeated:return False
        ids=state.get('effect_admission_ids')
        if not isinstance(ids,list):return False
        with self.store._lock:
            rows=self.store.connection.execute('''SELECT o.admission_id FROM runner_operations o
                JOIN runner_imports i USING(operation_id) WHERE o.binding_sha=? AND o.state='imported'
                ORDER BY o.rowid DESC''',(digest(binding),)).fetchall()
            candidates=[item['admission_id'] for item in rows if item['admission_id'] in ids]
            if not candidates:return False
            admission_id=candidates[0]
            proof=self.records.effect_result(admission_id)
            if proof.get('state')!='confirmed' or not proof.get('runner_receipt'):return False
            if repeated and admission_id not in state.get('runner_receipt_admissions',[]):return False
        if held and capability.get('runner_tool_evidence_v1')==1:
            # Import is historical evidence only. Continuing afterwards still
            # requires the same guards as every other resumed autonomous turn,
            # and waiting owner input can then take over through the handoff.
            scope=self.records.admit_scope(binding,origin=row['origin'],evidence_only=True)
            self._activate(client,scope)
            claim=client.command('recovery_import_runner_tool',binding=binding,expected_revision=state['revision'],effect_admission_id=admission_id,**scope)
            client.release(binding,scope,claim)
            self._state(scope['admission_id'],'active',revision=client.inspect(binding)['revision'])
            self.store.add_activity('Saved runner result imported without executing the tool again')
            return True
        # Import is historical. Acquiring new authority and continuing require
        # the same guards as every other resumed autonomous turn.
        self._check_outcomes(binding)
        with self.store._lock:self.records._guard(binding)
        scope=self.records.admit_scope(binding,origin=row['origin'])
        self._activate(client,scope)
        claim=client.claim(binding,state['revision'],scope)
        try:
            client.reconcile_tool(binding,scope,claim,admission_id,claim['revision'])
        finally:client.release(binding,scope,claim)
        self._state(scope['admission_id'],'active',revision=client.inspect(binding)['revision'])
        self.store.add_activity('Saved runner result confirmed without executing the tool again')
        return True

    def reconcile_one(self):
        if not self.enabled:return False
        pending=self._pending()
        if not pending:return False
        row=pending[0]
        if row['origin']!='autonomous':return False
        binding=validate_binding(json.loads(row['binding_json']))
        from .runner_recovery import RunnerRecoveryAdapter,reconcile_pending_runner_result
        adapter=getattr(self.controller,'runner_recovery',None)
        if isinstance(adapter,RunnerRecoveryAdapter) and reconcile_pending_runner_result(
                self.store,adapter.client,binding,expected_runner_uid=adapter.expected_runner_uid):return True
        client=self.client_factory()
        try:
            capability=client.capabilities()
            if capability['store_id']!=binding['store_id']:raise RecoveryInterrupted('binding_mismatch')
            state=client.status(binding,row['backend_attempt_id']) if row['backend_attempt_id'] else client.inspect(binding)
            terminal=state.get('phase')=='terminal_committed'
            if self._late_provider_receipt(client,binding,row,state,capability):return True
            if self._runner_tool_receipt(client,binding,row,state,capability):return True
            if (not terminal and capability.get('owner_handoff')==1
                    and self.store.pending_owner_input(self.controller.owner_id)
                    and (state.get('phase')!='held' or state.get('hold_reason') in {'owner_blocked','provider_uncertain','tool_uncertain'})):
                if state.get('state')=='running':return False
                scope=self.records.admit_scope(binding,origin=row['origin'],yield_only=True)
                self._activate(client,scope)
                claim=client.command('recovery_yield',binding=binding,expected_revision=state['revision'],**scope)
                return self._finish(client,binding,scope,claim,client.inspect(binding),row['origin'])
            if row['state']=='held' and not terminal:return False
            if not terminal:
                if not self.controller.autonomous_enabled:return False
                self._check_outcomes(binding)
                with self.store._lock:self.records._guard(binding)
            scope=self.records.admit_scope(binding,origin=row['origin'],evidence_only=terminal)
            self._activate(client,scope)
            self._drive(client,binding,scope,state,row['origin'],threading.Event(),old_attempt=row['backend_attempt_id'])
            if (self.store.get_state('autonomy_error','')=='local_recovery_interrupted'
                    and self.store.get_state('active_session_id','')==binding['session_id']
                    and self.store.get_state('owner_control_revision','0')==binding['owner_revision']
                    and not any(self.store.get_state(key,'0')=='1' for key in ('paused','sleeping','restore_recovery_required'))):
                self.store.set_state('autonomy_error','')
                self.store.promote_completed_model_turn(binding['session_id'])
                self.store.recover_confirmed_session(binding['session_id'])
            return True
        except LettaTurnYielded:
            return True
        except RecoveryInterrupted as error:
            if error.category in {'provider_uncertain','tool_uncertain','state_corrupt','storage_limit','restore_required','projection_conflict','legacy_unproven'}:
                with self.store._lock,self.store.connection:
                    self.store.connection.execute("UPDATE recovery_attempts SET state='held',hold_reason=? WHERE binding_sha=? AND state IN ('active','evidence','held')",(error.category,digest(binding)))
            return False
        except (ValueError,OSError,TimeoutError):return False
        finally:self._deactivate();client.close()

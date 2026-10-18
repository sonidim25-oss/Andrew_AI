"""Controller-only effect dispatch gate shared by normal and resumed journaled steps."""
from .recovery_records import RecoveryRecords,digest


class RecoveryDispatch:
    def __init__(self,store):self.store=store;self.records=RecoveryRecords(store)
    def authorize(self,binding,claim,effect,scope_token):return self.records.admit_effect(binding,claim,effect,scope_token)
    def result(self,admission_id):return self.records.effect_result(admission_id)
    def run_tool(self,metadata,name,arguments,callback):
        proof=self.records.effect_result(metadata['admission_id'])
        if proof['state']=='confirmed':
            with self.store._lock:self.records._validate_dispatch(metadata,{'name':name,'arguments':arguments},{'tool'})
            return proof['result']
        admission_id=self.records.begin_tool(metadata,name,arguments)
        from .memory import MEMORY_SOURCE_CONTEXT
        with self.store._lock:
            tainted=self.store.connection.execute('SELECT coalesce(max(untrusted),0) FROM recovery_attempts WHERE binding_sha=?',(digest(metadata['binding']),)).fetchone()[0]
        context={'untrusted':bool(tainted)};token=MEMORY_SOURCE_CONTEXT.set(context)
        try:
            result=callback()
        finally:
            MEMORY_SOURCE_CONTEXT.reset(token)
            if context['untrusted']:
                with self.store._lock,self.store.connection:
                    self.store.connection.execute('UPDATE recovery_attempts SET untrusted=1 WHERE binding_sha=?',(digest(metadata['binding']),))
        self.records.confirm_tool(admission_id,result,terminal_stop=name if name in {'sleep','wake_at'} and self.store.get_state('sleeping','0')=='1' else None)
        return result

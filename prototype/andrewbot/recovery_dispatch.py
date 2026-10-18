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
        result=callback()
        self.records.confirm_tool(admission_id,result)
        return result

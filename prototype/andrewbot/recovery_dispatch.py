"""Controller-only effect dispatch gate shared by normal and resumed journaled steps."""
from contextlib import contextmanager
from contextvars import ContextVar
import json
from .recovery_records import RecoveryRecords,digest
from .tool_errors import ToolRejected


_LOCAL_TOOL_AUTHORITY = ContextVar('local_recovery_tool_authority', default=None)
_LOCAL_MUTATIONS = frozenset({'research.save', 'research.focus', 'research.delete',
                              'sleep', 'wake_at', 'continue_activity'})


def authorized_interrupted_tool(store, names):
    """Recheck exact admitted authority at a local mutation, never a global bypass."""
    authority = _LOCAL_TOOL_AUTHORITY.get()
    if (authority is None or authority['store'] is not store
            or authority['request']['name'] not in names):
        return False
    # Controller bookkeeping can commit before reaching the mutation. Acquire
    # the writer again before validating owner controls against another store.
    if not store.connection.in_transaction:
        store.connection.execute('BEGIN IMMEDIATE')
    row = authority['records']._validate_dispatch(
        authority['metadata'], authority['request'], {'tool'})
    if row['state'] != 'admitted':
        raise ValueError('Recovery authority_lost')
    return True


class RecoveryDispatch:
    def __init__(self,store,runner_adapter=None):self.store=store;self.records=RecoveryRecords(store);self.runner_adapter=runner_adapter
    def authorize(self,binding,claim,effect,scope_token):return self.records.admit_effect(binding,claim,effect,scope_token)
    def result(self,admission_id):return self.records.effect_result(admission_id)
    @contextmanager
    def _local_authority(self, metadata, name, arguments, origin):
        if name not in _LOCAL_MUTATIONS or origin is None or origin['origin'] != 'autonomous':
            yield
            return
        # Only these local callbacks run under the database lock. External tools
        # retain their existing execution and uncertain-outcome handling.
        with self.store._lock, self.store.connection:
            token = _LOCAL_TOOL_AUTHORITY.set({'store': self.store, 'records': self.records,
                'metadata': metadata, 'request': {'name': name, 'arguments': arguments}})
            try:
                yield
            finally:
                _LOCAL_TOOL_AUTHORITY.reset(token)
    def run_tool(self,metadata,name,arguments,callback):
        from .runner_protocol import TOOLS
        with self.store._lock:
            mapped=self.store.connection.execute('SELECT 1 FROM runner_operations WHERE admission_id=?',(metadata['admission_id'],)).fetchone()
            origin=self.store.connection.execute('SELECT origin FROM recovery_attempts WHERE admission_id=?',(metadata['claim']['admission_id'],)).fetchone()
        if (name in TOOLS and origin is not None and origin['origin']=='autonomous'
                and self.store.get_state('runner_receipt_store_id','') and self.runner_adapter is None):
            raise ValueError('Recovery unsupported_contract')
        if mapped or (name in TOOLS and self.runner_adapter is not None and origin is not None and origin['origin']=='autonomous'):
            if self.runner_adapter is None:raise ValueError('Recovery unsupported_contract')
            return self.runner_adapter.execute(metadata,name,arguments)
        proof=self.records.effect_result(metadata['admission_id'])
        if proof['state']=='confirmed':
            with self.store._lock:self.records._validate_dispatch(metadata,{'name':name,'arguments':arguments},{'tool'})
            return proof['result']
        admission_id=self.records.begin_tool(metadata,name,arguments)
        from .memory import MEMORY_SOURCE_CONTEXT
        with self.store._lock:
            tainted=self.store.connection.execute('SELECT coalesce(max(untrusted),0) FROM recovery_attempts WHERE binding_sha=?',(digest(metadata['binding']),)).fetchone()[0]
        context={'untrusted':bool(tainted)};token=MEMORY_SOURCE_CONTEXT.set(context)
        rejected=False
        try:
            try:
                with self._local_authority(metadata, name, arguments, origin):
                    result=callback()
            except ToolRejected as error:
                # Only audited pre-effect rejection sites can settle an error.
                # Authority validation above and unknown failures still escape.
                result=json.dumps(error.result(), ensure_ascii=False)
                rejected=True
        finally:
            MEMORY_SOURCE_CONTEXT.reset(token)
            if context['untrusted']:
                with self.store._lock,self.store.connection:
                    self.store.connection.execute('UPDATE recovery_attempts SET untrusted=1 WHERE binding_sha=?',(digest(metadata['binding']),))
        self.records.confirm_tool(admission_id,result,terminal_stop=name if not rejected and name in {'sleep','wake_at'} and self.store.get_state('sleeping','0')=='1' else None)
        return result

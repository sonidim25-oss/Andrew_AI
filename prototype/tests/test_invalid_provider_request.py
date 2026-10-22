"""A proven invalid wire name is permanent rejection, never a retry permit."""
import unittest
from andrewbot.model import ProviderRequestError
from andrewbot.recovery_records import digest
import test_recovery_dispatch as fixture


class InvalidProviderRequestTests(unittest.TestCase):
    setUp=fixture.RecoveryDispatchTests.setUp
    metadata=fixture.RecoveryDispatchTests.metadata

    def rejected_request(self):
        self.payload={'model':self.gateway.model_id,'messages':[{'role':'user','content':'fixture'}],
                      'tools':[{'type':'function','function':{'name':'python.run','parameters':{'type':'object'}}}]}
        metadata=self.metadata()
        self.provider.complete.side_effect=ProviderRequestError('http_error',400)
        self.assertEqual(self.gateway.complete('Bearer fixture-token',self.payload,recovery=metadata)[0],422)
        activity=self.store.connection.execute('SELECT max(id) FROM activity').fetchone()[0]
        for key in ('agent_id','conversation_id'):self.store.set_state('letta_'+key,self.binding[key])
        self.controller.handle_update({'message':{'from':{'id':17},'chat':{'id':17,'type':'private'},'text':'/pause'}})
        self.records.revoke(self.scope['admission_id'])
        evidence={'http_status':400,'type':'invalid_request_error','code':'invalid_value','parameter':'input[3].name',
                  'pattern':'^[a-zA-Z0-9_-]+$','diagnostic_sha':'d'*64,'activity_id':activity,'adapter':'unmapped_tool_names_v1'}
        return metadata,evidence

    def test_explicit_invalid_name_rejection_keeps_conservative_cost_and_yields(self):
        metadata,evidence=self.rejected_request()
        cost=self.store.connection.execute('SELECT cost_usd FROM budget_reservations').fetchone()[0]
        self.store.reserve_usage('unrelated-reservation','2026-10','2026-10-18',.012)
        proof=self.records.reject_invalid_tool_names(metadata['admission_id'],self.payload,evidence)
        self.assertEqual(proof['reason'],'invalid_tool_name');self.assertFalse(proof['retryable'])
        self.assertEqual(proof['http_status'],400);self.assertEqual(proof['request_sha'],digest(self.payload))
        self.assertEqual(self.store.connection.execute('SELECT cost_usd FROM usage').fetchone()[0],cost)
        self.assertEqual(self.store.connection.execute('SELECT reservation_id FROM budget_reservations').fetchone()[0],'unrelated-reservation')
        scope=self.records.admit_scope(self.binding,yield_only=True)
        claim={**self.claim,'admission_id':scope['admission_id'],'fence':2}
        self.assertEqual(self.records.prepare_owner_yield(self.binding,claim,scope['scope_token'])['state'],'owner_yield')

    def test_invalid_evidence_or_changed_request_keeps_uncertainty_and_reservation(self):
        metadata,evidence=self.rejected_request()
        for change in ({'http_status':503},{'code':'context_length_exceeded'},{'pattern':'anything'},
                       {'adapter':'mapped_tool_names_v1'},{'activity_id':0},{'diagnostic_sha':'bad'}):
            with self.subTest(change=change),self.assertRaises(ValueError):
                self.records.reject_invalid_tool_names(metadata['admission_id'],self.payload,{**evidence,**change})
        with self.assertRaises(ValueError):self.records.reject_invalid_tool_names(metadata['admission_id'],{**self.payload,'tools':[]},evidence)
        self.assertEqual(self.records.effect_result(metadata['admission_id'])['state'],'uncertain')
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM budget_reservations').fetchone()[0],1)

    def test_current_owner_or_restore_fence_refuses_maintenance_rejection(self):
        metadata,evidence=self.rejected_request()
        for key,value in [('paused','0'),('owner_control_revision','0'),('active_session_id','other-session'),
                          ('model_turn_origin','owner'),('restore_recovery_required','1')]:
            old=self.store.get_state(key,'');self.store.set_state(key,value)
            try:
                with self.assertRaises(ValueError):self.records.reject_invalid_tool_names(metadata['admission_id'],self.payload,evidence)
            finally:self.store.set_state(key,old)
        (self.store.path.parent/'runner-restore-required.json').write_text('{}')
        with self.assertRaises(ValueError):self.records.reject_invalid_tool_names(metadata['admission_id'],self.payload,evidence)

    def test_permanent_rejection_cannot_dispatch_even_if_old_authority_is_restored(self):
        metadata,evidence=self.rejected_request()
        self.records.reject_invalid_tool_names(metadata['admission_id'],self.payload,evidence)
        for key,value in {'paused':'0','owner_control_revision':'0','active_session_id':self.binding['session_id'],
                          'active_cycle_id':self.binding['cycle_id'],'autonomy_error':''}.items():
            self.store.set_state(key,value)
        with self.store.connection:self.store.connection.execute("UPDATE recovery_attempts SET state='active' WHERE admission_id=?",(self.scope['admission_id'],))
        self.gateway.complete('Bearer fixture-token',self.payload,recovery=metadata)
        self.assertEqual(self.records.effect_result(metadata['admission_id'])['state'],'rejected')
        self.assertEqual(self.provider.complete.call_count,1)

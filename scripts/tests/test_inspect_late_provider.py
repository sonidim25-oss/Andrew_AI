import json
import unittest
import test_inspect_owner_handoff as handoff_fixture
from inspect_owner_handoff import plan

class LateProviderInspectionTests(unittest.TestCase):
    fixture_setup=handoff_fixture.HandoffInspectionTests.fixture_setup
    metadata=handoff_fixture.HandoffInspectionTests.metadata
    save=handoff_fixture.HandoffInspectionTests.save
    def setUp(self):
        handoff_fixture.HandoffInspectionTests.setUp(self)
        self.snapshot['hold_reason']='provider_uncertain';self.snapshot['held_phase']='provider_admitted'
        self.snapshot['steps'][0].update(state='admitted',kind='provider');self.snapshot['steps'][0].pop('result_ref')
        self.snapshot['tools']=[];self.save()
        self.store.connection.execute('DELETE FROM messages')
        self.store.set_state('model_turn_progress',json.dumps({'phase':'processing the model response','at':1}))
    def inspect(self):return plan(self.path,self.home,self.scope['admission_id'],late_provider=True)
    def test_late_settled_result_eligible_without_pending_owner_and_preserves_stale_progress(self):
        before='\n'.join(self.store.connection.iterdump())
        result=self.inspect();self.assertEqual(result['mode'],'late_provider_receipt')
        self.assertEqual(result['late_admission_id'],self.effect)
        self.assertEqual(before,'\n'.join(self.store.connection.iterdump()))
    def test_newer_settled_pause_allows_receipt_import_without_restoring_authority(self):
        for key in ('agent_id','conversation_id'):self.store.set_state('letta_'+key,self.binding[key])
        self.controller.handle_update({'message':{'from':{'id':17},'chat':{'id':17,'type':'private'},'text':'/pause'}})
        result=self.inspect()
        self.assertEqual(result['mode'],'late_provider_receipt')
        self.assertEqual(result['owner_revision'],'1')
        self.assertEqual(self.store.get_state('paused'),'1')
        self.assertEqual(self.store.get_state('active_session_id'),'')
        self.store.interrupt_unfinished_cycles()
        self.assertEqual(self.inspect(),result)
        self.store.set_state('model_turn_progress','')
        self.assertEqual(self.inspect(),result)
        self.store.set_state('paused','0')
        with self.assertRaises(ValueError):self.inspect()

    def test_unsettled_usage_unknown_result_or_active_request_rejected(self):
        saved=[tuple(row) for row in self.store.connection.execute('SELECT * FROM recovery_results')]
        for sql in ("UPDATE recovery_results SET usage_json='{}'","DELETE FROM recovery_results","UPDATE recovery_effects SET state='admitted'"):
            with self.store.connection:self.store.connection.execute(sql)
            with self.assertRaises(ValueError):self.inspect()
            with self.store.connection:
                self.store.connection.execute('DELETE FROM recovery_results')
                self.store.connection.executemany('INSERT INTO recovery_results VALUES(?,?,?,?,?)',saved)
                self.store.connection.execute("UPDATE recovery_effects SET state='confirmed'")
    def test_only_last_provider_hold_can_be_imported(self):
        self.snapshot['hold_reason']='tool_uncertain';self.save()
        with self.assertRaises(ValueError):self.inspect()
    def test_changed_owner_authority_is_rejected_without_rebinding(self):
        self.store.set_state('owner_control_revision','99')
        with self.assertRaises(ValueError):self.inspect()


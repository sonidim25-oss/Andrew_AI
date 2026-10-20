import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from recovery_projection_metadata import transcript_structure, checkpoint_structure


class ProjectionMetadataTests(unittest.TestCase):
    def test_checkpoint_labels_do_not_expose_model_content(self):
        snapshot={'phase':'held','held_phase':'tool_prepared','hold_reason':'owner_blocked',
                  'revision':42,'binding':{'client_message_id':'input-fixture'},
                  'steps':[{'state':'confirmed'}],
                  'tools':[{'name':'recent_activity','state':'confirmed','arguments':'private argument'},
                           {'name':'private invented name','state':'prepared'}]}
        result=checkpoint_structure(snapshot,{'client_message_id':'input-fixture'})
        self.assertEqual(result['hold_reason'],'owner_blocked')
        self.assertTrue(result['input_matches'])
        self.assertEqual(result['tool_states'],{'confirmed':1,'prepared':1})
        self.assertNotIn('private',str(result))

    def test_checkpoint_foreign_input_and_unrecognized_labels_are_visible_safely(self):
        result=checkpoint_structure({'phase':'private phase','hold_reason':'private error',
                                    'binding':{'client_message_id':'foreign'},'steps':[],'tools':[]},
                                    {'client_message_id':'expected'})
        self.assertFalse(result['input_matches'])
        self.assertEqual(result['phase'],'other')
        self.assertEqual(result['hold_reason'],'other')
        self.assertNotIn('private',str(result))

    def test_plain_history_matches_active(self):
        result=transcript_structure({'in_context_message_ids':['a']},
            [{'type':'message','message':{'id':'a','content':'private text'}}])
        self.assertTrue(result['raw_prefix_matches_active'])
        self.assertNotIn('private text',str(result))

    def test_updated_message_entries_are_counted_without_exposing_content(self):
        result=transcript_structure({'in_context_message_ids':['a']},[
            {'type':'message','message':{'id':'a','content':'private original'}},
            {'type':'message','message':{'id':'a','content':'private replacement'}}])
        self.assertEqual(result['message_entries'],2)
        self.assertEqual(result['unique_message_ids'],1)
        self.assertFalse(result['raw_prefix_matches_active'])
        self.assertEqual(result['prefix_changed_fields'],['content'])
        self.assertNotIn('private',str(result))

    def test_compaction_retains_historical_entries_outside_active_context(self):
        result=transcript_structure({'in_context_message_ids':['summary']},[
            {'type':'message','message':{'id':'old','content':'old private'}},
            {'type':'compaction','message':{'id':'summary','content':'private summary'}}])
        self.assertEqual(result['compaction_entries'],1)
        self.assertEqual(result['active_messages'],1)
        self.assertFalse(result['raw_prefix_matches_active'])

    def test_missing_active_ids_are_visible(self):
        self.assertEqual(transcript_structure({'in_context_message_ids':['missing']},[])['missing_active_ids'],1)

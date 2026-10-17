"""Offline delivery confirmation and crash recovery without Telegram resends."""
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from andrewbot.controller import Controller
from andrewbot.diary import deliver_pending_diary
from andrewbot.dispatch import ControllerDispatch
from andrewbot.sessions import SessionRunner
from andrewbot.store import Store
from andrewbot.telegram import OwnerRateLimit, TelegramAPI, deliver_saved_message


class TelegramReceiptTests(unittest.TestCase):
    def setUp(self):
        directory=self.enterContext(tempfile.TemporaryDirectory())
        self.path=Path(directory)/'state.db'
        self.store=Store(self.path)
        self.addCleanup(lambda:self.store.close())
        self.calls=[]
        self.timeout=False
        def network(method,payload):
            self.calls.append(payload['text'])
            if self.timeout: raise TimeoutError('fixture confirmation lost')
            return {'message_id':len(self.calls),'chat':{'id':payload['chat_id']},'text':payload['text']}
        self.api=TelegramAPI('fixture-token')
        self.api.call=network
        self.controller=Controller(self.store,Mock(),17)
        self.dispatch=ControllerDispatch(self.store,self.controller,self.api,Mock(),OwnerRateLimit(),17,False,False)

    def reopen(self):
        self.store.close()
        self.store=Store(self.path)
        self.controller.store=self.store
        self.dispatch.store=self.store

    def fail_progress(self,text='Fixture reply.'):
        self.store.queue_reply(17,text)
        with patch.object(self.store,'acknowledge_telegram_chunk',side_effect=OSError('fixture disk failure')):
            with self.assertRaises(OSError):self.dispatch.deliver_reply()
        self.assertEqual(self.store.next_reply()['delivery_state'],'held')

    def test_confirmed_receipt_recovers_after_progress_failure_without_a_second_send(self):
        self.fail_progress()
        self.reopen()
        self.assertEqual(self.store.reconcile_telegram_receipts(17,startup=True),1)
        self.assertIsNone(self.store.next_reply())
        self.assertFalse(self.dispatch.deliver_reply())
        self.assertEqual(self.calls,['Fixture reply.'])
        row=dict(self.store.connection.execute('SELECT * FROM telegram_chunk_receipts').fetchone())
        self.assertEqual(row['message_id'],1)
        self.assertEqual(row['wire_sha256'],hashlib.sha256(b'Fixture reply.').hexdigest())
        self.assertNotIn('Fixture reply.',json.dumps(row))
        self.assertNotIn('fixture-token',json.dumps(row))

    def test_runtime_worker_reconciles_held_confirmations_without_a_gmail_connection(self):
        self.fail_progress()
        self.controller.reconcile_actions()
        self.assertIsNone(self.store.next_reply())
        self.assertEqual(len(self.calls),1)

    def test_only_definitively_unsent_suffix_resumes_after_confirmed_part(self):
        self.fail_progress('x'*5000)
        self.assertEqual(self.calls,['x'*3800])
        self.store.reconcile_telegram_receipts(17,startup=True)
        self.assertEqual(self.store.next_reply()['delivery_state'],'pending')
        self.assertTrue(self.dispatch.deliver_reply())
        self.assertEqual(self.calls,['x'*3800,'x'*1200])
        self.assertIsNone(self.store.next_reply())

    def test_timeout_without_a_receipt_remains_held_after_restart(self):
        self.store.queue_reply(17,'Already received, response lost.')
        self.timeout=True
        with self.assertRaises(TimeoutError):self.dispatch.deliver_reply()
        self.reopen()
        self.assertEqual(self.store.reconcile_telegram_receipts(17,startup=True),0)
        self.store.recover_reply_claims()
        self.assertFalse(self.dispatch.deliver_reply())
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.store.next_reply()['delivery_state'],'held')
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM telegram_chunk_receipts').fetchone()[0],0)

    def test_confirmed_prefix_does_not_settle_a_later_unknown_part(self):
        original=self.api.call
        def network(method,payload):
            self.timeout=len(self.calls)==1
            return original(method,payload)
        self.api.call=network
        self.store.queue_reply(17,'x'*5000)
        with self.assertRaises(TimeoutError):self.dispatch.deliver_reply()
        self.reopen()
        self.store.reconcile_telegram_receipts(17,startup=True)
        self.store.recover_reply_claims()
        self.assertFalse(self.dispatch.deliver_reply())
        self.assertEqual(len(self.calls),2)
        self.assertEqual(self.store.next_reply()['delivery_state'],'held')

    def test_reused_outbox_id_cannot_borrow_an_older_delivery_receipt(self):
        self.store.queue_reply(17,'Same text.')
        first=self.store.next_reply()['id']
        self.dispatch.deliver_reply()
        self.store.queue_reply(17,'Same text.')
        self.assertEqual(self.store.next_reply()['id'],first)
        self.timeout=True
        with self.assertRaises(TimeoutError):self.dispatch.deliver_reply()
        self.store.reconcile_telegram_receipts(17,startup=True)
        self.assertEqual(self.store.next_reply()['delivery_state'],'held')
        self.assertEqual(len(self.calls),2)

    def test_changed_chat_or_content_cannot_use_an_existing_receipt(self):
        self.fail_progress()
        with self.store.connection:
            self.store.connection.execute('UPDATE telegram_outbox SET chat_id=18')
        self.assertEqual(self.store.reconcile_telegram_receipts(17,startup=True),0)
        with self.store.connection:
            self.store.connection.execute('UPDATE telegram_outbox SET chat_id=17')
            self.store.connection.execute("UPDATE telegram_delivery_progress SET content='Changed body'")
        self.assertEqual(self.store.reconcile_telegram_receipts(17,startup=True),0)
        self.assertEqual(self.store.next_reply()['delivery_state'],'held')

    def test_stale_acknowledgement_cannot_confirm_a_new_attempt(self):
        self.store.prepare_telegram_delivery('reply',7,'Fixture')
        self.store.attempt_telegram_chunk('reply',7,0)
        context=self.store.telegram_chunk_context('reply',7,0)
        with self.store.connection:
            self.store.connection.execute("UPDATE telegram_delivery_progress SET chunk_attempts=0,chunk_token=''")
        self.store.attempt_telegram_chunk('reply',7,0)
        with self.assertRaises(ValueError):
            self.store.save_telegram_receipt(context,17,'Fixture',{'message_id':1,'chat':{'id':17},'text':'Fixture'})
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM telegram_chunk_receipts').fetchone()[0],0)

    def test_invalid_telegram_success_envelope_never_confirms_delivery(self):
        for result in (None,{}, {'message_id':True,'chat':{'id':17},'text':'Fixture'},
                {'message_id':1,'chat':{'id':18},'text':'Fixture'},
                {'message_id':1,'chat':{'id':17},'text':'Different'}):
            api=TelegramAPI('fixture-secret',opener=lambda *a,**k:io.BytesIO(json.dumps({'ok':True,'result':result}).encode()))
            with self.assertRaises(RuntimeError) as caught:api.send_message(17,'Fixture')
            self.assertNotIn('fixture-secret',str(caught.exception))
        api=TelegramAPI('fixture-secret',opener=lambda *a,**k:io.BytesIO(json.dumps({'ok':'true','result':{'message_id':1,'chat':{'id':17},'text':'Fixture'}}).encode()))
        with self.assertRaises(RuntimeError):api.send_message(17,'Fixture')

    def test_receipt_storage_failure_keeps_the_unacknowledged_send_held(self):
        self.store.queue_reply(17,'Fixture')
        with patch.object(self.store,'save_telegram_receipt',side_effect=OSError('disk fixture')):
            with self.assertRaises(OSError):self.dispatch.deliver_reply()
        self.store.reconcile_telegram_receipts(17,startup=True)
        self.assertEqual(self.store.next_reply()['delivery_state'],'held')
        self.assertEqual(len(self.calls),1)

    def test_diary_receipt_finishes_after_restart_without_resending(self):
        entry=self.store.create_diary_entry('2026-10-17','A saved diary entry.')
        with patch.object(self.store,'acknowledge_telegram_chunk',side_effect=OSError('disk fixture')):
            self.assertEqual(deliver_pending_diary(self.api,self.store,17),0)
        self.reopen()
        self.assertEqual(self.store.reconcile_telegram_receipts(17,startup=True),1)
        self.assertEqual(self.store.diary_delivery_state(entry),'delivered')
        self.assertEqual(deliver_pending_diary(self.api,self.store,17),0)
        self.assertEqual(len(self.calls),1)

    def test_existing_errors_sleep_and_pause_are_preserved(self):
        self.fail_progress()
        self.store.set_state('autonomy_error','provider_attempt_uncertain')
        self.store.set_state('sleeping','1')
        self.store.set_state('paused','1')
        self.controller.reconcile_actions()
        self.assertIsNotNone(self.store.next_reply())
        self.store.reconcile_telegram_receipts(17,startup=True)
        self.assertEqual(self.store.get_state('autonomy_error'),'provider_attempt_uncertain')
        self.assertEqual(self.store.get_state('sleeping'),'1')
        self.assertEqual(self.store.get_state('paused'),'1')

    def test_active_sender_keeps_its_claim_until_it_stops(self):
        self.store.queue_reply(17,'Fixture')
        row=self.store.claim_reply('normal')
        self.store.prepare_telegram_delivery('reply',row['id'],row['text'])
        self.store.attempt_telegram_chunk('reply',row['id'],0)
        context=self.store.telegram_chunk_context('reply',row['id'],0)
        self.store.save_telegram_receipt(context,17,row['text'],{'message_id':1,'chat':{'id':17},'text':row['text']})
        self.assertEqual(self.store.reconcile_telegram_receipts(17),0)
        self.assertEqual(self.store.next_reply()['claim_token'],row['claim_token'])
        self.assertEqual(self.store.next_reply()['delivery_state'],'sending')

    def test_explicit_diary_retry_consumes_known_confirmation_without_resending(self):
        entry=self.store.create_diary_entry('2026-10-17','A saved diary entry.')
        with patch.object(self.store,'acknowledge_telegram_chunk',side_effect=OSError('disk fixture')):
            self.assertEqual(deliver_pending_diary(self.api,self.store,17),0)
        self.assertTrue(self.store.retry_uncertain_diary_delivery(entry))
        self.assertEqual(deliver_pending_diary(self.api,self.store,17),1)
        self.assertEqual(self.store.diary_delivery_state(entry),'delivered')
        self.assertEqual(len(self.calls),1)

    def test_receipt_survives_abrupt_exit_before_progress_is_advanced(self):
        path=self.path.parent/'crash.db'
        script="""
import os,sys
from andrewbot.store import Store
from andrewbot.telegram import deliver_saved_message
s=Store(sys.argv[1]);s.queue_reply(17,'Crash fixture')
r=s.claim_reply('normal')
class Telegram:
 def send_message(self,chat,text):return {'message_id':123,'chat':{'id':chat},'text':text}
s.acknowledge_telegram_chunk=lambda *args:os._exit(0)
deliver_saved_message(Telegram(),s,'reply',r['id'],17,r['text'])
"""
        subprocess.run([sys.executable,'-c',script,str(path)],env=os.environ,check=True)
        other=Store(path)
        try:
            self.assertEqual(other.next_reply()['delivery_state'],'sending')
            self.assertEqual(other.reconcile_telegram_receipts(17,startup=True),1)
            self.assertIsNone(other.next_reply())
            self.assertEqual(other.connection.execute('SELECT message_id FROM telegram_chunk_receipts').fetchone()[0],123)
        finally:other.close()

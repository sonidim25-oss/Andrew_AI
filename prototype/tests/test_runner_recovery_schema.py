import sqlite3
import unittest

from andrewbot.store import Store
from runner_recovery_fixture import RunnerRecoveryFixture


class RunnerRecoverySchemaTests(RunnerRecoveryFixture, unittest.TestCase):
    def make_old_schema(self):
        connection = self.store.connection
        connection.commit(); connection.execute('PRAGMA foreign_keys=OFF')
        connection.executescript('''BEGIN IMMEDIATE;
            CREATE TABLE old_cycle_tools(attempt_id TEXT PRIMARY KEY,
                cycle_id TEXT NOT NULL REFERENCES cycles(cycle_id),tool_name TEXT NOT NULL,
                outcome TEXT NOT NULL CHECK(outcome IN ('started','returned','uncertain')),
                started_at TEXT NOT NULL,finished_at TEXT);
            INSERT INTO old_cycle_tools SELECT * FROM cycle_tool_attempts;
            DROP TABLE cycle_tool_attempts;
            ALTER TABLE old_cycle_tools RENAME TO cycle_tool_attempts;
            CREATE INDEX cycle_tool_attempts_cycle ON cycle_tool_attempts(cycle_id,outcome);
            COMMIT;''')
        connection.execute('PRAGMA foreign_keys=ON')

    def test_older_schema_upgrade_preserves_unknowns_and_external_receipt_foreign_keys(self):
        attempts = [self.store.begin_cycle_tool(self.store.research_tool_context(), 'python.run') for _ in range(3)]
        with self.store.connection:
            self.store.connection.execute('''INSERT INTO external_action_receipts(
                action_id,adapter,operation,request_id,digest,principal,lookup,state,attempt_id,session_id,cycle_id,created_at)
                VALUES(?,?,?,?,?,?,?,'uncertain',?,?,?,?)''', (
                'action','gmail','draft','request','0'*64,'fixture','{}',attempts[0],
                self.binding['session_id'],self.binding['cycle_id'],self.store.now()))
        self.make_old_schema()
        self.store.close(); self.store = Store(self.path)
        self.assertEqual(self.store.connection.execute('PRAGMA foreign_keys').fetchone()[0], 1)
        self.assertIsNone(self.store.connection.execute('PRAGMA foreign_key_check').fetchone())
        references = list(self.store.connection.execute('PRAGMA foreign_key_list(external_action_receipts)'))
        self.assertTrue(any(row['table'] == 'cycle_tool_attempts' for row in references))
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM cycle_tool_attempts').fetchone()[0], 3)
        self.assertEqual(self.store.connection.execute('SELECT attempt_id FROM external_action_receipts').fetchone()[0], attempts[0])
        with self.store.connection:
            self.store.connection.execute("UPDATE cycle_tool_attempts SET outcome='not_executed' WHERE attempt_id=?", (attempts[1],))
        self.assertTrue(self.store.cycle_uncertain_effects(self.binding['cycle_id']))

    def test_failed_upgrade_rolls_back_table_replacement_and_preserves_records(self):
        attempt = self.store.begin_cycle_tool(self.store.research_tool_context(), 'python.run')
        self.make_old_schema()
        self.store.connection.execute('PRAGMA foreign_keys=OFF')
        with self.store.connection:
            self.store.connection.execute('''INSERT INTO external_action_receipts(
                action_id,adapter,operation,request_id,digest,principal,lookup,state,attempt_id,session_id,cycle_id,created_at)
                VALUES(?,?,?,?,?,?,?,'uncertain',?,?,?,?)''', (
                'action','gmail','draft','request','0'*64,'fixture','{}','missing-attempt',
                self.binding['session_id'],self.binding['cycle_id'],self.store.now()))
        self.store.close()
        with self.assertRaises(Exception):
            Store(self.path)
        # Open a plain connection to inspect the rejected upgrade without trying
        # another startup migration. The original table/data must still exist.
        self.store = type('PlainStore', (), {})()
        self.store.connection = sqlite3.connect(self.path)
        self.store.close = self.store.connection.close
        sql = self.store.connection.execute("SELECT sql FROM sqlite_master WHERE name='cycle_tool_attempts'").fetchone()[0]
        self.assertNotIn('not_executed', sql)
        self.assertEqual(self.store.connection.execute('SELECT attempt_id FROM cycle_tool_attempts').fetchone()[0], attempt)

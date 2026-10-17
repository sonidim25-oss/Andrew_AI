import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from andrewbot.skills import SkillCatalog, SKILL_DOCUMENT_HASHES, SKILL_INDEX_PROMPT


class SkillCatalogTests(unittest.TestCase):
    def test_compact_index_is_metadata_only_and_does_not_read_documents(self):
        catalog = SkillCatalog()
        with patch.object(Path, 'open', side_effect=AssertionError('index must not load full skill text')):
            index = catalog.execute('skills.list', {})
        self.assertEqual(['voice-communication', 'image-creation', 'research-workflow'], [row['name'] for row in index['skills']])
        self.assertLessEqual(len(SKILL_INDEX_PROMPT.encode()), 1200)
        self.assertNotIn('## Expected result', SKILL_INDEX_PROMPT)
        for row in index['skills']:
            self.assertIn(row['name'], SKILL_INDEX_PROMPT)
            self.assertNotIn('instructions', row)

    def test_loads_exact_versioned_markdown_by_name(self):
        catalog = SkillCatalog()
        root = Path(__file__).resolve().parents[2] / 'skills'
        for name, digest in SKILL_DOCUMENT_HASHES.items():
            with self.subTest(name=name):
                result = catalog.execute('skills.load', {'name': name})
                data = (root / name / 'SKILL.md').read_bytes()
                self.assertEqual(data.decode(), result['instructions'])
                self.assertEqual(hashlib.sha256(data).hexdigest(), digest)
                self.assertEqual(digest, result['sha256'])
                self.assertFalse(result['grants_permissions'])

    def test_rejects_unknown_names_paths_and_extra_arguments(self):
        for arguments in ({'name': '../vault'}, {'name': '/etc/passwd'}, {'name': 'voice-communication/SKILL.md'},
                          {'name': 'system-self-check'}, {'name': []}, {'name': 'voice-communication', 'path': '/etc/passwd'}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                SkillCatalog().execute('skills.load', arguments)
        with self.assertRaises(ValueError):
            SkillCatalog().execute('skills.list', {'name': 'voice-communication'})

    def test_rejects_modified_oversized_and_symlinked_documents(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            topic = root / 'voice-communication'
            topic.mkdir()
            document = topic / 'SKILL.md'
            catalog = SkillCatalog(root)
            document.write_text('private replacement')
            with self.assertRaises(ValueError) as error:
                catalog.execute('skills.load', {'name': 'voice-communication'})
            self.assertNotIn('private replacement', str(error.exception))
            document.write_text('x' * 13000)
            with self.assertRaises(ValueError):
                catalog.execute('skills.load', {'name': 'voice-communication'})
            document.unlink()
            document.symlink_to(Path(__file__).resolve().parents[2] / 'skills/voice-communication/SKILL.md')
            with self.assertRaises(ValueError):
                catalog.execute('skills.load', {'name': 'voice-communication'})

    def test_missing_documents_and_symlinked_roots_fail_without_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(ValueError):
                SkillCatalog(root).execute('skills.load', {'name': 'research-workflow'})
            link = root / 'linked-skills'
            link.symlink_to(Path(__file__).resolve().parents[2] / 'skills', target_is_directory=True)
            with self.assertRaises(ValueError):
                SkillCatalog(link).execute('skills.load', {'name': 'research-workflow'})

    def test_loading_does_not_grant_media_permissions_or_modify_memory(self):
        from andrewbot.controller import Controller
        from andrewbot.media import MediaService
        from andrewbot.store import Store
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'controller.sqlite3')
            self.addCleanup(store.close)
            media = MediaService(store, Path(directory) / 'artifacts', 42)
            controller = Controller(store, Mock(), 42, media=media)
            for key, value, cleared in (('paused', '1', '0'), ('sleeping', '1', '0'), ('autonomy_error', 'timeout', '')):
                store.set_state(key, value)
                for name in SKILL_DOCUMENT_HASHES:
                    result = json.loads(controller.execute_tool('skills.load', {'name': name}))
                    self.assertIn('instructions', result)
                self.assertEqual(value, store.get_state(key))
                store.set_state(key, cleared)
            self.assertFalse(media.policy.enabled)
            for table in ('memory_records', 'personality_revisions', 'messages', 'usage', 'media_jobs'):
                self.assertEqual(0, store.connection.execute('SELECT count(*) FROM ' + table).fetchone()[0])
            with self.assertRaises(ValueError):
                controller.execute_tool('voice.generate', {'text': 'not authorized', 'owner_request_id': 'invented'})
            controller.model.reply.assert_not_called()

    def test_only_index_is_added_to_standing_prompt_and_tools_are_read_only(self):
        from andrewbot.appserver import AppServerClient, LettaAgent
        client = object.__new__(AppServerClient)
        self.assertIn('skills.list', client.tool_names)
        self.assertIn('skills.load', client.tool_names)
        self.assertNotIn('skills.write', client.tool_names)
        self.assertNotIn('voice.generate', client.tool_names)
        self.assertIn(SKILL_INDEX_PROMPT, LettaAgent.system_prompt)
        self.assertNotIn('## Persistence and interruption', LettaAgent.system_prompt)

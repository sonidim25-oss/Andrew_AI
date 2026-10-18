import importlib.util
import io
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import Mock, patch
import zlib


class MediaSampleTests(unittest.TestCase):
    def test_repeated_sample_never_regenerates_or_replays_uncertain_delivery(self):
        path = Path(__file__).resolve().parents[1] / 'media-live-sample.py'
        spec = importlib.util.spec_from_file_location('media_sample_test', path)
        sample = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sample)
        def chunk(kind, data):
            return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
        png = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(b'\0\xff\0\0')) + chunk(b'IEND', b'')
        provider = Mock()
        provider.generate.side_effect = [(b'ID3' + bytes(40), 'audio/mpeg', .0001), (png, 'image/png', .001)]
        telegram = Mock()
        telegram.send_media.side_effect = [
            {'message_id': 1, 'chat': {'id': 42},
             'voice': {'file_id': 'sample-file', 'file_unique_id': 'sample-unique'}},
            TimeoutError('uncertain')]
        def catalog(url, timeout):
            return io.BytesIO(json.dumps({'data': [
                {'id': 'elevenlabs/eleven-v3', 'supported_voices': ['george']},
                {'id': 'openai/gpt-image-1-mini'}]}).encode())
        with tempfile.TemporaryDirectory() as directory:
            environment = {'ANDREW_DATA_DIR': directory, 'ANDREW_VAULT_DIR': directory + '/vault', 'ANDREW_OWNER_ID': '42'}
            with patch.dict(os.environ, environment), patch('sys.argv', ['media-live-sample.py', '--approved-sample']), \
                    patch.object(sample, 'urlopen', side_effect=catalog), patch.object(sample, 'Vault') as vault, \
                    patch.object(sample, 'TelegramAPI', return_value=telegram), \
                    patch.object(sample, 'OpenRouterMedia', return_value=provider), patch('builtins.print'):
                vault.return_value.get_secret.return_value = 'fixture'
                self.assertEqual(2, sample.main())
                self.assertEqual(2, sample.main())
            self.assertEqual(2, provider.generate.call_count)
            self.assertEqual(2, telegram.send_media.call_count)
            report = json.loads((Path(directory) / 'media-sample-20261017.json').read_text())
            self.assertEqual(['delivered', 'uncertain'], [row['delivery'] for row in report['jobs']])

import importlib.util
from pathlib import Path
import unittest

class ImportedImageTests(unittest.TestCase):
    def load_helper(self):
        p=Path(__file__).resolve().parents[1]/'verify-imported-image.py'
        spec=importlib.util.spec_from_file_location('imported_image',p)
        module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        return module
    def test_imported_digest_may_change_only_when_content_identity_matches(self):
        module=self.load_helper()
        release={'image':'localhost/andrew-browser@sha256:'+'a'*64,'image_config_id':'sha256:'+'b'*64}
        imported={'Id':'b'*64,'Digest':'sha256:'+'c'*64}
        self.assertEqual(module.imported_reference(release,[imported]),'localhost/andrew-browser@sha256:'+'c'*64)
        with self.assertRaises(ValueError): module.imported_reference(release,[{**imported,'Id':'d'*64}])
    def test_rejects_missing_or_unpinned_import_identity(self):
        module=self.load_helper()
        release={'image':'localhost/andrew-browser@sha256:'+'a'*64,'image_config_id':'sha256:'+'b'*64}
        for value in ([],[{'Id':'b'*64,'Digest':''}],[{'Id':'b'*64,'Digest':'latest'}], [{'Id':'b'*64,'Digest':'sha256:'+'c'*64}]*2):
            with self.subTest(value=value), self.assertRaises(ValueError): module.imported_reference(release,value)

import importlib.util
from pathlib import Path
import sys,tempfile,unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'prototype'))
from andrewbot.store import Store
class RuntimeStateTests(unittest.TestCase):
    def test_snapshot_detects_spending_and_owner_state_changes(self):
        path=Path(__file__).resolve().parents[1]/'verify-runtime-state.py'
        spec=importlib.util.spec_from_file_location('runtime_state',path); module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as root:
            db=Path(root)/'state.sqlite3'; store=Store(db)
            store.set_state('paused','1'); before=module.snapshot(db)
            self.assertEqual(before,module.snapshot(db))
            store.record_usage('2026-10','2026-10-14',.001,10,5)
            self.assertNotEqual(before,module.snapshot(db)); before=module.snapshot(db)
            store.set_state('paused','0'); self.assertNotEqual(before,module.snapshot(db)); before=module.snapshot(db)
            store.begin_cycle('session','cycle','2026-10-14'); self.assertNotEqual(before,module.snapshot(db)); store.close()

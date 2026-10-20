"""Keep temporary backup databases independent of the host's adoption record."""
from pathlib import Path
import tempfile
from unittest.mock import patch


def isolate_backup_adoption(testcase):
    directory = tempfile.TemporaryDirectory()
    testcase.addCleanup(directory.cleanup)
    adoption = Path(directory.name) / 'runner-receipt-adoption.json'
    def local_path(value, *args, **kwargs):
        if str(value) == '/etc/andrew/runner-receipt-adoption.json':
            return adoption
        return Path(value, *args, **kwargs)
    testcase.enterContext(patch('andrewbot.memory_backup.Path', local_path))
    return adoption

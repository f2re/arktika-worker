"""Safety checks for bounded cleanup of abandoned reanalysis checkpoints."""
import hashlib
import tempfile
import time
import unittest
from pathlib import Path

from arktika.reanalysis.worker import _field_lock, _prune_stale_checkpoints


class CheckpointCleanupTests(unittest.TestCase):
    def test_prunes_only_stale_inactive_checkpoint_directories(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache=Path(temporary);root=cache/'.incomplete';root.mkdir()
            old_time=time.time()-31*24*60*60
            inactive_id=hashlib.sha256(b'inactive').hexdigest()
            active_id=hashlib.sha256(b'active').hexdigest()
            inactive=root/(inactive_id+'-'+'a'*64);inactive.mkdir()
            active=root/(active_id+'-'+'b'*64);active.mkdir()
            fresh=root/(hashlib.sha256(b'fresh').hexdigest()+'-'+'c'*64);fresh.mkdir()
            for path in (inactive,active):
                import os
                os.utime(path,(old_time,old_time))

            with _field_lock(cache,active_id):
                _prune_stale_checkpoints(cache)
                self.assertTrue(active.exists())
            _prune_stale_checkpoints(cache)
            self.assertFalse(inactive.exists())
            self.assertFalse(active.exists())
            self.assertTrue(fresh.exists())


if __name__=='__main__':
    unittest.main()

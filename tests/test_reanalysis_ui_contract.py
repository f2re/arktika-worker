"""Ensure paused remote requests remain manageable without a satellite scene."""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ReanalysisUiContractTests(unittest.TestCase):
    def test_credential_pause_can_be_cancelled_from_the_request_dialog(self):
        source = (ROOT / "static/reanalysis.js").read_text(encoding="utf-8")
        self.assertIn('id="fieldDialogCancel"', source)
        self.assertIn("if($('#fieldDialogCancel'))$('#fieldDialogCancel').hidden=!(busy||j.status==='needs_action'", source)
        self.assertIn("$('#fieldDialogCancel').onclick=cancelFieldJob", source)
        self.assertIn("api('/api/reanalysis/cancel',{id:FIELDS.job.id})", source)


if __name__ == "__main__":
    unittest.main()

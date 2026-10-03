"""Public CLI admission gate; no Feishu or image provider is contacted here."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from factory import cli
from factory.util import FactoryError


ROOT = Path(__file__).resolve().parents[1]


class IntakeCLITests(unittest.TestCase):
    def test_intake_once_without_local_grant_fails_before_remote_access(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.json"
            config.write_text(json.dumps({"base_token": "DEMO_ONLY", "tables": {"tasks": "tblTEST"}}))
            result = subprocess.run(
                [sys.executable, "-m", "factory", "--config", str(config),
                 "--state", str(root / "state"), "intake-once"],
                cwd=ROOT, capture_output=True, text=True, timeout=10,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("受信授权", result.stderr + result.stdout)
            self.assertFalse((root / "state").exists())

    def test_intake_loop_one_cycle_stays_idle_without_remote_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.json"
            config.write_text(json.dumps({
                "base_token": "DEMO_ONLY",
                "tables": {"tasks": "tblTEST"},
                "intake_profile": {"allowed_product_ids": ["recTEST"]},
                "intake_grant": {"approved": True, "authorization_id": "AUTH-TEST",
                                 "scope": {"product_record_id": "recTEST"}},
            }))
            args = cli.parser().parse_args([
                "--config", str(config), "--state", str(root / "state"),
                "intake-loop", "--interval", "0.01", "--max-cycles", "1",
            ])
            with patch("factory.cli.FeishuGateway") as gateway, \
                 patch("factory.cli.Engine") as engine, \
                 patch("factory.form_intake_service.poll_form_once", return_value={"status": "idle"}) as poll:
                result = cli.execute(args)
            self.assertEqual(result, {"status": "idle", "cycles": 1})
            gateway.assert_called_once()
            engine.assert_called_once()
            poll.assert_called_once()

    def test_intake_loop_stops_before_next_scan_if_local_grant_is_removed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.json"
            original = {
                "base_token": "DEMO_ONLY", "tables": {"tasks": "tblTEST"},
                "intake_profile": {"allowed_product_ids": ["recTEST"]},
                "intake_grant": {"approved": True, "authorization_id": "AUTH-TEST",
                                 "scope": {"product_record_id": "recTEST"}},
            }
            config.write_text(json.dumps(original))
            args = cli.parser().parse_args([
                "--config", str(config), "--state", str(root / "state"),
                "intake-loop", "--interval", "0.01", "--max-cycles", "2",
            ])

            def remove_grant(*_args):
                changed = dict(original)
                changed.pop("intake_grant")
                config.write_text(json.dumps(changed))
                return {"status": "idle"}

            with patch("factory.cli.FeishuGateway"), \
                 patch("factory.cli.Engine"), \
                 patch("factory.cli.time.sleep"), \
                 patch("factory.form_intake_service.poll_form_once", side_effect=remove_grant) as poll:
                with self.assertRaises(FactoryError):
                    cli.execute(args)
            poll.assert_called_once()

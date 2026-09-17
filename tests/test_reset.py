import json
import subprocess
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

import test_eeprom_v3 as fixtures
from napi_config.tui.app import UI


class ResetTests(unittest.TestCase):
    def setUp(self):
        fixtures.EepromV3Tests.setUp(self)

    def store(self, cfg):
        return fixtures.EepromV3Tests.store(self, cfg)

    def test_reset_and_new_configuration(self):
        for cfg in (self.cfg, replace(self.cfg, format_version=2, proc_id_type=0, proc_id=b'')):
            before = self.store(cfg)
            plan = self.service.reset_eeprom_plan()
            with self.assertRaisesRegex(ValueError, 'confirmation'):
                self.service.apply_reset_eeprom_plan(plan)
            self.assertEqual(self.path.read_bytes(), before)
            self.service.apply_reset_eeprom_plan(plan, confirmed=True)
            self.assertEqual(self.path.read_bytes(), bytes(256))
            self.assertEqual(Path(self.service.last_eeprom_backup).read_bytes(), before)
            self.service.write_eeprom(self.service.defaults(), confirmed=True)
            self.assertEqual(self.service.read_eeprom().format_version, 3)


    def test_reset_invalid_stale_tampered_backup_and_readback(self):
        self.path.write_bytes(b'\xff' * 256)
        plan = self.service.reset_eeprom_plan()
        with self.assertRaisesRegex(ValueError, 'Invalid EEPROM reset'):
            self.service.apply_reset_eeprom_plan(dict(plan, after=b'X' * 256), confirmed=True)
        self.path.write_bytes(b'X' * 256)
        with self.assertRaisesRegex(ValueError, 'changed since reset'):
            self.service.apply_reset_eeprom_plan(plan, confirmed=True)
        self.path.write_bytes(plan['before_image'])
        with patch.object(self.service.configurations, 'backup_eeprom', side_effect=OSError('backup failed')):
            with self.assertRaisesRegex(OSError, 'backup failed'):
                self.service.apply_reset_eeprom_plan(plan, confirmed=True)
        self.assertEqual(self.path.read_bytes(), plan['before_image'])
        with patch.object(self.service.eeprom, 'write'):
            with self.assertRaisesRegex(OSError, 'read-back mismatch'):
                self.service.apply_reset_eeprom_plan(plan, confirmed=True)
        self.service.apply_reset_eeprom_plan(plan, confirmed=True)
        with patch.object(self.service.eeprom, 'write') as write:
            self.assertFalse(self.service.apply_reset_eeprom_plan(self.service.reset_eeprom_plan(), confirmed=True))
            write.assert_not_called()
        self.path.write_bytes(bytes(126))
        with self.assertRaisesRegex(ValueError, '256-byte'):
            self.service.reset_eeprom_plan()


    def test_combined_processor_action(self):
        self.store(replace(self.cfg, proc_id=bytes.fromhex('090b131d04')))
        plan = self.service.processor_write_plan()
        self.assertIn('PROCESSOR MISMATCH', plan['comparison']['note'])
        self.service.apply_instance_eeprom_plan(plan, confirmed=True)
        self.assertEqual(self.service.read_eeprom(), self.cfg)
        self.store(replace(self.cfg, format_version=2, proc_id_type=0, proc_id=b''))
        before = self.path.read_bytes()
        plan = self.service.processor_write_plan()
        self.assertFalse(plan['writable'])
        self.assertEqual(plan['comparison']['rows'][0]['proposed'], self.otp.hex())
        self.assertEqual(self.path.read_bytes(), before)


    def test_tui_cancel_and_confirm_reset(self):
        before = self.store(self.cfg)
        ui = UI(Mock(), self.service)
        ui.view_comparison = Mock(return_value=False)
        ui.confirm_yes = Mock(return_value=False)
        ui.reset_eeprom()
        ui.confirm_yes.assert_not_called()
        ui.view_comparison.return_value = True
        ui.reset_eeprom()
        self.assertEqual(self.path.read_bytes(), before)
        ui.confirm_yes.return_value = True
        ui.reset_eeprom()
        self.assertEqual(self.path.read_bytes(), bytes(256))
        self.assertEqual(ui.cfg, self.service.defaults())
        self.assertIn('EEPROM empty', ui.status)


    def test_cli_reset_requires_confirmation(self):
        before = self.store(self.cfg)
        common = ['--eeprom', str(self.path), '--db', str(self.root / 'boards.yaml'), '--json']
        def cli(*args):
            return subprocess.run([sys.executable, '-B', '-m', 'napi_config', 'eeprom', 'reset',
                                   *common, *args], capture_output=True, text=True)
        preview = cli('--preview')
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.assertEqual(json.loads(preview.stdout)['preview']['title'], 'RESET EEPROM')
        self.assertNotEqual(cli().returncode, 0)
        self.assertEqual(self.path.read_bytes(), before)
        done = cli('--yes')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.path.read_bytes(), bytes(256))



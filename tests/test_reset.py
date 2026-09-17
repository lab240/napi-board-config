import json
import subprocess
import sys
import unittest
import curses
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

    def test_reset_processor_preserves_raw_fields_and_reserved_area(self):
        before = self.store(self.cfg)
        plan = self.service.reset_processor_plan()
        with self.assertRaisesRegex(ValueError, 'confirmation'):
            self.service.apply_reset_processor_plan(plan)
        self.assertEqual(self.path.read_bytes(), before)
        self.service.apply_reset_processor_plan(plan, confirmed=True)
        after = self.path.read_bytes()
        self.assertEqual(after[:104], before[:104])
        self.assertEqual(after[104:122], bytes(18))
        self.assertEqual(after[126:], before[126:])
        self.assertEqual(self.service.read_eeprom(), replace(self.cfg, proc_id_type=0, proc_id=b''))
        self.assertEqual(Path(self.service.last_eeprom_backup).read_bytes(), before)
        self.assertFalse(self.service.apply_reset_processor_plan(self.service.reset_processor_plan(), confirmed=True))
        self.otp_path.write_bytes(bytes(20) + bytes.fromhex('090b131d04'))
        candidate = self.service.eeprom_write_configuration(self.service.read_eeprom())
        self.assertEqual(candidate.proc_id.hex(), '090b131d04')
        self.service.write_eeprom(candidate, confirmed=True)
        self.assertEqual(self.service.read_eeprom(), replace(self.cfg, proc_id=bytes.fromhex('090b131d04')))

    def test_reset_processor_rejects_stale_tampered_and_failed_backup(self):
        before = self.store(self.cfg)
        plan = self.service.reset_processor_plan()
        with self.assertRaisesRegex(ValueError, 'Invalid processor reset'):
            self.service.apply_reset_processor_plan(dict(plan, after=bytes(256)), confirmed=True)
        with patch.object(self.service.configurations, 'backup_eeprom', side_effect=OSError('backup failed')):
            with self.assertRaisesRegex(OSError, 'backup failed'):
                self.service.apply_reset_processor_plan(plan, confirmed=True)
        self.assertEqual(self.path.read_bytes(), before)
        self.store(replace(self.cfg, serial_number=77))
        with self.assertRaisesRegex(ValueError, 'changed since processor reset'):
            self.service.apply_reset_processor_plan(plan, confirmed=True)

    def test_missing_otp_prevents_full_write_and_view_mac_cancel_preserves_draft(self):
        self.path.write_bytes(bytes(256))
        self.service.otp = Mock()
        self.service.otp.read_id.side_effect = ValueError('OTP unavailable')
        with self.assertRaisesRegex(ValueError, 'OTP unavailable'):
            self.service.write_eeprom(self.service.defaults(), confirmed=True)
        self.assertEqual(self.path.read_bytes(), bytes(256))
        ui = UI(Mock(), self.service)
        draft = ui.cfg
        ui.view_text = Mock(return_value=False)
        ui.generate_macs = Mock()
        ui.view_generate_macs()
        ui.generate_macs.assert_not_called()
        self.assertEqual(ui.cfg, draft)

    def test_full_preview_contains_otp_and_rejects_changed_soc_or_eeprom(self):
        self.path.write_bytes(bytes(256))
        plan = self.service.eeprom_write_plan(self.service.defaults())
        self.assertEqual(plan['configuration'].proc_id, self.otp)
        self.otp_path.write_bytes(bytes(20) + bytes.fromhex('090b131d04'))
        with self.assertRaisesRegex(ValueError, 'PROCESSOR MISMATCH'):
            self.service.apply_eeprom_write_plan(plan, confirmed=True)
        self.assertEqual(self.path.read_bytes(), bytes(256))
        self.otp_path.write_bytes(bytes(20) + self.otp)
        self.path.write_bytes(b'X' * 256)
        with self.assertRaisesRegex(ValueError, 'changed since full write'):
            self.service.apply_eeprom_write_plan(plan, confirmed=True)

    def test_menu_columns_and_horizontal_navigation(self):
        ui = UI(Mock(), self.service)
        screen = ui.stdscr
        screen.getmaxyx.return_value = (50, 120)
        ui.draw()
        drawn = [call.args for call in screen.addnstr.call_args_list]
        headers = {args[2]: args[:2] for args in drawn if args[2] in ('--- ACTIONS ---', '--- SERVICE ---')}
        self.assertEqual(headers['--- ACTIONS ---'][0], headers['--- SERVICE ---'][0])
        self.assertLess(headers['--- ACTIONS ---'][1], headers['--- SERVICE ---'][1])
        screen.getch.side_effect = [curses.KEY_RIGHT, curses.KEY_LEFT, ord('q')]
        positions = []
        ui.draw = lambda: positions.append(ui.cursor)
        with patch('napi_config.tui.app.curses.curs_set'):
            ui.run()
        self.assertEqual(positions, [0, ui.service_start, 0])

    def test_cli_processor_reset_preview_denied_and_confirmed(self):
        before = self.store(self.cfg)
        command = [sys.executable, '-B', '-m', 'napi_config', 'processor', 'reset',
                   '--eeprom', str(self.path), '--db', str(self.root / 'boards.yaml'), '--json']
        preview = subprocess.run(command + ['--preview'], capture_output=True, text=True)
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.assertEqual(self.path.read_bytes(), before)
        denied = subprocess.run(command, capture_output=True, text=True)
        self.assertNotEqual(denied.returncode, 0)
        self.assertEqual(self.path.read_bytes(), before)
        written = subprocess.run(command + ['--yes'], capture_output=True, text=True)
        self.assertEqual(written.returncode, 0, written.stderr)
        self.assertTrue(json.loads(written.stdout)['verified'])
        self.assertEqual(self.service.read_eeprom(), replace(self.cfg, proc_id_type=0, proc_id=b''))

    def test_view_eeprom_reads_actual_data_without_changing_draft(self):
        before = self.store(self.cfg)
        ui = UI(Mock(), self.service)
        ui.cfg = replace(self.cfg, serial_number=999, board_name='Unsaved')
        draft = ui.cfg
        ui.view_text = Mock()
        ui.view_eeprom()
        text = ui.view_text.call_args.args[0]
        self.assertIn('Serial number: 1234', text)
        self.assertIn('CRC32: valid', text)
        self.assertIn(self.otp.hex(), text)
        self.assertIs(ui.cfg, draft)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(ui.config_text('instance', 'processor_binding'), 'Processor ID : ' + self.otp.hex())
        corrupt = bytearray(before)
        corrupt[16] ^= 1
        self.path.write_bytes(corrupt)
        ui.view_eeprom()
        text = ui.view_text.call_args.args[0]
        self.assertIn('CRC mismatch', text)
        self.assertIn('Raw EEPROM:', text)
        self.assertNotIn('CRC32: valid', text)
        self.assertIs(ui.cfg, draft)
        self.path.write_bytes(bytes(256))
        ui.view_eeprom()
        self.assertIn('EEPROM empty', ui.view_text.call_args.args[0])
        self.assertIs(ui.cfg, draft)

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
        self.assertEqual(ui.cfg, self.cfg)
        self.assertIn(self.otp.hex(), ui.config_text('instance', 'processor_binding'))
        self.assertIn('not in EEPROM', ui.config_text('instance', 'processor_binding'))
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

    def test_processor_initialization_uses_draft_and_preserves_tail(self):
        draft = replace(self.cfg, format_version=2, proc_id_type=0, proc_id=b'', comment='RAM only')
        for empty in (bytes(256), b'\xff' * 256):
            self.path.write_bytes(empty)
            plan = self.service.processor_write_plan(draft)
            self.assertEqual(plan['kind'], 'initialize')
            self.assertIn('FULL', plan['comparison']['note'])
            self.assertEqual(self.path.read_bytes(), empty)
            with self.assertRaisesRegex(ValueError, 'confirmation'):
                self.service.apply_processor_write_plan(plan)
            self.service.apply_processor_write_plan(plan, confirmed=True)
            expected = replace(self.cfg, comment='')
            self.assertEqual(self.service.read_eeprom(), expected)
            self.assertEqual(Path(self.service.last_eeprom_backup).read_bytes(), empty)
            self.assertEqual(self.path.read_bytes()[126:], empty[126:])
            self.assertEqual(draft.format_version, 2)

    def test_processor_initialization_rejects_stale_otp_and_eeprom(self):
        self.path.write_bytes(bytes(256))
        plan = self.service.processor_write_plan(self.cfg)
        self.otp_path.write_bytes(bytes(20) + bytes.fromhex('090b131d04'))
        with self.assertRaisesRegex(ValueError, 'Processor changed'):
            self.service.apply_processor_write_plan(plan, confirmed=True)
        self.assertEqual(self.path.read_bytes(), bytes(256))
        self.otp_path.write_bytes(bytes(20) + self.otp)
        self.store(self.cfg)
        with self.assertRaisesRegex(ValueError, 'changed since processor preview'):
            self.service.apply_processor_write_plan(plan, confirmed=True)
        self.path.write_bytes(bytes(256))
        with patch.object(self.service.configurations, 'backup_eeprom', side_effect=OSError('backup failed')):
            with self.assertRaisesRegex(OSError, 'backup failed'):
                self.service.apply_processor_write_plan(plan, confirmed=True)
        with patch.object(self.service.eeprom, 'write'):
            with self.assertRaisesRegex(OSError, 'read-back mismatch'):
                self.service.apply_processor_write_plan(plan, confirmed=True)

    def test_corrupt_eeprom_is_not_automatically_initialized(self):
        self.path.write_bytes(b'X' * 256)
        plan = self.service.processor_write_plan(self.cfg)
        self.assertFalse(plan['writable'])
        self.assertEqual(self.path.read_bytes(), b'X' * 256)

    def test_plain_write_reinitializes_preserved_v2_draft_as_v3(self):
        draft = replace(self.cfg, format_version=2, proc_id_type=0, proc_id=b'')
        self.path.write_bytes(bytes(256))
        comparison = self.service.eeprom_comparison(draft)
        self.assertEqual(comparison['rows'][0]['proposed'], '3')
        self.service.write_eeprom(draft, confirmed=True)
        self.assertEqual(self.service.read_eeprom(), replace(draft, format_version=3, proc_id_type=1, proc_id=self.otp))

    def test_navigation_wraps_at_menu_boundaries_and_processor_is_in_actions(self):
        for width in (60, 120):
            screen = Mock()
            screen.getmaxyx.return_value = (40, width)
            screen.getch.side_effect = [curses.KEY_UP, curses.KEY_DOWN, ord('q')]
            ui = UI(screen, self.service)
            positions = []
            ui.draw = lambda: positions.append(ui.cursor)
            with patch('napi_config.tui.app.curses.curs_set'):
                ui.run()
            self.assertEqual(positions, [0, len(ui.rows) - 1, 0])
            self.assertNotIn(('action', 'write_processor'), ui.rows)
            self.assertIn(('action', 'reset_processor'), ui.SERVICES)
            self.assertEqual(ui.rows.count(('action', 'view_generate_macs')), 1)
            self.assertNotIn(('action', 'view_boot'), ui.rows)

    def test_tui_initialization_keeps_draft_on_cancel_and_writes_it_on_yes(self):
        self.path.write_bytes(bytes(256))
        ui = UI(Mock(), self.service)
        ui.cfg = replace(self.cfg, proc_id_type=0, proc_id=b'', comment='keep comment')
        draft = ui.cfg
        ui.view_comparison = Mock(return_value=True)
        ui.confirm_yes = Mock(return_value=False)
        ui.change_instance('write')
        self.assertEqual(ui.cfg, draft)
        self.assertEqual(self.path.read_bytes(), bytes(256))
        ui.confirm_yes.return_value = True
        ui.change_instance('write')
        self.assertEqual(ui.cfg, replace(self.cfg, comment='keep comment'))

    def test_cli_initializes_processor_and_configuration_from_json(self):
        self.path.write_bytes(bytes(256))
        config = self.root / 'instance.json'
        config.write_text(json.dumps(self.service.document(self.cfg)))
        command = [sys.executable, '-B', '-m', 'napi_config', 'processor', 'write',
                   '--eeprom', str(self.path), '--db', str(self.root / 'boards.yaml'),
                   '--otp', str(self.otp_path), '--config', str(config), '--json']
        preview = subprocess.run(command + ['--preview'], capture_output=True, text=True)
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.assertTrue(json.loads(preview.stdout)['writable'])
        done = subprocess.run(command + ['--yes'], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.service.read_eeprom(), self.cfg)

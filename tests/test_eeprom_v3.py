import json
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from napi_config.bootstrap import create_service
from napi_config.core.codec import decode_config, encode_config
from napi_config.core.configuration import from_document, to_document
from napi_config.core.mac import generate_macs
from napi_config.core.models import BoardConfig
from napi_config.core.profiles import config_to_profile
from napi_config.tui.app import UI


class EepromV3Tests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / 'eeprom'
        self.path.write_bytes(b'\xff' * 256)
        self.otp_path = self.root / 'otp'
        self.otp = bytes.fromhex('0235221703')
        self.otp_path.write_bytes(bytes(20) + self.otp)
        self.service = create_service(str(self.path), str(self.root / 'boards.yaml'),
                                      otp_path=str(self.otp_path))
        self.cfg = BoardConfig(enabled={'i2c1'}, serial_number=1234,
                               mfg_date='2026-09-15', board_name='NAPI test',
                               proc_id_type=1, proc_id=self.otp,
                               macs=generate_macs(self.otp))

    def store(self, cfg):
        data = encode_config(cfg, self.service.catalog)
        self.path.write_bytes(data + b'T' * (256 - len(data)))
        return self.path.read_bytes()

    def corrected_crc(self, data):
        data = bytearray(data)
        offset = 104 if data[4] == 2 else 122
        data[offset:offset + 4] = struct.pack('<I', zlib.crc32(data[:offset]))
        return bytes(data)

    def test_exact_v3_layout_and_crc(self):
        data = encode_config(self.cfg, self.service.catalog)
        header = struct.pack('<4sBBIHIIBBBB32s', b'NAPI', 3, 1, 0, 1,
                             1 << 10, 1234, 26, 9, 15, 8, b'NAPI test')
        body = header + b''.join(self.cfg.macs) + b'\x01\x05' + self.otp + bytes(11)
        self.assertEqual(data, body + struct.pack('<I', zlib.crc32(body)))
        self.assertEqual(len(data), 126)
        self.assertEqual(data[104:122], b'\x01\x05' + self.otp + bytes(11))
        self.assertEqual(decode_config(data, self.service.catalog), self.cfg)
        corrupt = bytearray(data)
        corrupt[106] ^= 1
        with self.assertRaisesRegex(ValueError, 'CRC mismatch'):
            decode_config(corrupt, self.service.catalog)

    def test_version_controls_layout_before_crc(self):
        v2 = replace(self.cfg, format_version=2, proc_id_type=0, proc_id=b'')
        data = encode_config(v2, self.service.catalog)
        self.assertEqual(len(data), 108)
        self.assertEqual(decode_config(data + b'\xff' * 148, self.service.catalog), v2)
        wrong = bytearray(data)
        wrong[4] = 3
        with self.assertRaisesRegex(ValueError, 'too short'):
            decode_config(wrong, self.service.catalog)
        wrong[4] = 99
        with self.assertRaisesRegex(ValueError, 'Unsupported EEPROM format'):
            decode_config(wrong, self.service.catalog)

    def test_invalid_processor_fields_with_valid_crc(self):
        valid = encode_config(self.cfg, self.service.catalog)
        cases = []
        for kind, length, area in [(0, 5, self.otp + bytes(11)), (0, 0, b'\x01' + bytes(15)),
                                   (1, 4, self.otp[:4] + bytes(12)), (1, 17, bytes(16)),
                                   (1, 5, bytes(16)), (1, 5, b'\xff' * 5 + bytes(11)),
                                   (1, 5, self.otp + b'\x01' + bytes(10)),
                                   (2, 5, self.otp + bytes(11))]:
            data = bytearray(valid)
            data[104:122] = bytes([kind, length]) + area
            cases.append(self.corrected_crc(data))
        for data in cases:
            with self.subTest(fields=data[104:122].hex()), self.assertRaises(ValueError):
                decode_config(data, self.service.catalog)

    def test_unset_binding_and_document_round_trip(self):
        cfg = replace(self.cfg, proc_id_type=0, proc_id=b'')
        self.assertEqual(encode_config(cfg, self.service.catalog)[104:122], bytes(18))
        document = to_document(self.cfg)
        self.assertEqual(document['proc_id'], '0235221703')
        self.assertEqual(document['proc_id_len'], 5)
        self.assertEqual(from_document(document), self.cfg)
        with self.assertRaisesRegex(ValueError, 'proc_id_len'):
            from_document(dict(document, proc_id_len=4))

    def test_processor_states_without_writes(self):
        for cfg, expected in [(replace(self.cfg, proc_id_type=0, proc_id=b''), 'NOT BOUND'),
                              (self.cfg, 'MATCH'),
                              (replace(self.cfg, proc_id=bytes.fromhex('090b131d04')), 'PROCESSOR MISMATCH')]:
            before = self.store(cfg)
            self.assertEqual(self.service.processor_status()['state'], expected)
            self.assertEqual(self.path.read_bytes(), before)
        self.otp_path.unlink()
        self.assertEqual(self.service.processor_status()['state'], 'OTP UNAVAILABLE')

    def test_migrate_requires_confirmation_preserves_instance_and_reserved_tail(self):
        v2 = replace(self.cfg, format_version=2, proc_id_type=0, proc_id=b'')
        before = self.store(v2)
        plan = self.service.migration_plan()
        self.assertEqual(self.path.read_bytes(), before)
        with self.assertRaisesRegex(ValueError, 'confirmation'):
            self.service.apply_instance_eeprom_plan(plan)
        self.assertFalse((self.root / 'eeprom-backups').exists())
        self.assertTrue(self.service.apply_instance_eeprom_plan(plan, confirmed=True))
        self.assertEqual(self.service.read_eeprom(), replace(v2, format_version=3))
        after = self.path.read_bytes()
        self.assertEqual(after[:4] + after[5:104], before[:4] + before[5:104])
        self.assertEqual(after[126:], before[126:])
        self.assertEqual(Path(self.service.last_eeprom_backup).read_bytes(), before)

    def test_migration_canonicalizes_only_legacy_rtc_bit(self):
        v2 = replace(self.cfg, format_version=2, proc_id_type=0, proc_id=b'', rtc_i2c1='ds1338')
        data = bytearray(encode_config(v2, self.service.catalog))
        data[12:16] = ((1 << 10) | 1 | (1 << 31)).to_bytes(4, 'little')
        before = self.corrected_crc(data) + b'T' * 148
        self.path.write_bytes(before)
        plan = self.service.migration_plan()
        self.assertEqual(int.from_bytes(plan['after'][12:16], 'little'), (1 << 10) | (1 << 14) | (1 << 31))
        self.assertEqual(decode_config(plan['after'], self.service.catalog).rtc_i2c1, 'ds1338')

    def test_v3_reserved_bit_zero_does_not_enable_rtc(self):
        data = bytearray(encode_config(self.cfg, self.service.catalog))
        data[12] |= 1
        self.assertEqual(decode_config(self.corrected_crc(data), self.service.catalog).rtc_i2c1, 'none')
        data[13] |= (1 << 5) | (1 << 6)
        with self.assertRaisesRegex(ValueError, 'mutually exclusive'):
            decode_config(self.corrected_crc(data), self.service.catalog)

    def test_explicit_binding_and_rebinding_preserve_serial_and_macs(self):
        before = self.store(replace(self.cfg, proc_id_type=0, proc_id=b''))
        plan = self.service.processor_plan()
        self.assertEqual(self.path.read_bytes(), before)
        with self.assertRaisesRegex(ValueError, 'confirmation'):
            self.service.apply_instance_eeprom_plan(plan)
        self.service.apply_instance_eeprom_plan(plan, confirmed=True)
        self.assertEqual(self.service.read_eeprom(), self.cfg)
        self.otp_path.write_bytes(bytes(20) + bytes.fromhex('090b131d04'))
        with self.assertRaisesRegex(ValueError, 'explicit processor rebind'):
            self.service.processor_plan()
        plan = self.service.processor_plan(rebind=True)
        self.service.apply_instance_eeprom_plan(plan, confirmed=True)
        actual = self.service.read_eeprom()
        self.assertEqual(actual.serial_number, self.cfg.serial_number)
        self.assertEqual(actual.macs, self.cfg.macs)
        self.assertEqual(actual.proc_id.hex(), '090b131d04')
        self.assertEqual(self.path.read_bytes()[126:], before[126:])

    def test_repeated_binding_skips_backup_and_write(self):
        self.store(self.cfg)
        plan = self.service.processor_plan()
        with patch.object(self.service.eeprom, 'write') as write:
            self.assertFalse(self.service.apply_instance_eeprom_plan(plan, confirmed=True))
            write.assert_not_called()
        self.assertFalse((self.root / 'eeprom-backups').exists())

    def test_stale_eeprom_and_changed_otp_rejected(self):
        before = self.store(replace(self.cfg, proc_id_type=0, proc_id=b''))
        plan = self.service.processor_plan()
        self.path.write_bytes(before[:-1] + b'X')
        with self.assertRaisesRegex(ValueError, 'changed since preview'):
            self.service.apply_instance_eeprom_plan(plan, confirmed=True)
        self.path.write_bytes(before)
        self.otp_path.write_bytes(bytes(20) + bytes.fromhex('090b131d04'))
        with self.assertRaisesRegex(ValueError, 'Processor changed'):
            self.service.apply_instance_eeprom_plan(plan, confirmed=True)
        self.assertEqual(self.path.read_bytes(), before)

    def test_tampered_binding_plan_and_unrelated_changes_rejected(self):
        before = self.store(replace(self.cfg, proc_id_type=0, proc_id=b''))
        plan = self.service.processor_plan()
        bad = dict(plan, after=encode_config(replace(self.cfg, serial_number=999), self.service.catalog))
        with self.assertRaisesRegex(ValueError, 'changed while preparing'):
            self.service.apply_instance_eeprom_plan(bad, confirmed=True)
        bad = dict(plan, after=encode_config(replace(self.cfg, proc_id=bytes.fromhex('090b131d04')), self.service.catalog))
        with self.assertRaisesRegex(ValueError, 'does not match current processor'):
            self.service.apply_instance_eeprom_plan(bad, confirmed=True)
        self.assertEqual(self.path.read_bytes(), before)

    def test_full_write_cannot_implicitly_migrate_or_change_binding(self):
        before = self.store(self.cfg)
        for cfg in [replace(self.cfg, proc_id_type=0, proc_id=b''),
                    replace(self.cfg, proc_id=bytes.fromhex('090b131d04'))]:
            with self.assertRaisesRegex(ValueError, 'explicit processor rebind'):
                self.service.write_eeprom(cfg, confirmed=True)
        self.assertEqual(self.path.read_bytes(), before)
        self.store(replace(self.cfg, format_version=2, proc_id_type=0, proc_id=b''))
        with self.assertRaisesRegex(ValueError, 'explicit eeprom migrate'):
            self.service.write_eeprom(replace(self.cfg, proc_id_type=0, proc_id=b''), confirmed=True)

    def test_profiles_preserve_binding_and_exclude_instance_fields(self):
        profile = config_to_profile(BoardConfig(product_id=6, enabled={'i2c1'}))
        actual = self.service.apply_profile(self.cfg, profile, confirmed=True)
        self.assertEqual((actual.proc_id_type, actual.proc_id, actual.serial_number, actual.macs),
                         (self.cfg.proc_id_type, self.cfg.proc_id, self.cfg.serial_number, self.cfg.macs))
        self.service.save_profile(actual, confirmed=True)
        for field in ('proc_id', 'serial_number', 'macs', 'mfg_date'):
            self.assertNotIn(field, (self.root / 'boards.yaml').read_text())
        for field in ('proc_id', 'proc_id_type', 'proc_id_len'):
            with self.assertRaisesRegex(ValueError, 'Instance'):
                self.service.validate_profiles({'boards': [dict(profile, **{field: 1})]})

    def test_mac_only_write_preserves_binding_and_v2_layout(self):
        for cfg in [self.cfg, replace(self.cfg, format_version=2, proc_id_type=0, proc_id=b'')]:
            before = self.store(replace(cfg, macs=[]))
            plan = self.service.mac_eeprom_plan(self.cfg.macs)
            self.service.apply_mac_eeprom_plan(plan, confirmed=True)
            after = self.path.read_bytes()
            end = 122 if cfg.format_version == 3 else 104
            self.assertEqual(after[104:end], before[104:end])
            self.assertEqual(self.service.read_eeprom(), cfg)
            size = 126 if cfg.format_version == 3 else 108
            self.assertEqual(after[size:], before[size:])

    def test_mismatch_and_unavailable_otp_do_not_regenerate_or_bind(self):
        cfg = replace(self.cfg, proc_id=bytes.fromhex('090b131d04'))
        self.store(cfg)
        with self.assertRaisesRegex(ValueError, 'PROCESSOR MISMATCH'):
            self.service.mac_plan(cfg)
        with self.assertRaisesRegex(ValueError, 'PROCESSOR MISMATCH'):
            self.service.mac_eeprom_plan(self.cfg.macs)
        self.otp_path.unlink()
        with self.assertRaises(ValueError):
            self.service.processor_plan(rebind=True)

    def test_v3_writes_and_migration_preserve_independent_bus_setting(self):
        self.assertNotIn('i2c1', self.service.defaults().enabled)
        cfg = BoardConfig()
        self.assertTrue(self.service.write_eeprom(cfg, confirmed=True))
        self.assertEqual(self.service.read_eeprom(), cfg)
        plan = self.service.processor_plan()
        self.service.apply_instance_eeprom_plan(plan, confirmed=True)
        self.assertNotIn('i2c1', self.service.read_eeprom().enabled)
        self.store(BoardConfig(format_version=2))
        plan = self.service.migration_plan()
        self.service.apply_instance_eeprom_plan(plan, confirmed=True)
        self.assertEqual(self.service.read_eeprom(), cfg)
        plan = self.service.mac_eeprom_plan(self.cfg.macs)
        self.service.apply_mac_eeprom_plan(plan, confirmed=True)
        self.assertNotIn('i2c1', self.service.read_eeprom().enabled)

    def test_tui_preview_and_confirmation_cancellation_do_not_write(self):
        self.store(replace(self.cfg, proc_id_type=0, proc_id=b''))
        ui = UI(Mock(), self.service)
        ui.view_comparison = Mock(return_value=False)
        ui.confirm_yes = Mock(return_value=False)
        with patch.object(self.service.eeprom, 'write') as write:
            ui.change_instance('bind')
            ui.confirm_yes.assert_not_called()
            ui.view_comparison.return_value = True
            ui.change_instance('bind')
            ui.confirm_yes.assert_called_once()
            write.assert_not_called()

    def test_backup_failure_blocks_write_and_readback_failure_is_reported(self):
        before = self.store(replace(self.cfg, proc_id_type=0, proc_id=b''))
        plan = self.service.processor_plan()
        with patch.object(self.service.configurations, 'backup_eeprom', side_effect=OSError('backup failed')):
            with patch.object(self.service.eeprom, 'write') as write:
                with self.assertRaisesRegex(OSError, 'backup failed'):
                    self.service.apply_instance_eeprom_plan(plan, confirmed=True)
                write.assert_not_called()
        with patch.object(self.service.eeprom, 'write'):
            with self.assertRaisesRegex(OSError, 'read-back mismatch'):
                self.service.apply_instance_eeprom_plan(plan, confirmed=True)
        self.assertEqual(Path(self.service.last_eeprom_backup).read_bytes(), before)

    def test_binding_requires_v3_but_migration_does_not_require_otp(self):
        self.store(replace(self.cfg, format_version=2, proc_id_type=0, proc_id=b''))
        self.otp_path.unlink()
        with self.assertRaisesRegex(ValueError, 'migrate v2 explicitly'):
            self.service.processor_plan()
        plan = self.service.migration_plan()
        self.service.apply_instance_eeprom_plan(plan, confirmed=True)
        self.assertEqual(self.service.read_eeprom().proc_id_type, 0)

    def test_mac_write_rechecks_binding_after_preview(self):
        before = self.store(replace(self.cfg, macs=[]))
        plan = self.service.mac_eeprom_plan(self.cfg.macs)
        self.otp_path.write_bytes(bytes(20) + bytes.fromhex('090b131d04'))
        with patch.object(self.service.eeprom, 'write_ranges') as write:
            with self.assertRaisesRegex(ValueError, 'PROCESSOR MISMATCH'):
                self.service.apply_mac_eeprom_plan(plan, confirmed=True)
            write.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)

    def test_cli_explicit_migration_and_binding_flow(self):
        self.store(replace(self.cfg, format_version=2, proc_id_type=0, proc_id=b''))
        common = ['--eeprom', str(self.path), '--db', str(self.root / 'boards.yaml'), '--json']

        def cli(group, action, *args):
            return subprocess.run([sys.executable, '-B', '-m', 'napi_config', group, action,
                                   *common, *args], capture_output=True, text=True)

        before = self.path.read_bytes()
        preview = cli('eeprom', 'migrate', '--preview')
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.assertEqual(json.loads(preview.stdout)['preview']['title'], 'MIGRATE EEPROM v2 TO v3')
        self.assertNotEqual(cli('eeprom', 'migrate').returncode, 0)
        self.assertEqual(self.path.read_bytes(), before)
        migrated = cli('eeprom', 'migrate', '--yes')
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        self.assertTrue(Path(json.loads(migrated.stdout)['backup']).exists())
        status = cli('processor', 'status', '--otp', str(self.otp_path))
        self.assertEqual(json.loads(status.stdout)['state'], 'NOT BOUND')
        preview = cli('processor', 'bind', '--preview', '--otp', str(self.otp_path))
        self.assertEqual(preview.returncode, 0, preview.stderr)
        bound = cli('processor', 'bind', '--yes', '--otp', str(self.otp_path))
        self.assertEqual(bound.returncode, 0, bound.stderr)
        status = cli('processor', 'status', '--otp', str(self.otp_path))
        self.assertEqual(json.loads(status.stdout)['state'], 'MATCH')


if __name__ == '__main__':
    unittest.main()

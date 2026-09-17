import errno
import fcntl
import struct
import tempfile
import time
import unittest
import zlib
from pathlib import Path
from unittest.mock import Mock, patch

from napi_config.bootstrap import create_service
from napi_config.core.codec import encode_config, decode_config
from napi_config.core.mac import generate_macs, MacPolicy, MacSlot, mac_assignments, OTP_ERROR
from napi_config.core.models import BoardConfig
from napi_config.hardware.otp import LinuxOtp
from napi_config.tui.app import UI


class MacTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.otp = self.root / 'nvmem'
        self.otp.write_bytes(bytes(8) + b'RKY32003' + bytes(4) + bytes.fromhex('0235221703'))
        self.eeprom = self.root / 'eeprom'
        self.eeprom.write_bytes(b'\xff' * 256)
        self.service = create_service(eeprom_path=str(self.eeprom), otp_path=str(self.otp))

    def test_known_vectors_and_fixed_slots(self):
        # SHA256 reference digests independently checked with OpenSSL.
        vectors = [('0235221703', '02981c8e55e0'), ('090b131d04', '029ee6974d60')]
        for otp, first in vectors:
            with self.subTest(otp=otp):
                macs = generate_macs(bytes.fromhex(otp))
                expected = bytes.fromhex(first)
                self.assertEqual(macs, [expected[:5] + bytes([expected[5] | i]) for i in range(8)])
                self.assertEqual(len(set(macs)), 8)
                self.assertEqual(macs[MacSlot.W5500_SPI1][5] & 7, 1)
                self.assertEqual(macs[MacSlot.W5500_SPI2][5] & 7, 2)
                self.assertTrue(all(mac[0] == 2 for mac in macs))

    def test_serial_and_interface_independence_and_no_eeprom_write(self):
        before = self.eeprom.read_bytes()
        cfg = self.service.generate_macs(BoardConfig(serial_number=1), confirmed=True)
        other = self.service.generate_macs(BoardConfig(serial_number=999, enabled={'usb_host'}), confirmed=True)
        self.assertEqual(cfg.macs, other.macs)
        self.assertEqual(cfg.macs, self.service.generate_macs(cfg, confirmed=True).macs)
        self.assertEqual(self.eeprom.read_bytes(), before)

    def test_invalid_ids_no_fallback(self):
        for otp in (b'', bytes(4), bytes(6), bytes(5), b'\xff' * 5, '0235221703'):
            with self.subTest(otp=otp), self.assertRaisesRegex(ValueError, OTP_ERROR):
                generate_macs(otp)
        before = self.eeprom.read_bytes()
        cfg = BoardConfig(serial_number=123, macs=[bytes.fromhex('02aabbccddee')])
        for content in (bytes(24), bytes(25), bytes(20) + b'\xff' * 5):
            self.otp.write_bytes(content)
            with self.assertRaisesRegex(ValueError, OTP_ERROR):
                self.service.generate_macs(cfg, confirmed=True)
            self.assertEqual(cfg.macs, [bytes.fromhex('02aabbccddee')])
            self.assertEqual(self.eeprom.read_bytes(), before)
        self.otp.unlink()
        with self.assertRaisesRegex(ValueError, OTP_ERROR):
            self.service.generate_macs(cfg, confirmed=True)

    def test_otp_lock_timeout_and_release(self):
        reader = LinuxOtp(str(self.otp), timeout=0.06)
        with self.otp.open('rb') as holder:
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
            started = time.monotonic()
            with self.assertRaisesRegex(ValueError, OTP_ERROR):
                reader.read_id()
            self.assertLess(time.monotonic() - started, 1)
            fcntl.flock(holder, fcntl.LOCK_UN)
            self.assertEqual(reader.read_id(), bytes.fromhex('0235221703'))
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.otp.write_bytes(bytes(23))
        with self.assertRaisesRegex(ValueError, OTP_ERROR):
            reader.read_id()
        with self.otp.open('rb') as holder:
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_otp_open_seek_read_and_lock_errors(self):
        reader = LinuxOtp(str(self.otp))
        for target in ('builtins.open', 'napi_config.hardware.otp.os.lseek',
                       'napi_config.hardware.otp.os.read', 'napi_config.hardware.otp.fcntl.flock'):
            with self.subTest(target=target), patch(target, side_effect=OSError(errno.EIO, 'I/O error')):
                with self.assertRaisesRegex(ValueError, OTP_ERROR):
                    reader.read_id()

    def test_exact_eeprom_offsets_crc_and_no_duplicate_write(self):
        cfg = self.service.generate_macs(BoardConfig(), confirmed=True)
        encoded = encode_config(cfg, self.service.catalog)
        self.assertEqual(encoded[0x17], 8)
        for slot, mac in enumerate(cfg.macs):
            self.assertEqual(encoded[0x38 + slot * 6:0x3e + slot * 6], mac)
        self.assertEqual(struct.unpack('<I', encoded[0x68:0x6c])[0], zlib.crc32(encoded[:0x68]))
        self.assertEqual(decode_config(encoded, self.service.catalog), cfg)
        self.assertTrue(self.service.write_eeprom(cfg, confirmed=True))
        with patch.object(self.service.eeprom, 'write') as write:
            self.assertFalse(self.service.write_eeprom(cfg, confirmed=True))
            write.assert_not_called()

    def test_tui_preview_cancel_confirm_and_existing_match(self):
        ui = UI.__new__(UI)
        ui.service = self.service
        ui.cfg = BoardConfig(macs=[bytes.fromhex('02aabbccddee')])
        old = self.service.document(ui.cfg)
        ui.view_text = Mock(return_value=False)
        ui.confirm_yes = Mock(return_value=False)
        ui.generate_macs()
        self.assertEqual(self.service.document(ui.cfg), old)
        ui.confirm_yes.assert_not_called()
        text = ui.view_text.call_args.args[0]
        self.assertIn('0235221703', text)
        self.assertIn('MAC2  W5500 SPI1', text)
        self.assertIn('MAC addresses already exist', text)
        ui.view_text.return_value = True
        ui.generate_macs()
        self.assertEqual(self.service.document(ui.cfg), old)
        ui.confirm_yes.return_value = True
        ui.generate_macs()
        self.assertEqual(len(ui.cfg.macs), 8)
        ui.confirm_yes.reset_mock()
        ui.generate_macs()
        ui.confirm_yes.assert_not_called()
        self.assertIn('already match this SoC', ui.status)
        self.assertEqual(self.eeprom.read_bytes(), b'\xff' * 256)

    def test_native_policy_does_not_shift_slots_or_alter_eeprom(self):
        macs = generate_macs(bytes.fromhex('0235221703'))
        off = mac_assignments(macs, MacPolicy())
        on = mac_assignments(macs, MacPolicy(True))
        self.assertEqual([row['slot'] for row in off], [2, 3, 4, 5])
        self.assertEqual([row['slot'] for row in on], [1, 2, 3, 4, 5])
        self.assertEqual(on[1:], off)
        cfg = BoardConfig(macs=macs)
        before = encode_config(cfg, self.service.catalog)
        self.service.mac_policy = MacPolicy(True)
        self.assertEqual(self.service.mac_assignments(cfg)['assignments'], on)
        self.assertEqual(encode_config(cfg, self.service.catalog), before)
        for value in ('false', 0, None):
            with self.assertRaises(ValueError):
                MacPolicy.from_document({'set_native_eth_mac': value})

    def test_preview_preserves_stored_macs_after_defaults_and_rejects_stale_config(self):
        stored = BoardConfig(macs=[bytes.fromhex('02aabbccddee')])
        self.service.write_eeprom(stored, confirmed=True)
        cfg = BoardConfig()
        plan = self.service.mac_plan(cfg)
        self.assertEqual(plan['eeprom_current']['macs'], ['02:AA:BB:CC:DD:EE'])
        with self.assertRaisesRegex(ValueError, 'confirmation'):
            self.service.apply_mac_plan(cfg, plan)
        changed = BoardConfig(serial_number=1)
        with self.assertRaisesRegex(ValueError, 'changed since MAC preview'):
            self.service.apply_mac_plan(changed, plan, confirmed=True)
        self.assertEqual(self.service.read_eeprom(), stored)

    def test_tui_eeprom_write_requires_preview_and_yes(self):
        cfg = self.service.generate_macs(BoardConfig(), confirmed=True)
        ui = UI.__new__(UI)
        ui.service = self.service
        ui.cfg = cfg
        ui.last_mac_plan = self.service.mac_plan(cfg)
        ui.view_text = Mock(return_value=False)
        ui.confirm_yes = Mock(return_value=False)
        with patch.object(self.service, 'write_eeprom') as write:
            ui.write_eeprom()
            ui.confirm_yes.assert_not_called()
            write.assert_not_called()
            ui.view_text.return_value = True
            ui.write_eeprom()
            write.assert_not_called()
            ui.confirm_yes.return_value = True
            ui.write_eeprom()
            write.assert_called_once_with(cfg, confirmed=True)
        text = ui.view_text.call_args.args[0]
        self.assertIn('RK3308 OTP ID: 0235221703', text)
        self.assertIn('Current EEPROM MACs', text)
        self.assertIn('MAC8  Reserved', text)

    def test_policy_loads_from_settings_separately_from_eeprom(self):
        path = self.root / 'settings.yaml'
        path.write_text('set_native_eth_mac: true\n')
        service = create_service(eeprom_path=str(self.eeprom), otp_path=str(self.otp), settings_path=str(path))
        cfg = service.generate_macs(BoardConfig(), confirmed=True)
        self.assertTrue(service.mac_assignments(cfg)['set_native_eth_mac'])
        self.assertEqual(service.mac_assignments(cfg)['assignments'][0]['slot'], 1)
        self.assertEqual(cfg.macs, self.service.generate_macs(BoardConfig(), confirmed=True).macs)

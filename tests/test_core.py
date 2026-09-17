import ast
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from napi_config.bootstrap import create_service
from napi_config.core.codec import encode_config, decode_config
from napi_config.core.configuration import from_document, to_document
from napi_config.core.models import BoardConfig
from napi_config.core.profiles import config_to_profile


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        self.eeprom = self.path / 'eeprom'
        self.eeprom.write_bytes(b'\xff' * 256)
        self.service = create_service(str(self.eeprom), str(self.path / 'boards.yaml'))
        self.service.otp = Mock()
        self.service.otp.read_id.return_value = bytes.fromhex('0235221703')
        self.cfg = BoardConfig(serial_number=0xffffffff, mfg_date='2255-12-31', enabled={'i2c1'})

    def test_round_trip_and_crc(self):
        cfg = self.service.generate_macs(self.cfg, confirmed=True)
        blob = encode_config(cfg, self.service.catalog)
        self.assertEqual(decode_config(blob, self.service.catalog), cfg)
        corrupt = bytearray(blob)
        corrupt[15] ^= 1
        with self.assertRaisesRegex(ValueError, 'CRC mismatch'):
            decode_config(bytes(corrupt), self.service.catalog)
        with self.assertRaisesRegex(ValueError, 'too short'):
            decode_config(blob[:10], self.service.catalog)

    def test_v15_binary_compatibility(self):
        # Header/layout comparison against independently specified struct fields.
        import struct, zlib
        header = struct.pack('<4sBBIHIIBBBB32s', b'NAPI', 2, 1, 0, 1, 1 << 10, 0, 0, 0, 0, 0, b'NAPI Board')
        body = header + bytes(48)
        self.assertEqual(encode_config(BoardConfig(format_version=2, enabled={'i2c1'}), self.service.catalog), body + struct.pack('<I', zlib.crc32(body)))

    def test_confirmation_prevents_writes(self):
        self.service.eeprom = Mock()
        with self.assertRaisesRegex(ValueError, 'confirmation'):
            self.service.write_eeprom(self.cfg)
        self.service.eeprom.write.assert_not_called()
        with self.assertRaises(ValueError):
            self.service.generate_macs(self.cfg)
        self.assertEqual(self.cfg.macs, [])

    def test_write_preserves_tail_and_verifies(self):
        self.service.write_eeprom(self.cfg, confirmed=True)
        self.assertEqual(self.service.read_eeprom(), self.cfg)
        size = len(encode_config(self.cfg, self.service.catalog))
        self.assertEqual(self.eeprom.read_bytes()[size:], b'\xff' * (256-size))

    def test_readback_mismatch(self):
        other = encode_config(BoardConfig(product_id=42), self.service.catalog)
        self.service.eeprom = Mock()
        self.service.eeprom.read.return_value = other
        self.service.eeprom.availability.return_value = {'enabled': True, 'reason': ''}
        with self.assertRaisesRegex(OSError, 'mismatch'):
            self.service.write_eeprom(self.cfg, confirmed=True)

    def test_field_limits_and_dates(self):
        for key, value in [('serial_number', -1), ('product_id', 1 << 32), ('product_rev', 1 << 16), ('mfg_date', '2026-02-30'), ('mfg_date', '1999-12-31'), ('board_name', 'я' * 17)]:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                self.service.update(self.cfg, key, value)
        self.assertEqual(self.cfg.serial_number, 0xffffffff)

    def test_mac_block(self):
        cfg = self.service.generate_macs(self.cfg, confirmed=True)
        self.assertEqual(len(set(cfg.macs)), 8)
        base = int.from_bytes(cfg.macs[0], 'big')
        for i, mac in enumerate(cfg.macs):
            self.assertEqual(mac[0] & 3, 2)
            self.assertEqual(int.from_bytes(mac, 'big'), base+i)
        self.assertEqual(from_document(to_document(cfg)), cfg)

    def test_profiles_preserve_instance_and_exclude_it_from_yaml(self):
        cfg = self.service.generate_macs(self.cfg, confirmed=True)
        profile = config_to_profile(BoardConfig(product_id=6, board_name='FCU3308'))
        with self.assertRaises(ValueError):
            self.service.apply_profile(cfg, profile)
        new = self.service.apply_profile(cfg, profile, confirmed=True)
        self.assertEqual((new.serial_number, new.mfg_date, new.macs), (cfg.serial_number, cfg.mfg_date, cfg.macs))
        self.service.save_profile(new, confirmed=True)
        text = (self.path / 'boards.yaml').read_text()
        for key in ['serial_number', 'mfg_date', 'macs']:
            self.assertNotIn(key, text)
        self.service.save_profile(new, confirmed=True)
        self.assertTrue(list(self.path.glob('boards.yaml.bak-*')))

    def test_conflicts_and_required_bus(self):
        cfg = self.service.toggle_interface(BoardConfig(), 'w5500_spi1')
        with self.assertRaises(ValueError):
            self.service.toggle_interface(cfg, 'spi1')
        cfg = self.service.toggle_interface(cfg, 'i2c1')
        self.assertIn('i2c1', cfg.enabled)
        cfg = self.service.toggle_interface(cfg, 'i2c1')
        self.assertNotIn('i2c1', cfg.enabled)
        self.assertEqual(decode_config(encode_config(cfg, self.service.catalog), self.service.catalog), cfg)
        profile = config_to_profile(cfg)
        self.assertFalse(profile['i2c']['i2c1']['enabled'])
        self.assertNotIn('eeprom', profile['i2c']['i2c1'])
        cfg = self.service.toggle_rtc(cfg)
        self.assertIn('i2c1', cfg.enabled)
        with self.assertRaisesRegex(ValueError, 'RTC'):
            self.service.toggle_interface(cfg, 'i2c1')

    def test_boot_preview_and_stale_plan(self):
        self.service.boot = Mock()
        self.service.boot.detect.return_value = '/boot/armbianEnv.txt'
        self.service.boot.inventory.return_value = {'overlay_files': {'rk3308-i2c1': '/fake.dtbo'}, 'user_overlay_files': {'custom': '/fake.dtbo'}}
        self.service.boot.read.return_value = 'overlay_prefix=rk3308\nother=keep\noverlays=old\nuser_overlays=custom\n'
        plan = self.service.boot_plan(BoardConfig(enabled={'i2c1'}))
        self.assertIn('other=keep', plan['after'])
        self.assertIn('user_overlays=custom', plan['after'])
        self.assertIn('overlays=i2c1', plan['after'])
        self.service.boot.write.assert_not_called()
        with self.assertRaises(ValueError):
            self.service.apply_boot_plan(plan)
        self.service.boot.read.return_value = 'changed'
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.service.apply_boot_plan(plan, confirmed=True)
        self.service.boot.write.assert_not_called()

    def test_frontends_do_not_access_hardware_or_storage(self):
        for path in [Path('napi_config/tui/app.py'), Path('napi_config/cli/app.py')]:
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    self.assertFalse(any(alias.name in ('os', 'pathlib', 'yaml', 'secrets', 'struct') for alias in node.names))
                if isinstance(node, ast.ImportFrom):
                    self.assertFalse((node.module or '').startswith(('hardware', 'storage')))


if __name__ == '__main__':
    unittest.main()

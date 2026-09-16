import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from napi_config.bootstrap import create_service
from napi_config.core.models import BoardConfig
from napi_config.hardware.boot import LinuxBootFiles
from napi_config.tui.app import UI


class BootTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.stock = self.root / 'dtb/rockchip/overlay'
        self.stock.mkdir(parents=True)
        self.user = self.root / 'overlay-user'
        self.user.mkdir()
        self.eeprom = self.root / 'eeprom'
        self.eeprom.write_bytes(bytes(256))
        self.service = create_service(str(self.eeprom), boot_dir=str(self.root))

    def env(self, text, filename='armbianEnv.txt'):
        path = self.root / filename
        path.write_text(text)
        return path

    def files(self, *names):
        for name in names:
            (self.stock / (name + '.dtbo')).touch()

    def test_armbian_runtime_prefix_and_alias(self):
        path = self.env('overlay_prefix=napi-rk3308\nfdtfile=rockchip/rk3308-napi-c.dtb\nother=preserve\n')
        self.files('napi-rk3308-i2c1', 'napi-rk3308-otg-host')
        cfg = BoardConfig(enabled={'i2c1', 'usb_host'})
        self.assertEqual(self.service.overlays(cfg)['overlays'], ['i2c1', 'otg-host'])
        plan = self.service.boot_plan(cfg)
        self.assertIn('overlay_prefix=napi-rk3308\n', plan['after'])
        self.assertIn('overlays=i2c1 otg-host\n', plan['after'])
        self.assertIn('other=preserve', plan['after'])
        self.assertEqual(path.read_text(), plan['before'])
        with self.assertRaises(ValueError):
            self.service.apply_boot_plan(plan)
        self.assertEqual(path.read_text(), plan['before'])
        backup = self.service.apply_boot_plan(plan, confirmed=True)
        self.assertEqual(Path(backup).read_text(), plan['before'])
        self.assertEqual(path.read_text(), plan['after'])

    def test_napilinux_full_names_and_comment_preservation(self):
        self.env('fdtfile=rk3308-napi-c.dtb\n#overlays=rk3308-uart1\nverbosity=7\n', 'uEnv.txt')
        directory = self.root / 'dtb/overlay'
        directory.mkdir()
        for name in ['rk3308-i2c1-ds1338', 'rk3308-spi2-w5500', 'rk3308-usb20-host']:
            (directory / (name + '.dtbo')).touch()
        cfg = BoardConfig(enabled={'i2c1', 'w5500_spi2', 'usb_host'}, rtc_i2c1='ds1338')
        plan = self.service.boot_plan(cfg)
        self.assertNotIn('overlay_prefix=', plan['after'])
        self.assertIn('#overlays=rk3308-uart1', plan['after'])
        self.assertIn('overlays=rk3308-i2c1-ds1338 rk3308-usb20-host rk3308-spi2-w5500', plan['after'])

    def test_missing_overlay_warns_and_can_be_written_after_confirmation(self):
        path = self.env('overlay_prefix=rk3308\nfdtfile=rockchip/rk3308-napi-c.dtb\nuser_overlays=custom-any-name  another-name  \n')
        original = path.read_text()
        plan = self.service.boot_plan(BoardConfig(enabled={'i2c1'}))
        self.assertIn('rk3308-i2c1.dtbo', plan['warnings'][0])
        self.assertEqual(plan['proposed_overlay_string'], 'overlays=i2c1')
        self.assertEqual(plan['current_user_overlay_string'], 'user_overlays=custom-any-name  another-name')
        self.assertEqual(plan['proposed_user_overlay_string'], plan['current_user_overlay_string'])
        ui = UI.__new__(UI)
        ui.view_text = Mock()
        ui.preview_boot(plan)
        self.assertEqual(ui.view_text.call_args.args[0].count(plan['current_user_overlay_string']), 2)
        self.assertEqual(path.read_text(), original)
        with self.assertRaises(ValueError):
            self.service.apply_boot_plan(plan)
        backup = self.service.apply_boot_plan(plan, confirmed=True)
        self.assertEqual(Path(backup).read_text(), original)
        self.assertIn('user_overlays=custom-any-name  another-name  \n', path.read_text())
        self.assertIn('overlays=i2c1\n', path.read_text())

    def test_tui_view_only_and_confirmation_cancellation(self):
        self.env('overlay_prefix=rk3308\n')
        ui = UI.__new__(UI)
        ui.service = self.service
        ui.cfg = BoardConfig()
        ui.preview_boot = Mock(return_value=False)
        ui.confirm_yes = Mock(return_value=False)
        self.service.apply_boot_plan = Mock()
        ui.write_env()
        ui.preview_boot.assert_called_once()
        ui.confirm_yes.assert_not_called()
        self.service.apply_boot_plan.assert_not_called()
        ui.preview_boot.return_value = True
        ui.write_env()
        ui.confirm_yes.assert_called_once()
        self.service.apply_boot_plan.assert_not_called()

    def test_startup_loads_valid_eeprom_and_falls_back_on_bad_crc(self):
        cfg = BoardConfig(board_name='From EEPROM', serial_number=12, mfg_date='2026-09-16')
        self.service.write_eeprom(cfg, confirmed=True)
        ui = UI(Mock(), self.service)
        self.assertEqual(ui.cfg, cfg)
        self.assertIn('EEPROM configuration loaded', ui.status)
        self.eeprom.write_bytes(b'\xff' * 256)
        ui = UI(Mock(), self.service)
        self.assertEqual(ui.cfg, self.service.defaults())
        self.assertIn('CRC mismatch', ui.status)
        self.eeprom.unlink()
        ui = UI(Mock(), self.service)
        self.assertIn('EEPROM unavailable / not configured', ui.status)

    def test_both_env_files_and_deployed_script(self):
        self.env('overlay_prefix=rk3308\n')
        self.env('verbosity=7\n', 'uEnv.txt')
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            self.service.boot_info()
        (self.root / 'boot.cmd').write_text('load mmc 0:1 ${addr} ${prefix}uEnv.txt\n')
        (self.root / 'boot.scr').write_bytes(b'header\0\n# uEnv.txt old comment\nload mmc 0:1 ${addr} ${prefix}armbianEnv.txt\n')
        self.assertTrue(self.service.boot_info()['path'].endswith('armbianEnv.txt'))

    def test_script_selects_real_overlay_directory(self):
        self.env('overlay_prefix=rk3308\n')
        active = self.root / 'actual-overlays'
        active.mkdir()
        (active / 'rk3308-i2c1.dtbo').touch()
        self.files('rk3308-usb20-host')
        (self.root / 'boot.scr').write_text('for overlay_file in ${overlays}; do\nload mmc 0:1 ${addr} ${prefix}actual-overlays/${overlay_prefix}-${overlay_file}.dtbo;\ndone\n')
        self.assertEqual(self.service.overlays(BoardConfig(enabled={'i2c1'}))['overlays'], ['i2c1'])
        info = self.service.overlays(BoardConfig(enabled={'i2c1', 'usb_host'}))
        self.assertIn('rk3308-usb20-host.dtbo', info['warnings'][0])

    def test_eeprom_in_base_dtb_and_standard_only_candidates(self):
        self.env('overlay_prefix=rk3308\n')
        self.assertTrue(self.service.action_status(BoardConfig(), 'read')['enabled'])
        self.assertTrue(self.service.action_status(BoardConfig(), 'write_eeprom')['enabled'])
        self.assertTrue(self.service.eeprom_setup('rk3308')['available'])
        self.assertIn('additional overlay is not required', self.service.eeprom_setup('rk3308')['message'])
        self.eeprom.unlink()
        self.assertFalse(self.service.action_status(BoardConfig(), 'read')['enabled'])
        with self.assertRaisesRegex(ValueError, 'EEPROM unavailable / not configured'):
            self.service.read_eeprom()
        (self.user / 'rk3308-i2c1-eeprom.dtbo').touch()
        self.assertEqual(self.service.eeprom_setup('rk3308')['candidates'], [])
        status = self.service.action_status(BoardConfig(), 'enable_i2c1_eeprom')
        self.assertFalse(status['enabled'])
        self.assertIn('No standard EEPROM overlay found', status['reason'])
        self.files('rk3308-eeprom24', 'rk3568-eeprom24')
        candidates = self.service.eeprom_setup('rk3308')['candidates']
        self.assertEqual([c['name'] for c in candidates], ['eeprom24'])
        self.assertTrue(self.service.action_status(BoardConfig(), 'enable_i2c1_eeprom')['enabled'])

    def test_eeprom_service_preview_and_confirmation_cancellation(self):
        path = self.env('overlay_prefix=napi-rk3308\noverlays=otg-host\nuser_overlays=arbitrary  name  \n')
        self.eeprom.unlink()
        self.files('napi-rk3308-i2c1', 'napi-rk3308-eeprom24')
        plan = self.service.boot_plan(BoardConfig(), eeprom_overlay='eeprom24')
        self.assertIn('overlays=i2c1 otg-host eeprom24\n', plan['after'])
        self.assertIn('user_overlays=arbitrary  name  \n', plan['after'])
        self.assertEqual(path.read_text(), plan['before'])
        ui = UI.__new__(UI)
        ui.service = self.service
        ui.cfg = BoardConfig()
        ui.choose_eeprom_overlay = Mock(return_value={'name': 'eeprom24'})
        ui.preview_boot = Mock(return_value=False)
        ui.confirm_yes = Mock(return_value=False)
        self.service.apply_boot_plan = Mock()
        self.service.reboot = Mock()
        ui.enable_i2c1_eeprom()
        ui.confirm_yes.assert_not_called()
        ui.preview_boot.return_value = True
        ui.enable_i2c1_eeprom()
        ui.confirm_yes.assert_called_once()
        self.service.apply_boot_plan.assert_not_called()
        self.service.reboot.assert_not_called()

    def test_optional_eeprom_and_independent_i2c1(self):
        self.env('overlay_prefix=rk3308\noverlays=\nuser_overlays=custom\n')
        self.files('rk3308-i2c1', 'rk3308-eeprom24')
        self.assertEqual(self.service.overlays(BoardConfig())['overlays'], [])
        plan = self.service.boot_plan(BoardConfig(enabled={'i2c1'}))
        self.assertEqual(plan['proposed_overlay_string'], 'overlays=i2c1')
        self.assertNotIn('eeprom24', plan['after'])
        self.assertEqual(plan['proposed_user_overlay_string'], 'user_overlays=custom')
        with self.assertRaisesRegex(ValueError, 'standard EEPROM overlay not found'):
            self.service.boot_plan(BoardConfig(), eeprom_overlay='custom')

    def test_napilinux_eeprom_full_name_and_preservation(self):
        path = self.env('overlays=rk3308-i2c1-ds1338 rk3308-usb20-host\nuser_overlays=anything\n', 'uEnv.txt')
        self.files('rk3308-eeprom24')
        plan = self.service.boot_plan(BoardConfig(), eeprom_overlay='rk3308-eeprom24')
        self.assertEqual(plan['proposed_overlay_string'], 'overlays=rk3308-i2c1-ds1338 rk3308-usb20-host rk3308-eeprom24')
        backup = self.service.apply_boot_plan(plan, confirmed=True)
        self.assertEqual(Path(backup).read_text(), plan['before'])
        cfg = BoardConfig(enabled={'i2c1'}, rtc_i2c1='ds1338')
        self.assertIn('rk3308-eeprom24', self.service.boot_plan(cfg)['proposed_overlay_string'])
        (self.stock / 'rk3308-eeprom24.dtbo').unlink()
        path.write_text(plan['before'])
        with self.assertRaisesRegex(ValueError, 'dtbo not found'):
            self.service.apply_boot_plan(plan, confirmed=True)

    def test_disabled_action_remains_visible_and_explains_reason(self):
        self.env('overlay_prefix=rk3308\n')
        self.eeprom.unlink()
        ui = UI.__new__(UI)
        ui.service = self.service
        ui.cfg = BoardConfig()
        ui.rows = [('action', 'read')]
        ui.cursor = 0
        ui.action_states = {'read': self.service.action_status(ui.cfg, 'read')}
        ui.view_text = Mock()
        self.assertIn('disabled', ui.action_label('read'))
        self.assertFalse(ui.activate())
        self.assertIn('EEPROM unavailable / not configured', ui.view_text.call_args.args[0])

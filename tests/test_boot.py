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

    def test_missing_overlay_blocks_plan_and_apply(self):
        self.env('overlay_prefix=rk3308\nfdtfile=rockchip/rk3308-napi-c.dtb\n')
        with self.assertRaisesRegex(ValueError, 'rk3308-i2c1.dtbo'):
            self.service.boot_plan(BoardConfig())
        self.files('rk3308-i2c1')
        plan = self.service.boot_plan(BoardConfig())
        (self.stock / 'rk3308-i2c1.dtbo').unlink()
        with self.assertRaisesRegex(ValueError, 'not found'):
            self.service.apply_boot_plan(plan, confirmed=True)

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
        self.assertEqual(self.service.overlays(BoardConfig())['overlays'], ['i2c1'])
        with self.assertRaisesRegex(ValueError, 'not found'):
            self.service.overlays(BoardConfig(enabled={'i2c1', 'usb_host'}))

    def test_eeprom_in_base_dtb_does_not_require_overlay_file(self):
        self.env('overlay_prefix=rk3308\n')
        self.assertTrue(self.service.action_status(BoardConfig(), 'read')['enabled'])
        self.assertTrue(self.service.action_status(BoardConfig(), 'write_eeprom')['enabled'])
        status = self.service.action_status(BoardConfig(), 'enable_i2c1_eeprom')
        self.assertFalse(status['enabled'])
        self.assertIn('rk3308-i2c1-eeprom.dtbo not found', status['reason'])
        self.eeprom.unlink()
        self.assertFalse(self.service.action_status(BoardConfig(), 'read')['enabled'])
        with self.assertRaisesRegex(ValueError, 'EEPROM device not found'):
            self.service.read_eeprom()
        (self.user / 'rk3308-i2c1-eeprom.dtbo').touch()
        self.assertTrue(self.service.action_status(BoardConfig(), 'enable_i2c1_eeprom')['enabled'])

    def test_eeprom_service_confirmation_and_full_user_name(self):
        self.env('overlay_prefix=napi-rk3308\n')
        self.files('napi-rk3308-i2c1')
        (self.user / 'rk3308-i2c1-eeprom.dtbo').touch()
        plan = self.service.boot_plan(BoardConfig(), ensure_eeprom=True)
        self.assertIn('user_overlays=rk3308-i2c1-eeprom', plan['after'])
        ui = UI.__new__(UI)
        ui.service = self.service
        ui.cfg = BoardConfig()
        ui.preview_boot = Mock()
        ui.confirm_yes = Mock(return_value=False)
        self.service.apply_boot_plan = Mock()
        self.service.reboot = Mock()
        ui.enable_i2c1_eeprom()
        ui.confirm_yes.assert_called_once()
        self.service.apply_boot_plan.assert_not_called()
        self.service.reboot.assert_not_called()

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
        self.assertIn('EEPROM device not found', ui.view_text.call_args.args[0])

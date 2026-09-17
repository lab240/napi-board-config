import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class CliTests(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, '-B', '-m', 'napi_config', *args], capture_output=True, text=True)

    def test_production_flow(self):
        with tempfile.TemporaryDirectory() as directory:
            config = str(Path(directory) / 'instance.json')
            boot = Path(directory) / 'boot'
            overlays = boot / 'dtb/rockchip/overlay'
            overlays.mkdir(parents=True)
            (boot / 'armbianEnv.txt').write_text('overlay_prefix=rk3308\nfdtfile=rockchip/rk3308-napi-c.dtb\n')
            for name in ['i2c1-ds1338', 'i2c3-m0', 'usb20-host', 'uart1', 'uart2-m0', 'uart4', 'spi1-w5500']:
                (overlays / ('rk3308-' + name + '.dtbo')).touch()
            eeprom = Path(directory) / 'eeprom'
            eeprom.write_bytes(b'\xff' * 256)
            denied = self.run_cli('mac', 'generate', '--output', config)
            self.assertNotEqual(denied.returncode, 0)
            self.assertFalse(Path(config).exists())
            otp = Path(directory) / 'otp'
            otp.write_bytes(bytes(20) + bytes.fromhex('0235221703'))
            generated = self.run_cli('mac', 'generate', '--profile', '6', '1', '--output', config, '--yes', '--json', '--otp', str(otp))
            self.assertEqual(generated.returncode, 0, generated.stderr)
            data = json.loads(generated.stdout)['configuration']
            self.assertEqual(data['board_name'], 'FCU3308')
            self.assertEqual(len(data['macs']), 8)
            before = eeprom.read_bytes()
            denied = self.run_cli('eeprom', 'write', '--config', config, '--eeprom', str(eeprom))
            self.assertNotEqual(denied.returncode, 0)
            self.assertEqual(eeprom.read_bytes(), before)
            write = self.run_cli('eeprom', 'write', '--config', config, '--eeprom', str(eeprom), '--yes', '--json')
            self.assertEqual(write.returncode, 0, write.stderr)
            shown = self.run_cli('eeprom', 'show', '--eeprom', str(eeprom), '--json')
            self.assertEqual(shown.returncode, 0, shown.stderr)
            # Comment is profile-only and intentionally absent in EEPROM.
            decoded = json.loads(shown.stdout)
            self.assertEqual(decoded['macs'], data['macs'])
            self.assertEqual(decoded['product_id'], data['product_id'])
            for group, action in [('board', 'info'), ('overlay', 'list')]:
                result = self.run_cli(group, action, '--config', config, '--boot-dir', str(boot), '--json')
                self.assertEqual(result.returncode, 0, result.stderr)
                json.loads(result.stdout)
            env = boot / 'armbianEnv.txt'
            original = env.read_text()
            (overlays / 'rk3308-spi1-w5500.dtbo').unlink()
            preview = self.run_cli('boot', 'preview', '--config', config, '--boot-dir', str(boot), '--json')
            self.assertEqual(preview.returncode, 0, preview.stderr)
            self.assertEqual(env.read_text(), original)
            self.assertTrue(json.loads(preview.stdout)['changed'])
            self.assertIn('rk3308-spi1-w5500.dtbo', json.loads(preview.stdout)['warnings'][0])
            denied = self.run_cli('boot', 'write', '--config', config, '--boot-dir', str(boot))
            self.assertNotEqual(denied.returncode, 0)
            self.assertEqual(env.read_text(), original)
            applied = self.run_cli('boot', 'write', '--config', config, '--boot-dir', str(boot), '--yes', '--json')
            self.assertEqual(applied.returncode, 0, applied.stderr)
            result = json.loads(applied.stdout)
            self.assertFalse(result['reboot'])
            self.assertEqual(Path(result['backup']).read_text(), original)
            self.assertEqual(env.read_text(), json.loads(preview.stdout)['after'])

    def test_optional_eeprom_setup_cli_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            boot = Path(directory)
            stock = boot / 'dtb/rockchip/overlay'
            stock.mkdir(parents=True)
            user = boot / 'overlay-user'
            user.mkdir()
            (user / 'rk3308-eeprom24.dtbo').touch()
            env = boot / 'armbianEnv.txt'
            original = 'overlay_prefix=rk3308\noverlays=otg-host\nuser_overlays=arbitrary  name  \n'
            env.write_text(original)
            common = ['--boot-dir', str(boot), '--eeprom', str(boot / 'missing'), '--json']
            result = self.run_cli('eeprom', 'overlays', *common)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['candidates'], [])
            for name in ['rk3308-eeprom24', 'rk3308-i2c1']:
                (stock / (name + '.dtbo')).touch()
            result = self.run_cli('eeprom', 'overlays', *common)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['candidates'][0]['name'], 'eeprom24')
            denied = self.run_cli('eeprom', 'enable', '--overlay', 'eeprom24', *common)
            self.assertNotEqual(denied.returncode, 0)
            self.assertEqual(env.read_text(), original)
            self.assertEqual(list(boot.glob('*.bak-*')), [])
            result = self.run_cli('eeprom', 'enable', '--overlay', 'eeprom24', '--yes', *common)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout)
            self.assertFalse(data['reboot'])
            self.assertEqual(Path(data['backup']).read_text(), original)
            self.assertIn('overlays=i2c1 otg-host eeprom24\n', env.read_text())
            self.assertIn('user_overlays=arbitrary  name  \n', env.read_text())
            self.assertEqual(list(user.iterdir()), [user / 'rk3308-eeprom24.dtbo'])

    def test_otp_preview_generation_and_no_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            otp = root / 'nvmem'
            otp.write_bytes(bytes(20) + bytes.fromhex('0235221703'))
            eeprom = root / 'eeprom'
            eeprom.write_bytes(b'\xff' * 256)
            common = ['--otp', str(otp), '--eeprom', str(eeprom), '--json']
            before = eeprom.read_bytes()
            preview = self.run_cli('mac', 'preview', *common)
            self.assertEqual(preview.returncode, 0, preview.stderr)
            plan = json.loads(preview.stdout)
            self.assertEqual(plan['otp_id'], '0235221703')
            self.assertEqual(plan['generated']['macs'][0], '02:98:1C:8E:55:E0')
            output = root / 'instance.json'
            denied = self.run_cli('mac', 'generate', '--output', str(output), *common)
            self.assertNotEqual(denied.returncode, 0)
            self.assertFalse(output.exists())
            first = self.run_cli('mac', 'generate', '--yes', '--output', str(output), *common)
            self.assertEqual(first.returncode, 0, first.stderr)
            document = output.read_text()
            second = self.run_cli('mac', 'generate', '--yes', *common)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(json.loads(first.stdout)['configuration'], json.loads(second.stdout)['configuration'])
            self.assertEqual(eeprom.read_bytes(), before)
            otp.write_bytes(bytes(25))
            failed = self.run_cli('mac', 'generate', '--yes', '--output', str(output), *common)
            self.assertEqual(failed.returncode, 1)
            self.assertEqual(failed.stdout, '')
            self.assertIn('MAC generation failed: invalid or unavailable RK3308 OTP ID', failed.stderr)
            self.assertEqual(output.read_text(), document)
            self.assertEqual(eeprom.read_bytes(), before)

    def test_invalid_eeprom_has_error_and_no_stdout(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'bad'
            path.write_bytes(b'\xff' * 256)
            result = self.run_cli('eeprom', 'show', '--eeprom', str(path), '--json')
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout, '')
            self.assertIn('CRC mismatch', result.stderr)

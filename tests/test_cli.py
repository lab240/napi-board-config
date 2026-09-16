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
            eeprom = Path(directory) / 'eeprom'
            eeprom.write_bytes(b'\xff' * 256)
            denied = self.run_cli('mac', 'generate', '--output', config)
            self.assertNotEqual(denied.returncode, 0)
            self.assertFalse(Path(config).exists())
            generated = self.run_cli('mac', 'generate', '--profile', '6', '1', '--output', config, '--yes', '--json')
            self.assertEqual(generated.returncode, 0, generated.stderr)
            data = json.loads(generated.stdout)
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
                result = self.run_cli(group, action, '--config', config, '--json')
                self.assertEqual(result.returncode, 0, result.stderr)
                json.loads(result.stdout)

    def test_invalid_eeprom_has_error_and_no_stdout(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'bad'
            path.write_bytes(b'\xff' * 256)
            result = self.run_cli('eeprom', 'show', '--eeprom', str(path), '--json')
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout, '')
            self.assertIn('CRC mismatch', result.stderr)

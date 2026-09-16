import tempfile
import unittest
from pathlib import Path
from napi_config.hardware.linux import LinuxProbe


class ProbeTests(unittest.TestCase):
    def test_sysfs_and_device_tree_observations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            eeprom = root / 'bus/i2c/devices/3-0050/eeprom'
            probe = LinuxProbe(eeprom, root)
            absent = probe.detect()
            self.assertEqual(absent['bus'], 3)
            self.assertFalse(absent['adapter_present'])
            (root / 'class/i2c-adapter/i2c-3').mkdir(parents=True)
            eeprom.parent.mkdir(parents=True)
            eeprom.write_bytes(b'not a valid EEPROM image')
            dt = root / 'firmware/devicetree/base'
            dt.mkdir(parents=True)
            (dt / 'compatible').write_bytes(b'napilab,fcu3308\0rockchip,rk3308\0')
            present = probe.detect()
            self.assertTrue(present['adapter_present'])
            self.assertTrue(present['device_present'])
            self.assertTrue(present['eeprom_node_present'])
            self.assertEqual(present['compatible'], ['napilab,fcu3308', 'rockchip,rk3308'])
            self.assertNotIn('healthy', present)

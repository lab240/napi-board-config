import re
from pathlib import Path


class LinuxProbe:
    def __init__(self, eeprom_path, sysfs_root='/sys'):
        self.eeprom_path = Path(eeprom_path)
        self.root = Path(sysfs_root)

    def detect(self):
        match = re.search(r'/(\d+)-([0-9a-fA-F]{4})/eeprom$', str(self.eeprom_path))
        bus, address = match.groups() if match else ('1', '0050')
        adapter = any(p.exists() for p in [self.root / f'class/i2c-adapter/i2c-{bus}', self.root / f'bus/i2c/devices/i2c-{bus}'])
        device = (self.root / f'bus/i2c/devices/{bus}-{address}').exists()
        dt = self.root / 'firmware/devicetree/base'
        compatible = dt / 'compatible'
        return {'bus': int(bus), 'address': address, 'adapter_present': adapter,
                'device_present': device, 'eeprom_node_present': self.eeprom_path.exists(),
                'device_tree_present': dt.exists(),
                'compatible': compatible.read_bytes().rstrip(b'\0').decode(errors='replace').split('\0') if compatible.exists() else []}

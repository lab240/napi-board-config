import os
from pathlib import Path


class LinuxEeprom:
    def __init__(self, path):
        self.path = Path(path)

    def read(self):
        return self.path.read_bytes()

    def availability(self, write=False):
        if not self.path.exists():
            return {'enabled': False, 'reason': f'EEPROM device not found: {self.path}'}
        mode = os.R_OK | (os.W_OK if write else 0)
        if not os.access(self.path, mode):
            return {'enabled': False, 'reason': f'EEPROM access denied: {self.path}'}
        return {'enabled': True, 'reason': ''}

    def write(self, data):
        with self.path.open('r+b', buffering=0) as stream:
            stream.seek(0)
            written = stream.write(data)
            if written != len(data):
                raise OSError('Incomplete EEPROM write')
            os.fsync(stream.fileno())

import os
from pathlib import Path


class LinuxEeprom:
    def __init__(self, path):
        self.path = Path(path)

    def read(self):
        return self.path.read_bytes()

    def write(self, data):
        with self.path.open('r+b', buffering=0) as stream:
            stream.seek(0)
            written = stream.write(data)
            if written != len(data):
                raise OSError('Incomplete EEPROM write')
            os.fsync(stream.fileno())

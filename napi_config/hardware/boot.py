import os
import shutil
import subprocess
import time
from pathlib import Path

ENV_TARGETS = {'armbianEnv': '/boot/armbianEnv.txt', 'uEnv': '/boot/uEnv.txt'}


class LinuxBootFiles:
    def read(self, target):
        path = Path(ENV_TARGETS[target])
        return path.read_text(encoding='utf-8') if path.exists() else ''

    def write(self, target, content):
        path = Path(ENV_TARGETS[target])
        backup = path.with_name(path.name + '.bak-' + time.strftime('%Y%m%d-%H%M%S') + '-' + str(time.time_ns()))
        if path.exists():
            shutil.copy2(path, backup)
        path.write_text(content, encoding='utf-8')
        os.sync()
        if path.read_text(encoding='utf-8') != content:
            raise OSError('Boot configuration read-back mismatch')
        return str(backup)

    def reboot(self):
        subprocess.run(['reboot'], check=True)

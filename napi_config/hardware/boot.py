import os
import re
import shutil
import subprocess
import time
from pathlib import Path


class LinuxBootFiles:
    def __init__(self, boot_dir='/boot'):
        self.root = Path(boot_dir)

    def _script(self):
        # boot.scr is the deployed script; boot.cmd may be an outdated source.
        for name in ('boot.scr', 'boot.cmd'):
            path = self.root / name
            if path.exists():
                text = path.read_bytes().decode('utf-8', errors='ignore')
                return '\n'.join(line for line in text.splitlines() if not line.lstrip().startswith('#'))
        return ''

    def detect(self):
        candidates = [name for name in ('armbianEnv.txt', 'uEnv.txt') if (self.root / name).is_file()]
        if not candidates:
            raise ValueError('Boot configuration not found: armbianEnv.txt / uEnv.txt')
        if len(candidates) > 1:
            script = self._script()
            referenced = [name for name in candidates if re.search(r'\bload\s+[^\n]*' + re.escape(name) + r'(?:\s|;|$)', script)]
            if len(referenced) != 1:
                raise ValueError('Both armbianEnv.txt and uEnv.txt exist; active boot configuration is ambiguous')
            candidates = referenced
        return str(self.root / candidates[0])

    def read(self, target):
        return Path(target).read_text(encoding='utf-8')

    def inventory(self, target, values):
        script = self._script()
        stock_dirs, user_dirs = [], []
        for expression in re.findall(r'([^\s"\']+\.dtbo)', script):
            if '${overlay_file}' not in expression:
                continue
            directory = expression.rsplit('/', 1)[0] if '/' in expression else ''
            directory = directory.replace('${prefix}', '').replace('${fdtfile}', values.get('fdtfile', ''))
            if '$' in directory or '..' in Path(directory).parts:
                continue
            destination = user_dirs if 'user_overlays' in script[max(0, script.find(expression)-220):script.find(expression)] or 'overlay-user' in directory else stock_dirs
            destination.append(self.root / directory.lstrip('/'))
        if not stock_dirs:
            fdt_parent = Path(values.get('fdtfile', '')).parent
            stock_dirs = [self.root / 'dtb' / fdt_parent / 'overlay', self.root / 'dtb/overlay',
                          self.root / 'dtb/rockchip/overlay', self.root / 'dtb/overlays',
                          self.root / 'overlays', self.root / 'overlay']
        if not user_dirs:
            user_dirs = [self.root / 'overlay-user']
        def files(directories):
            result = {}
            for directory in directories:
                if directory.is_dir():
                    for path in sorted(directory.glob('*.dtbo')):
                        result.setdefault(path.stem, str(path))
            return result
        return {'overlay_files': files(stock_dirs), 'user_overlay_files': files(user_dirs),
                'overlay_directories': [str(p) for p in dict.fromkeys(stock_dirs)],
                'user_overlay_directories': [str(p) for p in dict.fromkeys(user_dirs)]}

    def write(self, target, content):
        path = Path(target)
        if str(path) != self.detect():
            raise ValueError('Active boot configuration changed')
        backup = path.with_name(path.name + '.bak-' + str(time.time_ns()))
        shutil.copy2(path, backup)
        with path.open('w', encoding='utf-8') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.sync()
        if path.read_text(encoding='utf-8') != content:
            raise OSError('Boot configuration read-back mismatch')
        return str(backup)

    def reboot(self):
        subprocess.run(['reboot'], check=True)

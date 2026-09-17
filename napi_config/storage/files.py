import json
import shutil
import time
import urllib.request
from pathlib import Path
import yaml


def parse_yaml(content):
    try:
        return yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise ValueError(f'Invalid YAML: {exc}') from exc


def backup(path):
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + '.bak-' + str(time.time_ns())))


def atomic_save(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    backup(path)
    import tempfile
    import os
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, prefix=path.name+'.', delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class YamlProfiles:
    def __init__(self, path):
        self.path = Path(path)

    def load(self):
        if not self.path.exists():
            return {'version': 2, 'boards': []}
        data = parse_yaml(self.path.read_text(encoding='utf-8'))
        return {'version': 2, 'boards': []} if data is None else data

    def save(self, data):
        atomic_save(self.path, yaml.safe_dump(data, allow_unicode=True, sort_keys=False))


class JsonConfigurations:
    def __init__(self, backup_dir):
        self.backup_dir = Path(backup_dir)

    def backup_eeprom(self, data):
        import os
        import tempfile
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='wb', dir=self.backup_dir,
                                         prefix='eeprom-', suffix='.bin', delete=False) as stream:
            path = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if path.read_bytes() != data:
            raise OSError('EEPROM backup verification failed')
        return str(path)

    def load(self, path):
        return json.loads(Path(path).read_text(encoding='utf-8'))

    def save(self, path, data):
        atomic_save(path, json.dumps(data, ensure_ascii=False, indent=2) + '\n')


def load_platforms(path):
    data = parse_yaml(Path(path).read_text(encoding='utf-8'))
    if not isinstance(data, dict) or not isinstance(data.get('platforms'), dict):
        raise ValueError('Invalid platforms.yaml')
    return data['platforms']


def load_settings(path):
    return parse_yaml(Path(path).read_text(encoding='utf-8'))


class ProfileDownload:
    def fetch(self):
        request = urllib.request.Request('https://raw.githubusercontent.com/napilab/napi-boards/main/yaml-config/boards.yaml', headers={'User-Agent': 'napi-config'})
        with urllib.request.urlopen(request, timeout=15) as response:
            return parse_yaml(response.read().decode('utf-8'))

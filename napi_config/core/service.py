from dataclasses import replace
from .models import BoardConfig, IF_BY_KEY, RTC_CHOICES
from .codec import encode_config, decode_config, overlays_for, platform_conflicts, format_mac
from .configuration import validate_configuration, generate_macs, updated, from_document, to_document, default_date
from .profiles import profile_to_config, config_to_profile
from .boot import patch_boot, boot_values, resolve_overlay
from .ports import EepromDevice, ProfileRepository, BootFiles, ConfigurationFiles, HardwareProbe, ProfileSource


def require_confirmation(confirmed):
    if confirmed is not True:
        raise ValueError('Explicit confirmation is required (--yes)')


class BoardService:
    def __init__(self, catalog, eeprom: EepromDevice, profiles: ProfileRepository,
                 boot: BootFiles, configurations: ConfigurationFiles,
                 probe: HardwareProbe, download: ProfileSource):
        self.catalog = catalog
        self.eeprom = eeprom
        self.profiles = profiles
        self.boot = boot
        self.configurations = configurations
        self.probe = probe
        self.download = download

    def defaults(self):
        return BoardConfig()

    def validate(self, cfg):
        validate_configuration(cfg, self.catalog)

    def document(self, cfg):
        return to_document(cfg)

    def load_configuration(self, path):
        cfg = from_document(self.configurations.load(path))
        self.validate(cfg)
        return cfg

    def save_configuration(self, path, cfg, confirmed=False):
        require_confirmation(confirmed)
        self.validate(cfg)
        self.configurations.save(path, self.document(cfg))

    def read_eeprom(self):
        self.require_eeprom()
        cfg = decode_config(self.eeprom.read(), self.catalog)
        self.validate(cfg)
        return cfg

    def write_eeprom(self, cfg, confirmed=False):
        require_confirmation(confirmed)
        self.require_eeprom(write=True)
        self.validate(cfg)
        encoded = encode_config(cfg, self.catalog)
        self.eeprom.write(encoded)
        actual = self.eeprom.read()[:len(encoded)]
        if actual != encoded:
            raise OSError('EEPROM read-back mismatch')
        decode_config(actual, self.catalog)

    def generate_macs(self, cfg, confirmed=False):
        require_confirmation(confirmed)
        candidate = replace(cfg, macs=generate_macs(), enabled=set(cfg.enabled))
        self.validate(candidate)
        return candidate

    def mac_strings(self, cfg):
        return [format_mac(mac) for mac in cfg.macs]

    def platform_names(self):
        return self.catalog.names()

    def blockers(self, cfg, key):
        return [other for other in IF_BY_KEY if other in cfg.enabled and
                (other in platform_conflicts(cfg, key, self.catalog) or key in platform_conflicts(cfg, other, self.catalog))]

    def toggle_interface(self, cfg, key):
        if key not in IF_BY_KEY:
            raise ValueError(f'Unknown interface: {key}')
        if key == 'i2c1':
            raise ValueError('I2C1 is required for EEPROM and cannot be disabled')
        enabled = set(cfg.enabled)
        if key in enabled:
            enabled.remove(key)
        else:
            enabled.add(key)
        return self.update(cfg, 'enabled', enabled)

    def toggle_rtc(self, cfg):
        return self.update(cfg, 'rtc_i2c1', 'ds1338' if cfg.rtc_i2c1 == 'none' else 'none')

    def next_rtc(self, cfg):
        if cfg.rtc_i2c1 == 'none':
            raise ValueError('Enable RTC first')
        types = RTC_CHOICES[1:]
        return self.update(cfg, 'rtc_i2c1', types[(types.index(cfg.rtc_i2c1) + 1) % len(types)])

    def next_platform(self, cfg):
        names = self.platform_names()
        return self.update(cfg, 'platform', names[(names.index(cfg.platform) + 1) % len(names)])

    def update(self, cfg, key, value):
        if key not in BoardConfig.__dataclass_fields__:
            raise ValueError(f'Unknown configuration field: {key}')
        return updated(cfg, key, value, self.catalog)

    def suggested_date(self, cfg):
        return cfg.mfg_date or default_date()

    def boot_info(self):
        target = self.boot.detect()
        text = self.boot.read(target)
        values = boot_values(text)
        return {'path': target, 'content': text, 'overlay_prefix': values.get('overlay_prefix', ''),
                'values': values, **self.boot.inventory(target, values)}

    def overlays(self, cfg):
        self.validate(cfg)
        boot = self.boot_info()
        p = self.catalog.get(cfg.platform)
        required = set(cfg.enabled) - {'i2c1'}
        missing = required - p.get('overlays', {}).keys()
        if missing:
            raise ValueError(f'Overlay mapping not found for: {", ".join(sorted(missing))}')
        names = [resolve_overlay(name, cfg.platform, boot['overlay_prefix'], boot['overlay_files'],
                                 p.get('overlay_aliases', {})) for name in overlays_for(cfg, self.catalog)]
        return {'platform': cfg.platform, 'boot_file': boot['path'], 'overlay_prefix': boot['overlay_prefix'],
                'overlays': names, 'user_overlays': boot['values'].get('user_overlays', '').split(),
                'warnings': self.overlay_warnings(names, boot)}

    def overlay_warnings(self, names, info):
        warnings = []
        for name in names:
            full = info['overlay_prefix'] + '-' + name if info['overlay_prefix'] else name
            if full not in info['overlay_files']:
                warnings.append(f'{full}.dtbo not found in standard overlay directories; a user overlay may provide this hardware')
        return warnings

    def initial_configuration(self):
        try:
            return self.read_eeprom(), 'EEPROM configuration loaded'
        except (ValueError, TypeError, OSError, KeyError) as exc:
            return self.defaults(), f'Defaults loaded; EEPROM: {exc}'

    def eeprom_status(self, write=False):
        return self.eeprom.availability(write=write)

    def require_eeprom(self, write=False):
        status = self.eeprom_status(write)
        if not status['enabled']:
            raise ValueError(status['reason'])

    def action_status(self, cfg, action):
        if action in ('read', 'write_eeprom'):
            return self.eeprom_status(write=action == 'write_eeprom')
        if action not in ('enable_i2c1_eeprom', 'view_boot', 'write_env'):
            return {'enabled': True, 'reason': ''}
        try:
            info = self.boot_info()
            if action == 'enable_i2c1_eeprom':
                overlay = self.catalog.get(cfg.platform).get('user_overlays', {}).get('eeprom')
                if not overlay:
                    raise ValueError(f'EEPROM overlay is not defined for {cfg.platform}')
                if overlay not in info['user_overlay_files']:
                    raise ValueError(f'{overlay}.dtbo not found')
            return {'enabled': True, 'reason': ''}
        except (ValueError, OSError) as exc:
            return {'enabled': False, 'reason': str(exc)}

    def detect(self):
        return self.probe.detect()

    def validate_profiles(self, data):
        if not isinstance(data, dict) or not isinstance(data.get('boards'), list):
            raise ValueError('Invalid board profile document')
        seen = set()
        for profile in data['boards']:
            if not isinstance(profile, dict) or not all(key in profile for key in ('id', 'rev', 'name')):
                raise ValueError('Profile requires id, rev and name')
            if not isinstance(profile['name'], str):
                raise ValueError('Profile name must be a string')
            interfaces = profile.get('interfaces', {})
            if not isinstance(interfaces, (dict, list)):
                raise ValueError('Profile interfaces must be an object or list')
            unknown = set(interfaces) - (set(IF_BY_KEY) | {'rtc'})
            if unknown:
                raise ValueError(f'Unknown profile interfaces: {sorted(unknown)}')
            i2c = profile.get('i2c', {})
            if not isinstance(i2c, dict) or not isinstance(i2c.get('i2c1', {}), dict):
                raise ValueError('Invalid I2C profile configuration')
            rtc = str(i2c.get('i2c1', {}).get('rtc', 'none')).lower()
            if rtc not in RTC_CHOICES:
                raise ValueError(f'Unsupported RTC type: {rtc}')
            forbidden = {'serial_number', 'mfg_date', 'macs'} & profile.keys()
            if forbidden:
                raise ValueError('Instance data is not allowed in board profiles')
            cfg = profile_to_config(profile)
            self.validate(cfg)
            identity = (cfg.product_id, cfg.product_rev)
            if identity in seen:
                raise ValueError('Duplicate board profile ID/revision')
            seen.add(identity)
        return data

    def list_profiles(self):
        return self.validate_profiles(self.profiles.load())['boards']

    def apply_profile(self, cfg, profile, confirmed=False):
        require_confirmation(confirmed)
        self.validate_profiles({'boards': [profile]})
        candidate = profile_to_config(profile)
        candidate.serial_number, candidate.mfg_date, candidate.macs = cfg.serial_number, cfg.mfg_date, list(cfg.macs)
        self.validate(candidate)
        return candidate

    def save_profile(self, cfg, confirmed=False):
        require_confirmation(confirmed)
        self.validate(cfg)
        data = self.validate_profiles(self.profiles.load())
        item = config_to_profile(cfg)
        boards = data['boards']
        replaced = any((b['id'], b['rev']) == (item['id'], item['rev']) for b in boards)
        data['boards'] = [b for b in boards if (b['id'], b['rev']) != (item['id'], item['rev'])] + [item]
        data['boards'].sort(key=lambda b: (b['id'], b['rev']))
        data['version'] = 2
        self.profiles.save(data)
        return replaced

    def delete_profile(self, profile, confirmed=False):
        require_confirmation(confirmed)
        data = self.validate_profiles(self.profiles.load())
        data['boards'] = [b for b in data['boards'] if (b['id'], b['rev']) != (profile['id'], profile['rev'])]
        self.profiles.save(data)

    def download_profiles(self):
        return self.validate_profiles(self.download.fetch())

    def replace_profiles(self, data, confirmed=False):
        require_confirmation(confirmed)
        self.profiles.save(self.validate_profiles(data))

    def boot_plan(self, cfg, ensure_eeprom=False):
        self.validate(cfg)
        info = self.boot_info()
        old = info['content']
        user_overlay = None
        if ensure_eeprom:
            status = self.action_status(cfg, 'enable_i2c1_eeprom')
            if not status['enabled']:
                raise ValueError(status['reason'])
            p = self.catalog.get(cfg.platform)
            user_overlay = p['user_overlays']['eeprom']
            variants = [p['overlays']['i2c1']] + [p['overlays'][f'rtc_{rtc}'] for rtc in RTC_CHOICES[1:] if f'rtc_{rtc}' in p['overlays']]
            names = old_names = info['values'].get('overlays', '').split()
            expected = set(variants) if info['overlay_prefix'] else {cfg.platform + '-' + v for v in variants}
            if not any(name in expected for name in old_names):
                chosen = resolve_overlay(variants[0], cfg.platform, info['overlay_prefix'], info['overlay_files'])
                names = [chosen] + old_names
        else:
            names = self.overlays(cfg)['overlays']
        new = patch_boot(old, names, user_overlay)
        return {'target': info['path'], 'before': old, 'after': new,
                'overlay_prefix': info['overlay_prefix'], 'changed': old != new,
                'current_overlay_string': 'overlays=' + info['values'].get('overlays', ''),
                'proposed_overlay_string': 'overlays=' + ' '.join(names),
                'warnings': self.overlay_warnings(names, info), 'eeprom_overlay': user_overlay}

    def apply_boot_plan(self, plan, confirmed=False):
        require_confirmation(confirmed)
        if self.boot.detect() != plan['target'] or self.boot.read(plan['target']) != plan['before']:
            raise ValueError('Boot configuration changed since preview')
        info = self.boot_info()
        values = boot_values(plan['after'])
        if values.get('overlay_prefix', '') != info['overlay_prefix']:
            raise ValueError('Boot overlay prefix changed since preview')
        if plan.get('eeprom_overlay') and plan['eeprom_overlay'] not in info['user_overlay_files']:
            raise ValueError(f"{plan['eeprom_overlay']}.dtbo not found")
        if not plan['changed']:
            return None
        return self.boot.write(plan['target'], plan['after'])

    def reboot(self, confirmed=False):
        require_confirmation(confirmed)
        self.boot.reboot()

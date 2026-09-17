from dataclasses import replace
import re
from .models import (BoardConfig, IF_BY_KEY, RTC_CHOICES, EEPROM_HEADER,
                     EEPROM_SIZE, EEPROM_MAC_COUNT_OFFSET, EEPROM_MAC_OFFSET, EEPROM_CRC_OFFSET)
from .codec import encode_config, decode_config, overlays_for, platform_conflicts, format_mac, replace_eeprom_macs
from .configuration import validate_configuration, updated, from_document, to_document, default_date
from .mac import generate_macs, validate_otp, MAC_SOURCE, MAC_SLOT_LABELS, MacPolicy, mac_assignments, OTP_ERROR
from .profiles import profile_to_config, config_to_profile
from .boot import patch_boot, boot_values, resolve_overlay
from .ports import EepromDevice, ProfileRepository, BootFiles, ConfigurationFiles, HardwareProbe, ProfileSource, OtpSource


def require_confirmation(confirmed):
    if confirmed is not True:
        raise ValueError('Explicit confirmation is required (--yes)')


class BoardService:
    def __init__(self, catalog, eeprom: EepromDevice, profiles: ProfileRepository,
                 boot: BootFiles, configurations: ConfigurationFiles,
                 probe: HardwareProbe, download: ProfileSource, otp: OtpSource, mac_policy=None):
        self.catalog = catalog
        self.eeprom = eeprom
        self.profiles = profiles
        self.boot = boot
        self.configurations = configurations
        self.probe = probe
        self.download = download
        self.otp = otp
        self.mac_policy = mac_policy if mac_policy is not None else MacPolicy()

    def defaults(self):
        return BoardConfig()

    def validate(self, cfg):
        validate_configuration(cfg, self.catalog)

    def document(self, cfg):
        return to_document(cfg)

    def load_configuration(self, path):
        return self.configuration_from_document(self.configurations.load(path))

    def configuration_from_document(self, data):
        cfg = from_document(data)
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
        if self.eeprom.read()[:len(encoded)] == encoded:
            return False
        self.eeprom.write(encoded)
        actual = self.eeprom.read()[:len(encoded)]
        if actual != encoded:
            raise OSError('EEPROM read-back mismatch')
        decode_config(actual, self.catalog)
        return True

    def generate_macs(self, cfg, confirmed=False):
        require_confirmation(confirmed)
        return self.apply_mac_plan(cfg, self.mac_plan(cfg), confirmed=True)

    def mac_plan(self, cfg):
        self.validate(cfg)
        if cfg.platform != 'rk3308':
            raise ValueError(OTP_ERROR)
        try:
            otp = validate_otp(self.otp.read_id())
        except (OSError, ValueError) as exc:
            raise ValueError(OTP_ERROR) from exc
        candidate = replace(cfg, macs=generate_macs(otp), enabled=set(cfg.enabled))
        self.validate(candidate)
        try:
            stored = self.document(self.read_eeprom())
            stored_error = ''
        except (ValueError, OSError) as exc:
            stored = None
            stored_error = str(exc)
        return {'otp_id': otp.hex(), 'source': MAC_SOURCE,
                'current': self.document(cfg), 'generated': self.document(candidate),
                'eeprom_current': stored, 'eeprom_error': stored_error,
                'already_matches': cfg.macs == candidate.macs,
                'slots': [{'slot': i + 1, 'purpose': purpose} for i, purpose in enumerate(MAC_SLOT_LABELS)]}

    def apply_mac_plan(self, cfg, plan, confirmed=False):
        require_confirmation(confirmed)
        if self.document(cfg) != plan['current']:
            raise ValueError('Configuration changed since MAC preview')
        candidate = from_document(plan['generated'])
        self.validate(candidate)
        return candidate

    def mac_assignments(self, cfg):
        self.validate(cfg)
        return {'set_native_eth_mac': self.mac_policy.set_native_eth_mac,
                'assignments': mac_assignments(cfg.macs, self.mac_policy)}

    def mac_rows(self, cfg):
        return [f'MAC{i + 1}  {MAC_SLOT_LABELS[i]:<18} {format_mac(mac)}'
                for i, mac in enumerate(cfg.macs)]

    def eeprom_rows(self, data):
        cfg = decode_config(data, self.catalog)
        self.validate(cfg)
        fields = EEPROM_HEADER.unpack(data[:EEPROM_HEADER.size])
        rows = [f'Format version: {fields[1]}', f'Platform: {cfg.platform} (ID {fields[2]})',
                f'Product ID: {cfg.product_id}', f'Product revision: {cfg.product_rev}',
                f'Board name: {cfg.board_name}', f'Serial number: {cfg.serial_number}',
                f'Manufacturing date: {cfg.mfg_date or "not set"}',
                f'Interface mask: 0x{fields[5]:08X}']
        rows += [f'{definition.title}: {"enabled" if key in cfg.enabled else "disabled"}'
                 for key, definition in IF_BY_KEY.items()]
        rows += [f'RTC on I2C1: {cfg.rtc_i2c1}', f'MAC count: {len(cfg.macs)}']
        rows += self.mac_rows(cfg)
        rows += [f'CRC32: 0x{int.from_bytes(data[EEPROM_CRC_OFFSET:EEPROM_SIZE], "little"):08X}']
        return rows

    def eeprom_preview(self, cfg):
        self.validate(cfg)
        try:
            self.require_eeprom()
            existing = '\n'.join(self.eeprom_rows(self.eeprom.read()))
        except ValueError as exc:
            existing = 'No valid EEPROM configuration: ' + str(exc)
        return ('FULL EEPROM CONFIGURATION WRITE\n\nCurrent EEPROM:\n' + existing
                + '\n\nProposed EEPROM:\n' + '\n'.join(self.eeprom_rows(encode_config(cfg, self.catalog)))
                + '\n\nWrites all Format v2 fields shown above. Comment and boot settings are not stored in EEPROM.')

    def mac_eeprom_plan(self, macs):
        self.require_eeprom(write=True)
        before = self.eeprom.read()[:EEPROM_SIZE]
        try:
            current = decode_config(before, self.catalog)
            self.validate(current)
        except ValueError as exc:
            raise ValueError('MAC-only write requires a valid EEPROM configuration. Use Write EEPROM to initialize all fields.') from exc
        candidate = replace(current, macs=list(macs), enabled=set(current.enabled))
        self.validate(candidate)
        after = replace_eeprom_macs(before, macs, self.catalog)
        return {'before': before, 'after': after, 'changed': before != after,
                'current': self.document(current), 'proposed': self.document(candidate)}

    def mac_eeprom_preview(self, plan):
        current = self.configuration_from_document(plan['current'])
        proposed = self.configuration_from_document(plan['proposed'])
        return ('WRITE MACs TO EEPROM\n\nCurrent:\n' + ('\n'.join(self.mac_rows(current)) or 'No MAC addresses')
                + '\n\nProposed:\n' + '\n'.join(self.mac_rows(proposed))
                + '\n\nOnly MAC fields, mac_count and CRC32 are written. All other EEPROM bytes are preserved.')

    def apply_mac_eeprom_plan(self, plan, confirmed=False):
        require_confirmation(confirmed)
        self.require_eeprom(write=True)
        if self.eeprom.read()[:EEPROM_SIZE] != plan['before']:
            raise ValueError('EEPROM changed since MAC write preview')
        cfg = decode_config(plan['after'], self.catalog)
        self.validate(cfg)
        if replace_eeprom_macs(plan['before'], cfg.macs, self.catalog) != plan['after']:
            raise ValueError('MAC-only plan modifies other EEPROM fields')
        if plan['before'] == plan['after']:
            return False
        spans = [(EEPROM_MAC_COUNT_OFFSET, EEPROM_MAC_COUNT_OFFSET + 1),
                 (EEPROM_MAC_OFFSET, EEPROM_CRC_OFFSET), (EEPROM_CRC_OFFSET, EEPROM_SIZE)]
        changes = [(start, plan['after'][start:end]) for start, end in spans
                   if plan['before'][start:end] != plan['after'][start:end]]
        self.eeprom.write_ranges(changes)
        if self.eeprom.read()[:EEPROM_SIZE] != plan['after']:
            raise OSError('EEPROM MAC read-back mismatch')
        return True

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

    @staticmethod
    def is_eeprom_overlay(name):
        # Names identify candidates only. User must verify the actual chip/address.
        return bool(re.search(r'(?:eeprom|(?:^|[-_])(?:at)?24c?[0-9]+)', name.lower()))

    def eeprom_overlay_candidates(self, platform, info=None):
        self.catalog.get(platform)
        info = info if info is not None else self.boot_info()
        prefix = info['overlay_prefix']
        candidates = []
        for full, path in sorted(info['overlay_files'].items()):
            if prefix and not full.startswith(prefix + '-'):
                continue
            if not prefix and full.startswith(('rk', 'napi-rk')) and not full.startswith((platform + '-', 'napi-' + platform + '-')):
                continue
            name = full[len(prefix)+1:] if prefix else full
            if self.is_eeprom_overlay(name):
                candidates.append({'file': full + '.dtbo', 'name': name, 'path': path})
        return candidates

    def eeprom_setup(self, platform):
        available = self.eeprom_status()
        info = self.boot_info()
        candidates = self.eeprom_overlay_candidates(platform, info)
        if available['enabled']:
            message = 'EEPROM is already accessible; an additional overlay is not required'
        elif candidates:
            message = 'Select a standard overlay only after verifying EEPROM model, I2C bus and address'
        else:
            message = ('No standard EEPROM overlay found. Install and configure a suitable overlay manually '
                       'using user_overlays. User overlay files and user_overlays are not modified.')
        return {'available': available['enabled'], 'reason': available['reason'],
                'boot_file': info['path'], 'candidates': candidates, 'message': message}

    def action_status(self, cfg, action):
        if action in ('read', 'write_eeprom'):
            return self.eeprom_status(write=action == 'write_eeprom')
        if action not in ('enable_i2c1_eeprom', 'view_boot', 'write_env'):
            return {'enabled': True, 'reason': ''}
        try:
            info = self.boot_info()
            if action == 'enable_i2c1_eeprom':
                setup = self.eeprom_setup(cfg.platform)
                if not setup['available'] and not setup['candidates']:
                    raise ValueError(setup['message'])
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

    def boot_plan(self, cfg, eeprom_overlay=None):
        self.validate(cfg)
        info = self.boot_info()
        old = info['content']
        if eeprom_overlay is not None:
            candidates = self.eeprom_overlay_candidates(cfg.platform, info)
            if eeprom_overlay not in {candidate['name'] for candidate in candidates}:
                raise ValueError('Selected standard EEPROM overlay not found; configure user_overlays manually if needed')
            p = self.catalog.get(cfg.platform)
            variants = [p['overlays']['i2c1']] + [p['overlays'][f'rtc_{rtc}'] for rtc in RTC_CHOICES[1:] if f'rtc_{rtc}' in p['overlays']]
            names = info['values'].get('overlays', '').split()
            expected = set(variants) if info['overlay_prefix'] else {cfg.platform + '-' + v for v in variants}
            if not any(name in expected for name in names):
                chosen = resolve_overlay(variants[0], cfg.platform, info['overlay_prefix'], info['overlay_files'])
                names = [chosen] + names
            names = list(dict.fromkeys(names + [eeprom_overlay]))
        else:
            names = self.overlays(cfg)['overlays']
            # Keep explicitly configured standard EEPROM support when its bus is enabled.
            if 'i2c1' in cfg.enabled:
                names = list(dict.fromkeys(names + [name for name in info['values'].get('overlays', '').split()
                                                    if self.is_eeprom_overlay(name)]))
        new = patch_boot(old, names)
        return {'target': info['path'], 'before': old, 'after': new,
                'overlay_prefix': info['overlay_prefix'], 'changed': old != new,
                'current_overlay_string': 'overlays=' + info['values'].get('overlays', ''),
                'proposed_overlay_string': 'overlays=' + ' '.join(names),
                'current_user_overlay_string': 'user_overlays=' + info['values'].get('user_overlays', ''),
                'proposed_user_overlay_string': 'user_overlays=' + boot_values(new).get('user_overlays', ''),
                'warnings': self.overlay_warnings(names, info), 'eeprom_overlay': eeprom_overlay}

    def apply_boot_plan(self, plan, confirmed=False):
        require_confirmation(confirmed)
        if self.boot.detect() != plan['target'] or self.boot.read(plan['target']) != plan['before']:
            raise ValueError('Boot configuration changed since preview')
        info = self.boot_info()
        values = boot_values(plan['after'])
        if values.get('overlay_prefix', '') != info['overlay_prefix']:
            raise ValueError('Boot overlay prefix changed since preview')
        if plan.get('eeprom_overlay'):
            name = plan['eeprom_overlay']
            full = info['overlay_prefix'] + '-' + name if info['overlay_prefix'] else name
            if full not in info['overlay_files']:
                raise ValueError(f'{full}.dtbo not found')
        if not plan['changed']:
            return None
        return self.boot.write(plan['target'], plan['after'])

    def reboot(self, confirmed=False):
        require_confirmation(confirmed)
        self.boot.reboot()

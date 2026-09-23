from dataclasses import replace
import re
import struct
import zlib
from .models import (BoardConfig, IF_BY_KEY, RTC_CHOICES, EEPROM_HEADER,
                     EEPROM_MAC_COUNT_OFFSET, EEPROM_MAC_OFFSET, EEPROM_MAC_END, RTC_BITS)
from .codec import encode_config, decode_config, overlays_for, platform_conflicts, format_mac, replace_eeprom_macs, eeprom_layout
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
        existing = self.eeprom.read()
        cfg = self.eeprom_write_configuration(cfg, existing)
        encoded = encode_config(cfg, self.catalog)
        try:
            stored = decode_config(existing, self.catalog)
        except ValueError:
            stored = None
        if stored and cfg.format_version != stored.format_version:
            raise ValueError('Use explicit eeprom migrate before changing EEPROM format')
        if stored and stored.proc_id_type and (cfg.proc_id_type, cfg.proc_id) != (stored.proc_id_type, stored.proc_id):
            raise ValueError('Stored processor ID cannot be changed by full write; use explicit processor rebind')
        if cfg.proc_id_type and cfg.proc_id != validate_otp(self.otp.read_id()):
            raise ValueError('PROCESSOR MISMATCH: configuration binding does not match current SoC')
        if existing[:len(encoded)] == encoded:
            return False
        self.last_eeprom_backup = self.configurations.backup_eeprom(existing)
        if self.eeprom.read() != existing:
            raise ValueError('EEPROM changed while creating backup')
        self.eeprom.write(encoded)
        actual = self.eeprom.read()[:len(encoded)]
        if actual != encoded:
            raise OSError('EEPROM read-back mismatch')
        decode_config(actual, self.catalog)
        return True

    def eeprom_write_plan(self, cfg):
        self.require_eeprom()
        image = self.eeprom.read()
        candidate = self.eeprom_write_configuration(cfg, image)
        self.validate(candidate)
        return {'before_image': image, 'configuration': candidate,
                'comparison': self.eeprom_comparison(candidate)}

    def apply_eeprom_write_plan(self, plan, confirmed=False):
        require_confirmation(confirmed)
        if self.eeprom.read() != plan['before_image']:
            raise ValueError('EEPROM changed since full write preview')
        return self.write_eeprom(plan['configuration'], confirmed=True)

    def eeprom_write_configuration(self, cfg, image=None):
        if image is None:
            try:
                image = self.eeprom.read()
            except OSError:
                return cfg
        if len(image) == 256 and image in (bytes(256), b'\xff' * 256):
            cfg = replace(cfg, format_version=3)
        if cfg.format_version == 3 and not cfg.proc_id_type:
            try:
                stored = decode_config(image, self.catalog)
            except ValueError:
                stored = None
            if stored and stored.proc_id_type:
                return replace(cfg, proc_id_type=stored.proc_id_type, proc_id=stored.proc_id)
            cfg = replace(cfg, proc_id_type=1, proc_id=validate_otp(self.otp.read_id()))
        return cfg

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
        if cfg.proc_id_type and cfg.proc_id != otp:
            raise ValueError('PROCESSOR MISMATCH: rebind explicitly before generating MAC addresses')
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

    def eeprom_fields(self, data):
        cfg = decode_config(data, self.catalog)
        self.validate(cfg)
        fields = EEPROM_HEADER.unpack(data[:EEPROM_HEADER.size])
        size, crc_offset = eeprom_layout(data)
        rows = [('Format version', str(fields[1])), ('Platform', f'{cfg.platform} (ID {fields[2]})'),
                ('Product ID', str(cfg.product_id)), ('Product revision', str(cfg.product_rev)),
                ('Board name', cfg.board_name), ('Serial number', str(cfg.serial_number)),
                ('Manufacturing date', cfg.mfg_date or 'not set'),
                ('Interface mask', f'0x{fields[5]:08X}')]
        rows += [(definition.title, 'enabled' if key in cfg.enabled else 'disabled')
                 for key, definition in IF_BY_KEY.items()]
        rows += [('RTC on I2C1', cfg.rtc_i2c1), ('MAC count', str(len(cfg.macs)))]
        rows += [(f'MAC{i + 1}  {purpose}', format_mac(cfg.macs[i]) if i < len(cfg.macs) else 'not set')
                 for i, purpose in enumerate(MAC_SLOT_LABELS)]
        if cfg.format_version == 3:
            rows += [('Processor ID type', str(cfg.proc_id_type)), ('Processor ID length', str(len(cfg.proc_id))),
                     ('Processor ID', cfg.proc_id.hex() or 'not bound')]
        rows += [('CRC32', f'0x{int.from_bytes(data[crc_offset:size], "little"):08X}')]
        return rows

    def eeprom_rows(self, data):
        return [f'{label:<24} {value}' if label.startswith('MAC') and label != 'MAC count'
                else f'{label}: {value}' for label, value in self.eeprom_fields(data)]

    def eeprom_view(self):
        self.require_eeprom()
        image = self.eeprom.read()
        try:
            rows = self.eeprom_rows(image)
        except ValueError as exc:
            empty = len(image) == 256 and image in (bytes(256), b'\xff' * 256)
            status = 'EEPROM empty / uninitialized' if empty else 'EEPROM invalid: ' + str(exc)
            dump = [f'{offset:02X}: ' + image[offset:offset + 16].hex(' ')
                    for offset in range(0, len(image), 16)]
            return status + '\nFields and CRC are not validated.\n\nRaw EEPROM:\n' + '\n'.join(dump)
        return 'CURRENT EEPROM\nCRC32: valid\n\n' + '\n'.join(rows)

    def eeprom_comparison(self, cfg):
        self.validate(cfg)
        error = ''
        try:
            self.require_eeprom()
            current = dict(self.eeprom_fields(self.eeprom.read()))
        except ValueError as exc:
            current = {}
            error = 'No valid EEPROM configuration: ' + str(exc)
        proposed = self.eeprom_fields(encode_config(self.eeprom_write_configuration(cfg), self.catalog))
        return self.configuration_comparison(current, proposed, 'FULL EEPROM CONFIGURATION WRITE',
                                            'Writes all EEPROM fields. Comment and boot settings are not stored in EEPROM.', error)

    def configuration_comparison(self, current, proposed, title, note, error=''):
        return {'title': title, 'note': note, 'error': error,
                'rows': [{'field': label, 'current': current.get(label, 'not available'), 'proposed': value,
                          'changed': current.get(label) != value} for label, value in proposed]}

    def mac_eeprom_comparison(self, plan):
        current = dict(self.eeprom_fields(plan['before']))
        proposed = [(label, value) for label, value in self.eeprom_fields(plan['after'])
                    if label.startswith('MAC') or label == 'CRC32']
        return self.configuration_comparison(current, proposed, 'WRITE MACs TO EEPROM',
                                            'Only MAC fields, mac_count and CRC32 are written; other EEPROM bytes are preserved.')

    def eeprom_preview(self, cfg):
        self.validate(cfg)
        try:
            self.require_eeprom()
            existing = '\n'.join(self.eeprom_rows(self.eeprom.read()))
        except ValueError as exc:
            existing = 'No valid EEPROM configuration: ' + str(exc)
        return ('FULL EEPROM CONFIGURATION WRITE\n\nCurrent EEPROM:\n' + existing
                + '\n\nProposed EEPROM:\n' + '\n'.join(self.eeprom_rows(encode_config(self.eeprom_write_configuration(cfg), self.catalog)))
                + '\n\nWrites all EEPROM fields shown above. Comment and boot settings are not stored in EEPROM.')

    def mac_eeprom_plan(self, macs):
        self.require_eeprom(write=True)
        image = self.eeprom.read()
        try:
            size, _ = eeprom_layout(image)
            before = image[:size]
            current = decode_config(before, self.catalog)
            self.validate(current)
        except ValueError as exc:
            raise ValueError('MAC-only write requires a valid EEPROM configuration. Use Write EEPROM to initialize all fields.') from exc
        if current.proc_id_type and validate_otp(self.otp.read_id()) != current.proc_id:
            raise ValueError('PROCESSOR MISMATCH: rebind explicitly before updating MAC addresses')
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
        size, crc_offset = eeprom_layout(plan['before'])
        if self.eeprom.read()[:size] != plan['before']:
            raise ValueError('EEPROM changed since MAC write preview')
        cfg = decode_config(plan['after'], self.catalog)
        self.validate(cfg)
        if replace_eeprom_macs(plan['before'], cfg.macs, self.catalog) != plan['after']:
            raise ValueError('MAC-only plan modifies other EEPROM fields')
        if cfg.proc_id_type and validate_otp(self.otp.read_id()) != cfg.proc_id:
            raise ValueError('PROCESSOR MISMATCH: processor changed since MAC write preview')
        if plan['before'] == plan['after']:
            return False
        spans = [(EEPROM_MAC_COUNT_OFFSET, EEPROM_MAC_COUNT_OFFSET + 1),
                 (EEPROM_MAC_OFFSET, EEPROM_MAC_END), (crc_offset, size)]
        changes = [(start, plan['after'][start:end]) for start, end in spans
                   if plan['before'][start:end] != plan['after'][start:end]]
        self.eeprom.write_ranges(changes)
        if self.eeprom.read()[:size] != plan['after']:
            raise OSError('EEPROM MAC read-back mismatch')
        return True

    def processor_status(self, cfg=None):
        if cfg is None:
            try:
                cfg = self.read_eeprom()
            except (ValueError, OSError):
                cfg = self.defaults()
        self.validate(cfg)
        try:
            current = validate_otp(self.otp.read_id())
        except (ValueError, OSError) as exc:
            return {'state': 'OTP UNAVAILABLE', 'stored_id': cfg.proc_id.hex(), 'current_id': '', 'error': str(exc)}
        state = 'NOT BOUND' if cfg.proc_id_type == 0 else 'MATCH' if cfg.proc_id == current else 'PROCESSOR MISMATCH'
        return {'state': state, 'stored_id': cfg.proc_id.hex(), 'current_id': current.hex(), 'error': ''}

    def processor_plan(self, rebind=False):
        cfg = self.read_eeprom()
        if cfg.format_version != 3:
            raise ValueError('Processor binding requires v3; reset EEPROM and write a new configuration first (or migrate v2 explicitly)')
        otp = validate_otp(self.otp.read_id())
        if cfg.proc_id_type and cfg.proc_id != otp and not rebind:
            raise ValueError('PROCESSOR MISMATCH: use explicit processor rebind')
        candidate = replace(cfg, proc_id_type=1, proc_id=otp)
        return self.instance_eeprom_plan(candidate, 'REBIND PROCESSOR' if rebind else 'BIND PROCESSOR',
                                         'Processor binding changes; serial number and stored MAC addresses are preserved.', otp,
                                         rebind=rebind)

    def migration_plan(self):
        cfg = self.read_eeprom()
        if cfg.format_version != 2:
            raise ValueError('Migration requires EEPROM format v2')
        return self.instance_eeprom_plan(replace(cfg, format_version=3), 'MIGRATE EEPROM v2 TO v3',
                                         'Format and CRC layout change; processor remains NOT BOUND. Serial and MACs are preserved.')

    def processor_write_plan(self, draft=None):
        self.require_eeprom()
        otp = validate_otp(self.otp.read_id())
        image = self.eeprom.read()
        if len(image) == 256 and (image == bytes(256) or image == b'\xff' * 256):
            candidate = replace(draft if draft is not None else self.defaults(),
                                format_version=3, proc_id_type=1, proc_id=otp)
            self.validate(candidate)
            after = encode_config(candidate, self.catalog)
            comparison = self.configuration_comparison({}, self.eeprom_fields(after),
                'INITIALIZE EEPROM AND WRITE PROCESSOR ID',
                'EEPROM is empty. Writes the FULL displayed configuration and current OTP ID; reserved bytes are preserved.')
            return {'kind': 'initialize', 'writable': True, 'changed': True,
                    'before_image': image, 'after': after, 'otp_id': otp, 'comparison': comparison}
        try:
            cfg = self.read_eeprom()
            reason = '' if cfg.format_version == 3 else 'EEPROM is v2. Reset EEPROM and write a new v3 configuration first.'
        except ValueError as exc:
            cfg = None
            reason = 'EEPROM is uninitialized or invalid. Write a new v3 configuration first. ' + str(exc)
        if reason:
            comparison = self.configuration_comparison(
                {'Processor ID': 'not stored'}, [('Processor ID', otp.hex())],
                'VIEW AND WRITE PROCESSOR ID', reason)
            return {'writable': False, 'changed': False, 'comparison': comparison, 'reason': reason}
        plan = self.processor_plan(rebind=True)
        plan['writable'] = True
        plan['comparison']['title'] = 'VIEW AND WRITE PROCESSOR ID'
        if cfg.proc_id_type and cfg.proc_id != plan['otp_id']:
            plan['comparison']['note'] = 'PROCESSOR MISMATCH. Explicit confirmation binds the replacement SoC. Serial and stored MACs are preserved.'
        return plan

    def apply_processor_write_plan(self, plan, confirmed=False):
        if plan.get('kind') != 'initialize':
            return self.apply_instance_eeprom_plan(plan, confirmed=confirmed)
        require_confirmation(confirmed)
        self.require_eeprom(write=True)
        before = plan['before_image']
        if len(before) != 256 or before not in (bytes(256), b'\xff' * 256):
            raise ValueError('Processor initialization requires empty EEPROM')
        if self.eeprom.read() != before:
            raise ValueError('EEPROM changed since processor preview')
        cfg = decode_config(plan['after'], self.catalog)
        self.validate(cfg)
        if cfg.format_version != 3 or cfg.proc_id_type != 1 or cfg.proc_id != plan['otp_id']:
            raise ValueError('Invalid processor initialization plan')
        if encode_config(cfg, self.catalog) != plan['after']:
            raise ValueError('Invalid processor initialization bytes')
        if validate_otp(self.otp.read_id()) != cfg.proc_id:
            raise ValueError('Processor changed since binding preview')
        self.last_eeprom_backup = self.configurations.backup_eeprom(before)
        if self.eeprom.read() != before:
            raise ValueError('EEPROM changed while creating backup')
        self.eeprom.write(plan['after'])
        expected = plan['after'] + before[len(plan['after']):]
        if self.eeprom.read() != expected:
            raise OSError('EEPROM initialization read-back mismatch')
        return True

    def instance_eeprom_plan(self, candidate, title, note, otp=None, rebind=False):
        self.validate(candidate)
        image = self.eeprom.read()
        size, _ = eeprom_layout(image)
        original = decode_config(image, self.catalog)
        if original.format_version == 2:
            if candidate.format_version != 3 or candidate.proc_id_type or otp is not None:
                raise ValueError('Migration must leave the processor NOT BOUND')
        elif candidate.format_version != 3 or candidate.proc_id_type != 1 or candidate.proc_id != otp:
            raise ValueError('Binding plan does not match current processor ID')
        elif original.proc_id_type and original.proc_id != otp and rebind is not True:
            raise ValueError('PROCESSOR MISMATCH: use explicit processor rebind')
        unchanged = replace(candidate, proc_id_type=original.proc_id_type,
                            proc_id=original.proc_id, format_version=original.format_version)
        if self.document(unchanged) != self.document(original):
            raise ValueError('EEPROM changed while preparing instance update')
        after = bytearray(image[:EEPROM_MAC_END])
        after[4] = 3
        if original.format_version == 2 and original.rtc_i2c1 != 'none':
            mask = int.from_bytes(after[12:16], 'little')
            mask = (mask & ~1) | (1 << RTC_BITS[original.rtc_i2c1])
            after[12:16] = mask.to_bytes(4, 'little')
        after += bytes([candidate.proc_id_type, len(candidate.proc_id)]) + candidate.proc_id.ljust(16, b'\x00')
        after += struct.pack('<I', zlib.crc32(after) & 0xffffffff)
        after = bytes(after)
        comparison = self.configuration_comparison(dict(self.eeprom_fields(image)), self.eeprom_fields(after), title, note)
        return {'before': image[:size], 'before_image': image, 'after': after, 'otp_id': otp,
                'rebind': rebind,
                'changed': image[:len(after)] != after, 'comparison': comparison}

    def apply_instance_eeprom_plan(self, plan, confirmed=False):
        require_confirmation(confirmed)
        self.require_eeprom(write=True)
        if self.eeprom.read() != plan['before_image']:
            raise ValueError('EEPROM changed since preview')
        cfg = decode_config(plan['after'], self.catalog)
        self.validate(cfg)
        # Reconstruct the permitted change from the original bytes, not caller fields.
        permitted = self.instance_eeprom_plan(cfg, '', '', plan['otp_id'], rebind=plan['rebind'])
        if permitted['after'] != plan['after']:
            raise ValueError('Instance plan modifies unrelated EEPROM bytes')
        if plan['otp_id'] is not None and validate_otp(self.otp.read_id()) != plan['otp_id']:
            raise ValueError('Processor changed since binding preview')
        if plan['otp_id'] is not None and (cfg.proc_id_type != 1 or cfg.proc_id != plan['otp_id']):
            raise ValueError('Binding plan does not match current processor ID')
        if plan['otp_id'] is None and cfg.proc_id_type:
            raise ValueError('Migration must not implicitly bind a processor')
        if not permitted['changed']:
            return False
        self.last_eeprom_backup = self.configurations.backup_eeprom(plan['before_image'])
        if self.eeprom.read() != plan['before_image']:
            raise ValueError('EEPROM changed while creating backup')
        self.eeprom.write(plan['after'])
        if self.eeprom.read()[:len(plan['after'])] != plan['after']:
            raise OSError('EEPROM read-back mismatch')
        return True

    def mac_strings(self, cfg):
        return [format_mac(mac) for mac in cfg.macs]

    def reset_processor_configuration(self, cfg):
        return replace(cfg, proc_id_type=0, proc_id=b'')

    def reset_processor_plan(self):
        self.require_eeprom()
        image = self.eeprom.read()
        cfg = decode_config(image, self.catalog)
        if cfg.format_version != 3 or len(image) != 256:
            raise ValueError('Processor ID reset requires a complete v3 EEPROM image')
        body = image[:104] + bytes(18)
        after = body + struct.pack('<I', zlib.crc32(body)) + image[126:]
        comparison = self.configuration_comparison(dict(self.eeprom_fields(image)),
            self.eeprom_fields(after), 'RESET PROCESSOR ID',
            'Only processor binding is cleared. Serial, date, MACs and board settings are preserved.')
        return {'before_image': image, 'after': after, 'changed': image != after,
                'comparison': comparison}

    def apply_reset_processor_plan(self, plan, confirmed=False):
        require_confirmation(confirmed)
        self.require_eeprom(write=True)
        current = self.reset_processor_plan()
        if current['before_image'] != plan['before_image']:
            raise ValueError('EEPROM changed since processor reset preview')
        if current['after'] != plan['after']:
            raise ValueError('Invalid processor reset plan')
        self.last_eeprom_backup = None
        if not current['changed']:
            return False
        self.last_eeprom_backup = self.configurations.backup_eeprom(current['before_image'])
        if self.eeprom.read() != current['before_image']:
            raise ValueError('EEPROM changed while creating backup')
        self.eeprom.write_ranges([(104, current['after'][104:122]), (122, current['after'][122:126])])
        if self.eeprom.read() != current['after']:
            raise OSError('Processor reset read-back mismatch')
        return True

    def reset_eeprom_plan(self):
        self.require_eeprom()
        image = self.eeprom.read()
        if len(image) != 256:
            raise ValueError('Reset requires a complete 256-byte EEPROM image')
        try:
            current = dict(self.eeprom_fields(image))
            error = ''
        except ValueError as exc:
            current = {'EEPROM content': 'invalid or uninitialized'}
            error = str(exc)
        proposed = [('EEPROM content', '256 zero bytes')]
        proposed += [(label, 'cleared') for label in current if label != 'EEPROM content']
        comparison = self.configuration_comparison(current, proposed, 'RESET EEPROM',
            'Erases ALL 256 bytes: configuration, serial, date, MACs, processor ID and reserved area. '
            'A binary backup is saved first. EEPROM becomes uninitialized.', error)
        return {'before_image': image, 'after': bytes(256), 'changed': image != bytes(256),
                'comparison': comparison}

    def apply_reset_eeprom_plan(self, plan, confirmed=False):
        require_confirmation(confirmed)
        self.require_eeprom(write=True)
        if len(plan['before_image']) != 256 or plan['after'] != bytes(256):
            raise ValueError('Invalid EEPROM reset plan')
        if self.eeprom.read() != plan['before_image']:
            raise ValueError('EEPROM changed since reset preview')
        self.last_eeprom_backup = None
        if plan['before_image'] == bytes(256):
            return False
        self.last_eeprom_backup = self.configurations.backup_eeprom(plan['before_image'])
        if self.eeprom.read() != plan['before_image']:
            raise ValueError('EEPROM changed while creating backup')
        self.eeprom.write(plan['after'])
        if self.eeprom.read() != plan['after']:
            raise OSError('EEPROM reset read-back mismatch')
        return True

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
        warnings = self.overlay_warnings(names, boot)
        names = self.existing_overlays(names, boot)
        return {'platform': cfg.platform, 'boot_file': boot['path'], 'overlay_prefix': boot['overlay_prefix'],
                'overlays': names, 'user_overlays': boot['values'].get('user_overlays', '').split(),
                'warnings': warnings}

    @staticmethod
    def existing_overlays(names, info):
        prefix = info['overlay_prefix']
        return [name for name in names
                if (prefix + '-' + name if prefix else name) in info['overlay_files']]

    def overlay_warnings(self, names, info):
        warnings = []
        for name in names:
            full = info['overlay_prefix'] + '-' + name if info['overlay_prefix'] else name
            if full not in info['overlay_files']:
                warnings.append(f'{full}.dtbo not found in standard overlay directories; not added to overlays=. Hardware may be enabled by the base DTB or a user overlay')
        return warnings

    def initial_configuration(self):
        try:
            self.require_eeprom()
            image = self.eeprom.read()
            if len(image) == 256 and image in (bytes(256), b'\xff' * 256):
                return self.defaults(), 'EEPROM empty; defaults loaded. Write a new configuration to initialize it.'
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
        return bool(re.search(r'(?:eeprom|(?:^|[-_])at24(?:$|[-_])|(?:^|[-_])(?:at)?24c?[0-9]+)', name.lower()))

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
        logical = self.catalog.get(platform).get('eeprom_overlay', 'i2c1-at24')
        full = (info['overlay_prefix'] or platform) + '-' + logical
        preferred = next((candidate for candidate in candidates if candidate['file'] == full + '.dtbo'), None)
        if available['enabled']:
            message = 'EEPROM is already accessible; an additional overlay is not required'
        elif preferred:
            message = 'Enable standard ' + preferred['file'] + ' in overlays= of ' + info['path'] + '; confirmation is required'
        elif candidates:
            message = 'Select a standard overlay only after verifying EEPROM model, I2C bus and address'
        else:
            message = ('No standard EEPROM overlay found: expected ' + full + '.dtbo. '
                       'Check the firmware standard overlay installation under /boot. '
                       'user_overlays is not modified.')
        return {'available': available['enabled'], 'reason': available['reason'],
                'boot_file': info['path'], 'candidates': candidates, 'preferred': preferred, 'message': message}

    def action_status(self, cfg, action):
        if action in ('view_eeprom', 'read', 'write_eeprom', 'view_processor', 'bind_processor', 'rebind_processor', 'migrate_eeprom',
                      'write_processor', 'reset_eeprom', 'reset_processor'):
            return self.eeprom_status(write=action not in ('view_eeprom', 'read', 'view_processor'))
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
            forbidden = {'serial_number', 'mfg_date', 'macs', 'proc_id', 'proc_id_type', 'proc_id_len'} & profile.keys()
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
        candidate.format_version = cfg.format_version
        candidate.proc_id_type, candidate.proc_id = cfg.proc_id_type, cfg.proc_id
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
        warnings = []
        if eeprom_overlay is not None:
            candidates = self.eeprom_overlay_candidates(cfg.platform, info)
            if eeprom_overlay not in {candidate['name'] for candidate in candidates}:
                raise ValueError('Selected standard EEPROM overlay not found; check the firmware standard overlays under /boot')
            p = self.catalog.get(cfg.platform)
            variants = [p['overlays']['i2c1']] + [p['overlays'][f'rtc_{rtc}'] for rtc in RTC_CHOICES[1:] if f'rtc_{rtc}' in p['overlays']]
            names = info['values'].get('overlays', '').split()
            expected = set(variants) if info['overlay_prefix'] else {cfg.platform + '-' + v for v in variants}
            if not any(name in expected for name in names):
                chosen = resolve_overlay(variants[0], cfg.platform, info['overlay_prefix'], info['overlay_files'])
                names = [chosen] + names
            names = list(dict.fromkeys(names + [eeprom_overlay]))
        else:
            resolved = self.overlays(cfg)
            names = resolved['overlays']
            warnings = resolved['warnings']
            # Keep explicitly configured standard EEPROM support when its bus is enabled.
            if 'i2c1' in cfg.enabled:
                names = list(dict.fromkeys(names + [name for name in info['values'].get('overlays', '').split()
                                                    if self.is_eeprom_overlay(name)]))
        warnings = list(dict.fromkeys(warnings + self.overlay_warnings(names, info)))
        if eeprom_overlay is None:
            names = self.existing_overlays(names, info)
        else:
            previous = info['values'].get('overlays', '').split()
            names = [name for name in names if name in previous or self.existing_overlays([name], info)]
        new = patch_boot(old, names)
        return {'target': info['path'], 'before': old, 'after': new,
                'overlay_prefix': info['overlay_prefix'], 'changed': old != new,
                'current_overlay_string': 'overlays=' + info['values'].get('overlays', ''),
                'proposed_overlay_string': 'overlays=' + ' '.join(names),
                'current_user_overlay_string': 'user_overlays=' + info['values'].get('user_overlays', ''),
                'proposed_user_overlay_string': 'user_overlays=' + boot_values(new).get('user_overlays', ''),
                'warnings': warnings, 'eeprom_overlay': eeprom_overlay}

    def boot_comparison(self, plan):
        before, after = plan['before'].splitlines(), plan['after'].splitlines()
        proposed = [(f'Line {i + 1}', line) for i, line in enumerate(after)]
        current = {f'Line {i + 1}': line for i, line in enumerate(before)}
        comparison = self.configuration_comparison(current, proposed, 'VIEW AND WRITE BOOT CONFIG',
            'Full file preview. Only overlays= is changed; other lines are preserved. Writing does not reboot automatically.')
        comparison.update(proposed_label='Proposed boot file', current_label='Current boot file')
        return comparison

    def apply_boot_plan(self, plan, confirmed=False):
        require_confirmation(confirmed)
        if self.boot.detect() != plan['target'] or self.boot.read(plan['target']) != plan['before']:
            raise ValueError('Boot configuration changed since preview')
        info = self.boot_info()
        values = boot_values(plan['after'])
        if values.get('overlay_prefix', '') != info['overlay_prefix']:
            raise ValueError('Boot overlay prefix changed since preview')
        added = set(values.get('overlays', '').split()) - set(info['values'].get('overlays', '').split())
        missing = self.overlay_warnings(added, info)
        if missing:
            raise ValueError('Overlay files changed since preview: ' + '; '.join(missing))
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

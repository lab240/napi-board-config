import datetime
from dataclasses import asdict, replace
from .models import BoardConfig, MAX_MACS, IF_BY_KEY, RTC_CHOICES, NAME_LEN
from .codec import validate, _date_to_bytes, format_mac, validate_processor_id


def validate_configuration(cfg, catalog):
    validate_processor_id(cfg)
    errors = validate(cfg, catalog)
    for key, limit in [('product_id', 0xffffffff), ('product_rev', 0xffff), ('serial_number', 0xffffffff)]:
        value = getattr(cfg, key)
        if type(value) is not int or not 0 <= value <= limit:
            errors.append(f'{key} must be in 0..{limit}')
    unknown = cfg.enabled - IF_BY_KEY.keys()
    if unknown:
        errors.append(f'Unknown interfaces: {sorted(unknown)}')
    if cfg.rtc_i2c1 != 'none' and 'i2c1' not in cfg.enabled:
        errors.append('I2C1 must be enabled when RTC is enabled')
    if len(cfg.board_name.encode('utf-8')) > NAME_LEN:
        errors.append(f'Board name exceeds {NAME_LEN} UTF-8 bytes')
    try:
        _date_to_bytes(cfg.mfg_date)
    except (ValueError, TypeError) as exc:
        errors.append(str(exc))
    if len(cfg.macs) > MAX_MACS or any(len(mac) != 6 for mac in cfg.macs):
        errors.append('MAC addresses must be six bytes, maximum eight addresses')
    if len(set(cfg.macs)) != len(cfg.macs):
        errors.append('Duplicate MAC addresses')
    if any(mac and mac[0] & 1 for mac in cfg.macs):
        errors.append('Multicast MAC addresses are not allowed')
    if errors:
        raise ValueError('; '.join(errors))


def to_document(cfg):
    data = asdict(cfg)
    data['enabled'] = sorted(cfg.enabled)
    data['macs'] = [format_mac(mac) for mac in cfg.macs]
    data['proc_id'] = cfg.proc_id.hex()
    data['proc_id_len'] = len(cfg.proc_id)
    return data


def from_document(data):
    if not isinstance(data, dict):
        raise ValueError('Configuration must be an object')
    data = dict(data)
    for key in ('platform', 'board_name', 'comment', 'rtc_i2c1', 'mfg_date'):
        if key in data and not isinstance(data[key], str):
            raise ValueError(f'{key} must be a string')
    if isinstance(data.get('enabled'), str) or not isinstance(data.get('enabled', []), list):
        raise ValueError('enabled must be a list')
    enabled = data.get('enabled', [])
    if any(not isinstance(value, str) for value in enabled):
        raise ValueError('enabled must contain interface names')
    macs = data.get('macs', [])
    if not isinstance(macs, list) or any(not isinstance(value, str) for value in macs):
        raise ValueError('macs must be a list of address strings')
    proc_id = data.get('proc_id', '')
    if not isinstance(proc_id, str):
        raise ValueError('proc_id must be a hexadecimal string')
    data['proc_id'] = bytes.fromhex(proc_id)
    if 'proc_id_len' in data:
        length = data.pop('proc_id_len')
        if type(length) is not int or length != len(data['proc_id']):
            raise ValueError('proc_id_len does not match proc_id')
    data['enabled'] = set(enabled)
    data['macs'] = [bytes.fromhex(value.replace(':', '')) for value in macs]
    return BoardConfig(**data)


def updated(cfg, key, value, catalog):
    candidate = replace(cfg, enabled=set(cfg.enabled), macs=list(cfg.macs))
    if key == 'platform':
        catalog.get(value)
        candidate.enabled = set()
        candidate.rtc_i2c1 = 'none'
    setattr(candidate, key, value)
    if key == 'rtc_i2c1' and value != 'none':
        candidate.enabled.add('i2c1')
    validate_configuration(candidate, catalog)
    return candidate


def default_date():
    return datetime.date.today().isoformat()

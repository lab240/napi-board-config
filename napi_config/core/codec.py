import datetime
import struct
import zlib
from .models import *

def platform_conflicts(cfg, key, catalog):
    p = catalog.get(cfg.platform)
    conflicts = p.get("conflicts", {})
    values = conflicts.get(key, [])
    return tuple(values) if isinstance(values, list) else ()

def validate(cfg: BoardConfig, catalog):
    errors = []
    catalog.get(cfg.platform)
    if cfg.rtc_i2c1 not in RTC_CHOICES:
        errors.append(f"Unsupported RTC type: {cfg.rtc_i2c1}")
    for key in cfg.enabled:
        if key not in IF_BY_KEY:
            continue
        for other in platform_conflicts(cfg, key, catalog):
            if other in cfg.enabled and other in IF_BY_KEY:
                errors.append(f"{IF_BY_KEY[key].title} conflicts with {IF_BY_KEY[other].title}")
    return sorted(set(errors))

def _date_to_bytes(value):
    if not value: return 0,0,0
    d=datetime.date.fromisoformat(value)
    if not (2000 <= d.year <= 2255): raise ValueError("Manufacture year must be 2000..2255")
    return d.year-2000,d.month,d.day

def _date_from_bytes(y,m,d):
    if (y,m,d)==(0,0,0): return ""
    return datetime.date(2000+y,m,d).isoformat()

def format_mac(mac):
    return ":".join(f"{b:02X}" for b in mac)

def eeprom_layout(data):
    if len(data) < 5:
        raise ValueError('EEPROM data too short: need format version')
    if data[:4] != MAGIC:
        raise ValueError(f'Bad magic: {data[:4]!r}')
    version = data[4]
    if version not in (2, 3):
        raise ValueError(f'Unsupported EEPROM format version: {version}')
    size = EEPROM_V2_SIZE if version == 2 else EEPROM_SIZE
    if len(data) < size:
        raise ValueError(f'EEPROM data too short: need {size}')
    return size, size - 4


def validate_processor_id(cfg):
    if type(cfg.format_version) is not int or cfg.format_version not in (2, 3):
        raise ValueError('Unsupported EEPROM format version')
    if type(cfg.proc_id_type) is not int or not isinstance(cfg.proc_id, bytes):
        raise ValueError('Invalid processor ID fields')
    if cfg.proc_id_type == 0:
        if cfg.proc_id:
            raise ValueError('Unset processor ID must be empty')
    elif cfg.proc_id_type == 1:
        if cfg.format_version != 3 or cfg.platform != 'rk3308':
            raise ValueError('RK3308 processor binding requires format v3 and RK3308 platform')
        from .mac import validate_otp
        validate_otp(cfg.proc_id)
    else:
        raise ValueError(f'Unsupported proc_id_type: {cfg.proc_id_type}')


def encode_config(cfg: BoardConfig, catalog) -> bytes:
    validate_processor_id(cfg)
    errors = validate(cfg, catalog)
    if errors:
        raise ValueError('; '.join(errors))
    if not 0 <= cfg.serial_number <= 0xffffffff:
        raise ValueError('Serial number must be uint32')
    if len(cfg.macs) > MAX_MACS or any(len(mac) != 6 for mac in cfg.macs):
        raise ValueError('MAC addresses must be six bytes, maximum eight addresses')
    y, month, day = _date_to_bytes(cfg.mfg_date)
    name = cfg.board_name.encode('utf-8')
    if len(name) > NAME_LEN:
        raise ValueError('Board name exceeds 32 UTF-8 bytes')
    header = EEPROM_HEADER.pack(MAGIC, cfg.format_version, catalog.id_for(cfg.platform),
                               cfg.product_id, cfg.product_rev, cfg.mask(), cfg.serial_number,
                               y, month, day, len(cfg.macs), name.ljust(NAME_LEN, b'\x00'))
    body = header + b''.join(cfg.macs).ljust(MAX_MACS * 6, b'\x00')
    if cfg.format_version == 3:
        body += bytes([cfg.proc_id_type, len(cfg.proc_id)]) + cfg.proc_id.ljust(16, b'\x00')
    return body + struct.pack('<I', zlib.crc32(body) & 0xffffffff)


def decode_config(data: bytes, catalog) -> BoardConfig:
    size, crc_offset = eeprom_layout(data)
    body = data[:crc_offset]
    crc = int.from_bytes(data[crc_offset:size], 'little')
    expected = zlib.crc32(body) & 0xffffffff
    if crc != expected:
        raise ValueError(f'CRC mismatch: stored=0x{crc:08x}, expected=0x{expected:08x}')
    magic, fmt, pid, product, rev, mask, serial, y, month, day, count, raw = EEPROM_HEADER.unpack(body[:EEPROM_HEADER.size])
    if count > MAX_MACS:
        raise ValueError(f'Invalid MAC count: {count}')
    if sum(bool(mask & (1 << bit)) for bit in RTC_BITS.values()) > 1:
        raise ValueError('RTC bits must be mutually exclusive')
    cfg = BoardConfig.from_mask(product, rev, raw.split(b'\x00', 1)[0].decode('utf-8'),
                                mask if fmt == 2 else mask & ~(1 << LEGACY_RTC_BIT))
    cfg.format_version = fmt
    cfg.platform = catalog.name_for_id(pid)
    cfg.serial_number = serial
    cfg.mfg_date = _date_from_bytes(y, month, day)
    cfg.macs = [body[EEPROM_MAC_OFFSET + i * 6:EEPROM_MAC_OFFSET + (i + 1) * 6] for i in range(count)]
    if fmt == 3:
        kind, length = body[EEPROM_PROC_OFFSET:EEPROM_PROC_OFFSET + 2]
        area = body[EEPROM_PROC_OFFSET + 2:EEPROM_CRC_OFFSET]
        if length > 16 or any(area[length:]):
            raise ValueError('Invalid proc_id length or nonzero padding')
        if kind == 0 and length != 0:
            raise ValueError('Unset processor ID must have zero length')
        cfg.proc_id_type, cfg.proc_id = kind, area[:length]
    validate_processor_id(cfg)
    return cfg


def replace_eeprom_macs(data, macs, catalog):
    decode_config(data, catalog)
    size, crc_offset = eeprom_layout(data)
    if len(macs) != MAX_MACS or any(len(mac) != 6 for mac in macs):
        raise ValueError('MAC-only write requires exactly eight six-byte MAC addresses')
    body = bytearray(data[:crc_offset])
    body[EEPROM_MAC_COUNT_OFFSET] = MAX_MACS
    body[EEPROM_MAC_OFFSET:EEPROM_MAC_END] = b''.join(macs)
    return bytes(body) + struct.pack('<I', zlib.crc32(body) & 0xffffffff)


def overlays_for(cfg: BoardConfig, catalog):
    p = catalog.get(cfg.platform)
    mapping = p.get("overlays", {})
    prefix = str(p.get("overlay_prefix", "")).strip()
    enabled = set(cfg.enabled)

    rtc_key = "i2c1" if cfg.rtc_i2c1 == "none" else f"rtc_{cfg.rtc_i2c1}"
    overlays = []
    rtc_overlay = mapping.get(rtc_key)
    if rtc_overlay and "i2c1" in enabled:
        overlays.append(str(rtc_overlay))
    enabled.discard("i2c1")

    if "w5500_spi1" in enabled:
        enabled.discard("spi1")
    if "w5500_spi2" in enabled:
        enabled.discard("spi2")

    for i in INTERFACES:
        if i.key in enabled and mapping.get(i.key):
            overlays.append(str(mapping[i.key]))

    # Defensive normalization: overlays= must not repeat overlay_prefix.
    if prefix:
        pref = prefix + "-"
        overlays = [x[len(pref):] if x.startswith(pref) else x for x in overlays]
    return list(dict.fromkeys(overlays))

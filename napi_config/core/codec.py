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

def encode_config(cfg: BoardConfig, catalog) -> bytes:
    errors=validate(cfg, catalog)
    if errors: raise ValueError("; ".join(errors))
    if not (0 <= cfg.serial_number <= 0xffffffff): raise ValueError("Serial number must be uint32")
    if len(cfg.macs)>MAX_MACS: raise ValueError(f"Maximum {MAX_MACS} MAC addresses")
    y,mo,d=_date_to_bytes(cfg.mfg_date)
    name=cfg.board_name.encode("utf-8")[:NAME_LEN]
    name+=b"\x00"*(NAME_LEN-len(name))
    header=EEPROM_HEADER.pack(MAGIC,FORMAT_VERSION,catalog.id_for(cfg.platform),
        cfg.product_id,cfg.product_rev,cfg.mask(),cfg.serial_number,y,mo,d,len(cfg.macs),name)
    area=b"".join(bytes(x) for x in cfg.macs)
    if any(len(x)!=6 for x in cfg.macs): raise ValueError("MAC must be 6 bytes")
    area+=b"\x00"*(MAX_MACS*6-len(area))
    body=header+area
    return body+struct.pack("<I",zlib.crc32(body)&0xffffffff)

def decode_config(data: bytes, catalog) -> BoardConfig:
    if len(data)<EEPROM_SIZE: raise ValueError(f"EEPROM data too short: need {EEPROM_SIZE}")
    body=data[:EEPROM_SIZE-4]
    crc=struct.unpack("<I",data[EEPROM_SIZE-4:EEPROM_SIZE])[0]
    expected=zlib.crc32(body)&0xffffffff
    if crc!=expected: raise ValueError(f"CRC mismatch: stored=0x{crc:08x}, expected=0x{expected:08x}")
    magic,fmt,pid,product,rev,mask,serial,y,mo,d,count,raw=EEPROM_HEADER.unpack(body[:EEPROM_HEADER.size])
    if magic!=MAGIC: raise ValueError(f"Bad magic: {magic!r}")
    if fmt!=FORMAT_VERSION: raise ValueError(f"Unsupported EEPROM format version: {fmt}")
    if count>MAX_MACS: raise ValueError(f"Invalid MAC count: {count}")
    cfg=BoardConfig.from_mask(product,rev,raw.split(b"\x00",1)[0].decode("utf-8","replace"),mask)
    cfg.platform=catalog.name_for_id(pid)
    cfg.serial_number=serial
    cfg.mfg_date=_date_from_bytes(y,mo,d)
    area=body[EEPROM_HEADER.size:EEPROM_HEADER.size+MAX_MACS*6]
    cfg.macs=[area[i*6:(i+1)*6] for i in range(count)]
    return cfg

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

#!/usr/bin/env python3
import argparse
import curses
import os
import re
import shutil
import struct
import time
import zlib
import urllib.request
import secrets
import datetime
from dataclasses import dataclass, field
from pathlib import Path
try:
    import yaml
except ImportError:
    raise SystemExit("PyYAML is required. Install: apt install python3-yaml")

MAGIC = b"NAPI"
FORMAT_VERSION = 2
NAME_LEN = 32
MAX_MACS = 8
EEPROM_HEADER = struct.Struct("<4sBBIHIIBBBB32s")
EEPROM_SIZE = EEPROM_HEADER.size + MAX_MACS * 6 + 4

DEFAULT_EEPROM = "/sys/bus/i2c/devices/1-0050/eeprom"
DEFAULT_DB = str(Path(__file__).resolve().with_name("boards.yaml"))
DEFAULT_PLATFORMS = str(Path(__file__).resolve().with_name("platforms.yaml"))
GITHUB_DB_URL = "https://raw.githubusercontent.com/napilab/napi-boards/main/yaml-config/boards.yaml"

ENV_TARGETS = {
    "armbianEnv": "/boot/armbianEnv.txt",
    "uEnv": "/boot/uEnv.txt",
}

@dataclass(frozen=True)
class InterfaceDef:
    key: str
    title: str
    bit: int

INTERFACES = [
    InterfaceDef("i2c0", "I2C0", 9),
    InterfaceDef("i2c1", "I2C1 (EEPROM, required)", 10),
    InterfaceDef("i2c3", "I2C3", 11),
    InterfaceDef("usb_host", "USB Host", 1),
    InterfaceDef("spi1", "SPI1", 2),
    InterfaceDef("spi2", "SPI2", 3),
    InterfaceDef("uart1", "UART1", 4),
    InterfaceDef("uart2", "UART2", 5),
    InterfaceDef("uart3", "UART3", 6),
    InterfaceDef("uart4", "UART4", 12),
    InterfaceDef("w5500_spi1", "W5500 on SPI1", 7),
    InterfaceDef("w5500_spi2", "W5500 on SPI2", 8),
]

IF_BY_KEY = {x.key: x for x in INTERFACES}

RTC_CHOICES = ("none", "ds1307", "ds1338", "ds3231")
# EEPROM mask bits reserved for RTC type.
# Legacy bit 0 from v5 means DS1338.
RTC_BITS = {
    "ds1307": 13,
    "ds1338": 14,
    "ds3231": 15,
}
LEGACY_RTC_BIT = 0

@dataclass
class BoardConfig:
    platform: str = "rk3308"
    product_id: int = 0
    product_rev: int = 1
    board_name: str = "NAPI Board"
    comment: str = ""
    enabled: set[str] = field(default_factory=lambda: {"i2c1"})
    rtc_i2c1: str = "none"
    serial_number: int = 0
    mfg_date: str = ""
    macs: list[bytes] = field(default_factory=list)

    def mask(self) -> int:
        enabled = set(self.enabled)
        enabled.add("i2c1")  # EEPROM bus is mandatory.
        m = 0
        for key in enabled:
            if key in IF_BY_KEY:
                m |= 1 << IF_BY_KEY[key].bit
        if self.rtc_i2c1 in RTC_BITS:
            m |= 1 << RTC_BITS[self.rtc_i2c1]
        return m

    @classmethod
    def from_mask(cls, product_id, product_rev, board_name, mask):
        enabled = {i.key for i in INTERFACES if mask & (1 << i.bit)}
        enabled.add("i2c1")

        rtc = "none"
        for name, bit in RTC_BITS.items():
            if mask & (1 << bit):
                rtc = name
                break

        # Backward compatibility with v5 EEPROM: old rtc bit meant DS1338.
        if rtc == "none" and (mask & (1 << LEGACY_RTC_BIT)):
            rtc = "ds1338"

        return cls(platform="rk3308", product_id=product_id, product_rev=product_rev, board_name=board_name, comment="", enabled=enabled, rtc_i2c1=rtc)

class PlatformDB:
    def __init__(self, path=DEFAULT_PLATFORMS):
        self.path = Path(path)
        self.data = self._load()

    def _load(self):
        if not self.path.exists():
            raise FileNotFoundError(f"Platform database not found: {self.path}")
        data = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("platforms"), dict):
            raise ValueError("Invalid platforms.yaml")
        return data["platforms"]

    def get(self, name):
        p = self.data.get(name)
        if not isinstance(p, dict):
            raise ValueError(f"Unknown platform: {name}")
        return p

    def names(self):
        return list(self.data.keys())

    def id_for(self, name):
        return int(self.get(name).get("id", 0))

    def name_for_id(self, platform_id):
        for name, p in self.data.items():
            if int(p.get("id", -1)) == int(platform_id):
                return name
        raise ValueError(f"Unknown platform id: {platform_id}")

PLATFORMS = None

def platform_db():
    global PLATFORMS
    if PLATFORMS is None:
        PLATFORMS = PlatformDB()
    return PLATFORMS

def platform_conflicts(cfg, key):
    p = platform_db().get(cfg.platform)
    conflicts = p.get("conflicts", {})
    values = conflicts.get(key, [])
    return tuple(values) if isinstance(values, list) else ()

def validate(cfg: BoardConfig):
    errors = []
    platform_db().get(cfg.platform)
    if cfg.rtc_i2c1 not in RTC_CHOICES:
        errors.append(f"Unsupported RTC type: {cfg.rtc_i2c1}")
    for key in cfg.enabled:
        if key not in IF_BY_KEY:
            continue
        for other in platform_conflicts(cfg, key):
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

def encode_config(cfg: BoardConfig) -> bytes:
    errors=validate(cfg)
    if errors: raise ValueError("; ".join(errors))
    if not (0 <= cfg.serial_number <= 0xffffffff): raise ValueError("Serial number must be uint32")
    if len(cfg.macs)>MAX_MACS: raise ValueError(f"Maximum {MAX_MACS} MAC addresses")
    y,mo,d=_date_to_bytes(cfg.mfg_date)
    name=cfg.board_name.encode("utf-8")[:NAME_LEN]
    name+=b"\x00"*(NAME_LEN-len(name))
    header=EEPROM_HEADER.pack(MAGIC,FORMAT_VERSION,platform_db().id_for(cfg.platform),
        cfg.product_id,cfg.product_rev,cfg.mask(),cfg.serial_number,y,mo,d,len(cfg.macs),name)
    area=b"".join(bytes(x) for x in cfg.macs)
    if any(len(x)!=6 for x in cfg.macs): raise ValueError("MAC must be 6 bytes")
    area+=b"\x00"*(MAX_MACS*6-len(area))
    body=header+area
    return body+struct.pack("<I",zlib.crc32(body)&0xffffffff)

def decode_config(data: bytes) -> BoardConfig:
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
    cfg.platform=platform_db().name_for_id(pid)
    cfg.serial_number=serial
    cfg.mfg_date=_date_from_bytes(y,mo,d)
    area=body[EEPROM_HEADER.size:EEPROM_HEADER.size+MAX_MACS*6]
    cfg.macs=[area[i*6:(i+1)*6] for i in range(count)]
    return cfg

def overlays_for(cfg: BoardConfig):
    p = platform_db().get(cfg.platform)
    mapping = p.get("overlays", {})
    prefix = str(p.get("overlay_prefix", "")).strip()
    enabled = set(cfg.enabled)
    enabled.add("i2c1")

    rtc_key = "i2c1" if cfg.rtc_i2c1 == "none" else f"rtc_{cfg.rtc_i2c1}"
    overlays = []
    rtc_overlay = mapping.get(rtc_key)
    if rtc_overlay:
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

def user_overlay_name(cfg: BoardConfig, key):
    p = platform_db().get(cfg.platform)
    value = p.get("user_overlays", {}).get(key)
    return str(value) if value else None

def human_config(cfg: BoardConfig, env_target="armbianEnv"):
    env_path = ENV_TARGETS.get(env_target, env_target)
    lines = [
        f"EEPROM format: {FORMAT_VERSION}",
        f"Platform:      {cfg.platform}",
        f"Product ID:    {cfg.product_id}",
        f"Product rev:   {cfg.product_rev}",
        f"Board name:    {cfg.board_name}",
        f"Comment:       {cfg.comment}",
        f"Serial:        {cfg.serial_number}",
        f"Mfg date:      {cfg.mfg_date or '-'}",
        f"MAC count:     {len(cfg.macs)}",
        f"Interface mask: 0x{cfg.mask():08x}",
        f"Boot env:      {env_path}",
        f"RTC on I2C1:   {cfg.rtc_i2c1}",
        "",
        "Interfaces:",
    ]
    for i in INTERFACES:
        mark = "x" if (i.key in cfg.enabled or i.key == "i2c1") else " "
        lines.append(f"  [{mark}] {i.title}")
    lines += ["", "Armbian/U-Boot:", "overlays=" + " ".join(overlays_for(cfg))]
    return "\n".join(lines)

class ProfileDB:
    def __init__(self, path: str):
        self.path = Path(path)

    def load(self):
        if not self.path.exists():
            return {"version": 1, "boards": []}
        data = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        if data is None:
            return {"version": 1, "boards": []}
        if not isinstance(data, dict):
            raise ValueError("Invalid YAML profile database")
        data.setdefault("version", 1)
        data.setdefault("boards", [])
        return data

    def save(self, db):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            stamp = time.strftime("%Y%m%d-%H%M%S")
            backup = self.path.with_name(self.path.name + f".bak-{stamp}")
            shutil.copy2(self.path, backup)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            yaml.safe_dump(db, allow_unicode=True, sort_keys=False),
            encoding="utf-8"
        )
        tmp.replace(self.path)

    def add_or_replace(self, cfg: BoardConfig):
        db = self.load()
        db["version"] = 2
        boards = db.setdefault("boards", [])
        slug = cfg.board_name.lower().strip()
        slug = "".join(ch if ch.isalnum() else "-" for ch in slug)
        while "--" in slug:
            slug = slug.replace("--", "-")

        item = {
            "id": cfg.product_id,
            "rev": cfg.product_rev,
            "slug": slug.strip("-") or f"board-{cfg.product_id}",
            "name": cfg.board_name,
            "comment": cfg.comment,
            "platform": cfg.platform,
            "interfaces": {
                i.key: (i.key in cfg.enabled)
                for i in INTERFACES
                if i.key not in {"i2c0", "i2c1", "i2c3"}
            },
            "i2c": {
                "i2c0": {"enabled": "i2c0" in cfg.enabled},
                "i2c1": {
                    "enabled": True,
                    "eeprom": True,
                    "rtc": cfg.rtc_i2c1,
                },
                "i2c3": {"enabled": "i2c3" in cfg.enabled},
            },
        }

        replaced = False
        for n, b in enumerate(boards):
            if b.get("id") == cfg.product_id and b.get("rev") == cfg.product_rev:
                boards[n] = item
                replaced = True
                break
        if not replaced:
            boards.append(item)
        boards.sort(key=lambda b: (b.get("id", 0), b.get("rev", 0)))
        self.save(db)
        return replaced

    def delete(self, product_id, product_rev):
        db = self.load()
        before = len(db.get("boards", []))
        db["boards"] = [
            b for b in db.get("boards", [])
            if not (b.get("id") == product_id and b.get("rev") == product_rev)
        ]
        self.save(db)
        return len(db["boards"]) != before

    def to_config(self, b):
        enabled = {"i2c1"}

        interfaces = b.get("interfaces", {})
        if isinstance(interfaces, dict):
            enabled |= {k for k, v in interfaces.items() if v and k in IF_BY_KEY}
        elif isinstance(interfaces, list):
            enabled |= {k for k in interfaces if k in IF_BY_KEY}

        rtc = "none"
        i2c = b.get("i2c", {})
        if isinstance(i2c, dict):
            for bus in ("i2c0", "i2c1", "i2c3"):
                bus_cfg = i2c.get(bus, {})
                if isinstance(bus_cfg, dict) and bus_cfg.get("enabled"):
                    enabled.add(bus)
            i2c1 = i2c.get("i2c1", {})
            if isinstance(i2c1, dict):
                rtc = str(i2c1.get("rtc", "none")).lower()

        # Backward compatibility with v5 flat YAML.
        if isinstance(interfaces, dict) and interfaces.get("rtc") and rtc == "none":
            rtc = "ds1338"

        if rtc not in RTC_CHOICES:
            rtc = "none"

        enabled.discard("rtc")
        enabled.add("i2c1")
        return BoardConfig(
            platform=str(b.get("platform", "rk3308")),
            product_id=int(b.get("id", 0)),
            product_rev=int(b.get("rev", 1)),
            board_name=str(b.get("name", "NAPI Board")),
            comment=str(b.get("comment", "")),
            enabled=enabled,
            rtc_i2c1=rtc,
        )

class UI:
    ACTIONS = [
        ("action", "load_defaults"),
        ("action", "read"),
        ("action", "write_eeprom"),
        ("action", "write_env"),
        ("action", "profile_load"),
        ("action", "profile_add"),
        ("action", "profile_delete"),
        ("action", "github_db"),
    ]

    def __init__(self, stdscr, eeprom_path, db_path):
        self.stdscr = stdscr
        self.eeprom_path = Path(eeprom_path)
        self.db = ProfileDB(db_path)
        self.cfg = BoardConfig(enabled={"i2c1"})
        self.env_target = "armbianEnv"
        self.blob = None
        self.status = "Ready"
        self.cursor = 0
        self.rows = list(self.ACTIONS) + [
            ("field", "platform"),
            ("field", "product_id"),
            ("field", "product_rev"),
            ("field", "board_name"),
            ("field", "comment"),
        ]
        self.rows += [("iface", i.key) for i in INTERFACES]
        self.rows += [
            ("rtc", "rtc_enabled"),
            ("field", "rtc_type"),
            ("instance", "serial_number"),
            ("instance", "mfg_date"),
            ("instance", "mac_count"),
            ("field", "env_target"),
            ("action", "enable_i2c1_eeprom"),
            ("action", "quit"),
        ]

    def is_action_cursor(self):
        return 0 <= self.cursor < len(self.ACTIONS)

    def run(self):
        curses.curs_set(0)
        self.stdscr.keypad(True)
        while True:
            self.draw()
            ch = self.stdscr.getch()
            if ch == 3:  # Ctrl-C
                return
            if self.is_action_cursor() and ch in (curses.KEY_LEFT, curses.KEY_RIGHT,
                                                   curses.KEY_UP, curses.KEY_DOWN,
                                                   ord("h"), ord("l"), ord("k"), ord("j")):
                n = len(self.ACTIONS)
                cols = 2 if self.stdscr.getmaxyx()[1] >= 64 else 1
                if cols == 2:
                    if ch in (curses.KEY_LEFT, ord("h")):
                        self.cursor = max(0, self.cursor - 1)
                    elif ch in (curses.KEY_RIGHT, ord("l")):
                        self.cursor = min(n-1, self.cursor + 1)
                    elif ch in (curses.KEY_UP, ord("k")):
                        self.cursor = max(0, self.cursor - 2)
                    else:
                        nxt = self.cursor + 2
                        self.cursor = nxt if nxt < n else min(n, len(self.rows)-1)
                else:
                    if ch in (curses.KEY_UP, ord("k")):
                        self.cursor = (self.cursor - 1) % len(self.rows)
                    elif ch in (curses.KEY_DOWN, ord("j")):
                        self.cursor = (self.cursor + 1) % len(self.rows)
            elif ch in (curses.KEY_UP, ord("k")):
                self.cursor = (self.cursor - 1) % len(self.rows)
            elif ch in (curses.KEY_DOWN, ord("j")):
                self.cursor = (self.cursor + 1) % len(self.rows)
            elif ch == ord(" "):
                kind, key = self.rows[self.cursor]
                if kind == "iface":
                    self.toggle_interface(key)
                elif kind == "rtc":
                    self.toggle_rtc()
            elif ch in (10, 13, curses.KEY_ENTER):
                if self.activate():
                    return
            elif ch in (ord("q"), ord("Q")):
                return

    def action_label(self, key):
        return {
            "load_defaults": "[ Load defaults ]",
            "read": "[ Load EEPROM ]",
            "write_eeprom": "[ Write EEPROM ]",
            "write_env": "[ Write boot file ]",
            "profile_load": "[ Load board from local DB ]",
            "profile_add": "[ Save board to local DB ]",
            "profile_delete": "[ Delete board from local DB ]",
            "github_db": "[ Load DB from GitHub ]",
            "enable_i2c1_eeprom": "[ Add EEPROM overlay + reboot ]",
            "quit": "[ Quit ]",
        }[key]

    def config_text(self, kind, key):
        if kind == "field":
            if key == "platform":
                return f"Platform     : {self.cfg.platform}"
            if key == "product_id":
                return f"Product ID   : {self.cfg.product_id}"
            if key == "product_rev":
                return f"Product rev  : {self.cfg.product_rev}"
            if key == "board_name":
                return f"Board name   : {self.cfg.board_name}"
            if key == "comment":
                return f"Comment      : {self.cfg.comment}"
            if key == "env_target":
                return f"Boot file    : {ENV_TARGETS.get(self.env_target, self.env_target)}"
            if key == "rtc_type":
                rtc = self.cfg.rtc_i2c1.upper() if self.cfg.rtc_i2c1 != "none" else "-"
                return f"    RTC type : {rtc}"
        if kind == "instance":
            if key == "serial_number":
                return f"Serial number: {self.cfg.serial_number:08d}"
            if key == "mfg_date":
                return f"Mfg date     : {self.cfg.mfg_date or '-'}"
            if key == "mac_count":
                return f"MAC addresses: {len(self.cfg.macs)}"
        if kind == "rtc":
            return f"[{'x' if self.cfg.rtc_i2c1 != 'none' else ' '}] RTC"
        if kind == "iface":
            idef = IF_BY_KEY[key]
            mark = "x" if key in self.cfg.enabled else " "
            suffix = ""
            blocked = self.blocked_by(key)
            if blocked and key not in self.cfg.enabled:
                suffix += f" (blocked by {', '.join(blocked)})"
            return f"[{mark}] {idef.title}{suffix}"
        return self.action_label(key)

    def draw(self):
        s = self.stdscr
        s.erase()
        h, w = s.getmaxyx()
        if h < 8 or w < 28:
            try:
                s.addnstr(0, 0, "Terminal too small", max(1, w-1))
                s.addnstr(1, 0, f"{w}x{h}; need >=28x8", max(1, w-1))
            except curses.error:
                pass
            s.refresh()
            return

        title = " NAPI Board Config v14 "
        try:
            s.addnstr(0, max(0,(w-len(title))//2), title, w-1, curses.A_BOLD)
        except curses.error:
            pass

        action_cols = 2 if w >= 64 else 1
        action_rows = (len(self.ACTIONS)+action_cols-1)//action_cols
        logical = []

        logical.append(("section", "--- ACTIONS ---", None))
        for r in range(action_rows):
            logical.append(("actionrow", r, None))
        logical.append(("section", "--- BOARD CONFIG ---", None))

        first_cfg = len(self.ACTIONS)
        env_index = next(i for i,x in enumerate(self.rows) if x == ("field","env_target"))
        service_index = next(i for i,x in enumerate(self.rows) if x == ("action","enable_i2c1_eeprom"))
        for idx in range(first_cfg, env_index):
            logical.append(("item", idx, None))
        logical.append(("section", "--- PROGRAM SETTINGS ---", None))
        logical.append(("item", env_index, None))
        logical.append(("section", "--- SERVICE ---", None))
        for idx in range(service_index, len(self.rows)):
            logical.append(("item", idx, None))

        # Determine which logical screen line contains the cursor.
        cursor_line = 1
        for n, entry in enumerate(logical):
            typ, val, _ = entry
            if typ == "actionrow":
                r=val
                ids=[r*action_cols+c for c in range(action_cols) if r*action_cols+c < len(self.ACTIONS)]
                if self.cursor in ids: cursor_line=n
            elif typ=="item" and val==self.cursor:
                cursor_line=n

        visible=max(1,h-3)
        if not hasattr(self,"scroll_top"): self.scroll_top=0
        if cursor_line < self.scroll_top: self.scroll_top=cursor_line
        elif cursor_line >= self.scroll_top+visible:
            self.scroll_top=cursor_line-visible+1
        self.scroll_top=max(0,min(self.scroll_top,max(0,len(logical)-visible)))

        y=1
        for typ,val,_ in logical[self.scroll_top:self.scroll_top+visible]:
            try:
                if typ=="section":
                    s.addnstr(y,1,val,max(1,w-2),curses.A_BOLD)
                elif typ=="actionrow":
                    r=val
                    if action_cols==2:
                        colw=max(1,(w-3)//2)
                        for c in range(2):
                            idx=r*2+c
                            if idx>=len(self.ACTIONS): continue
                            text=self.action_label(self.ACTIONS[idx][1])
                            attr=curses.A_REVERSE if idx==self.cursor else curses.A_NORMAL
                            s.addnstr(y,1+c*colw,text,max(1,colw-1),attr)
                    else:
                        idx=r
                        text=self.action_label(self.ACTIONS[idx][1])
                        attr=curses.A_REVERSE if idx==self.cursor else curses.A_NORMAL
                        s.addnstr(y,1,text,max(1,w-2),attr)
                else:
                    idx=val
                    kind,key=self.rows[idx]
                    text=self.config_text(kind,key)
                    # RTC type is visibly disabled while RTC is off.
                    if key=="rtc_type" and self.cfg.rtc_i2c1=="none":
                        attr=curses.A_DIM
                    else:
                        attr=curses.A_REVERSE if idx==self.cursor else curses.A_NORMAL
                    s.addnstr(y,1,text,max(1,w-2),attr)
            except curses.error:
                pass
            y+=1

        try:
            s.addnstr(h-2,1,self.status,max(1,w-2))
            nav="Arrows move  Space toggle  Enter select  q quit  Ctrl-C exit"
            s.addnstr(h-1,1,nav,max(1,w-2),curses.A_DIM)
        except curses.error:
            pass
        s.refresh()

    def blocked_by(self, key):
        blockers=[]
        for other in platform_conflicts(self.cfg, key):
            if other in self.cfg.enabled and other in IF_BY_KEY:
                blockers.append(IF_BY_KEY[other].title)
        for other in self.cfg.enabled:
            if other in IF_BY_KEY and key in platform_conflicts(self.cfg, other):
                title=IF_BY_KEY[other].title
                if title not in blockers:
                    blockers.append(title)
        return blockers

    def toggle_interface(self, key):
        if key=="i2c1":
            self.cfg.enabled.add("i2c1")
            self.status="I2C1 is required for EEPROM and cannot be disabled"
            curses.beep(); return
        if key in self.cfg.enabled:
            self.cfg.enabled.remove(key)
            self.status=f"Disabled {IF_BY_KEY[key].title}"
            self.blob=None; return
        blocked=self.blocked_by(key)
        if blocked:
            self.status=f"Cannot enable {IF_BY_KEY[key].title}: conflict with {', '.join(blocked)}"
            curses.beep(); return
        self.cfg.enabled.add(key)
        self.status=f"Enabled {IF_BY_KEY[key].title}"
        self.blob=None

    def toggle_rtc(self):
        if self.cfg.rtc_i2c1=="none":
            self.cfg.rtc_i2c1="ds1338"
            self.status="RTC enabled; type DS1338"
        else:
            self.cfg.rtc_i2c1="none"
            self.status="RTC disabled"
        self.blob=None

    def activate(self):
        kind,key=self.rows[self.cursor]
        if kind=="field":
            self.edit_field(key); return False
        if kind=="iface":
            self.toggle_interface(key); return False
        if kind=="rtc":
            self.toggle_rtc(); return False
        if kind=="instance":
            self.edit_instance(key); return False
        {
            "load_defaults":self.load_defaults,
            "read":self.read_eeprom,
            "write_eeprom":self.write_eeprom,
            "write_env":self.write_env,
            "profile_load":self.profile_load,
            "profile_add":self.profile_add,
            "profile_delete":self.profile_delete,
            "github_db":self.load_db_github,
            "generate_macs":self.generate_macs,
            "view_macs":self.view_macs,
            "enable_i2c1_eeprom":self.enable_i2c1_eeprom,
        }.get(key,lambda:None)()
        return key=="quit"

    def prompt(self,label,initial):
        h,w=self.stdscr.getmaxyx()
        curses.echo(); curses.curs_set(1)
        try:
            self.stdscr.move(h-2,1); self.stdscr.clrtoeol()
            prompt=f"{label} [{initial}]: "
            self.stdscr.addstr(h-2,1,prompt)
            raw=self.stdscr.getstr(h-2,1+len(prompt),max(1,w-len(prompt)-3))
            value=raw.decode("utf-8").strip()
            return value if value else str(initial)
        finally:
            curses.noecho(); curses.curs_set(0)

    def edit_field(self,key):
        try:
            if key=="platform":
                names=platform_db().names()
                cur=self.cfg.platform if self.cfg.platform in names else names[0]
                self.cfg.platform=names[(names.index(cur)+1)%len(names)]
                self.cfg.enabled={"i2c1"}
                self.cfg.rtc_i2c1="none"
            elif key=="product_id":
                self.cfg.product_id=int(self.prompt("Product ID",self.cfg.product_id),0)
            elif key=="product_rev":
                self.cfg.product_rev=int(self.prompt("Product rev",self.cfg.product_rev),0)
            elif key=="board_name":
                self.cfg.board_name=self.prompt("Board name",self.cfg.board_name)
            elif key=="comment":
                self.cfg.comment=self.prompt("Comment",self.cfg.comment)
            elif key=="env_target":
                self.env_target="uEnv" if self.env_target=="armbianEnv" else "armbianEnv"
            elif key=="rtc_type":
                if self.cfg.rtc_i2c1=="none":
                    self.status="Enable RTC first"
                    curses.beep(); return
                types=["ds1307","ds1338","ds3231"]
                cur=self.cfg.rtc_i2c1 if self.cfg.rtc_i2c1 in types else "ds1338"
                self.cfg.rtc_i2c1=types[(types.index(cur)+1)%len(types)]
            self.blob=None
            self.status="Value updated"
        except ValueError:
            self.status="Invalid numeric value"; curses.beep()

    def confirm_yes(self, message):
        h, w = self.stdscr.getmaxyx()
        ph = min(9, h)
        pw = min(max(42, min(70, w - 2)), w)
        y0 = max(0, (h - ph) // 2)
        x0 = max(0, (w - pw) // 2)
        win = curses.newwin(ph, pw, y0, x0)
        win.keypad(True)
        win.erase()
        win.box()

        # Wrap the question inside the dialog instead of putting it on one terminal line.
        usable = max(10, pw - 4)
        words = message.split()
        lines, cur = [], ""
        for word in words:
            candidate = word if not cur else cur + " " + word
            if len(candidate) <= usable:
                cur = candidate
            else:
                if cur:
                    lines.append(cur)
                cur = word
        if cur:
            lines.append(cur)
        lines = lines[:3]

        try:
            for i, line in enumerate(lines):
                win.addnstr(1 + i, 2, line, usable)
            prompt_y = min(ph - 3, 2 + len(lines))
            win.addnstr(prompt_y, 2, "Type yes to confirm:", usable, curses.A_BOLD)
            win.addnstr(ph - 2, 2, "Anything else = cancel", usable, curses.A_DIM)
        except curses.error:
            pass

        curses.echo()
        curses.curs_set(1)
        try:
            input_y = min(ph - 2, prompt_y + 1)
            win.move(input_y, 2)
            win.clrtoeol()
            win.addstr(input_y, 2, "> ")
            win.refresh()
            raw = win.getstr(input_y, 4, max(1, pw - 6))
            value = raw.decode("utf-8", errors="replace").strip()
            return value == "yes"
        except KeyboardInterrupt:
            raise
        finally:
            curses.noecho()
            curses.curs_set(0)

    def enable_i2c1_eeprom(self):
        try:
            path=Path(ENV_TARGETS[self.env_target])
            if not path.exists():
                self.status=f"Boot config not found: {path}"; curses.beep(); return
            pdef=platform_db().get(self.cfg.platform)
            mapping=pdef.get("overlays", {})
            variants={str(mapping.get("i2c1",""))}
            for rtc in ("ds1307","ds1338","ds3231"):
                v=mapping.get(f"rtc_{rtc}")
                if v: variants.add(str(v))
            variants.discard("")
            eeprom_overlay=user_overlay_name(self.cfg, "eeprom")
            if not eeprom_overlay:
                self.status=f"No EEPROM user overlay defined for {self.cfg.platform}"; curses.beep(); return

            old=path.read_text(encoding="utf-8")
            lines=old.splitlines()
            result=[]; found_overlays=False; found_user=False
            stock_i2c1=str(mapping.get("i2c1",""))
            for ln in lines:
                if ln.startswith("overlays=") and not found_overlays:
                    found_overlays=True
                    items=ln.split("=",1)[1].split()
                    prefix=str(pdef.get("overlay_prefix","")).strip()
                    if prefix:
                        pref=prefix+"-"
                        items=[x[len(pref):] if x.startswith(pref) else x for x in items]
                    if stock_i2c1 and not any(x in variants for x in items):
                        items.insert(0,stock_i2c1)
                    result.append("overlays="+" ".join(dict.fromkeys(items)))
                elif ln.startswith("user_overlays=") and not found_user:
                    found_user=True
                    items=ln.split("=",1)[1].split()
                    if eeprom_overlay not in items: items.append(eeprom_overlay)
                    result.append("user_overlays="+" ".join(dict.fromkeys(items)))
                else:
                    result.append(ln)
            if not found_overlays and stock_i2c1:
                result.append("overlays="+stock_i2c1)
            if not found_user:
                result.append("user_overlays="+eeprom_overlay)
            new="\n".join(result).rstrip()+"\n"
            if new==old.rstrip()+"\n":
                self.status="I2C1 and EEPROM overlay already configured"; return
            if not self.confirm_yes(f"Modify {path}, add EEPROM overlay and reboot?"):
                self.status="Cancelled"; return
            stamp=time.strftime("%Y%m%d-%H%M%S")
            backup=path.with_name(path.name+f".bak-{stamp}")
            shutil.copy2(path,backup)
            path.write_text(new,encoding="utf-8"); os.sync()
            self.status=f"EEPROM overlay added; backup {backup.name}; rebooting"
            self.draw(); time.sleep(1); os.system("reboot")
        except Exception as e:
            self.status=f"EEPROM overlay error: {e}"; curses.beep()

    def i2c1_status(self):
        eeprom=self.eeprom_path
        if eeprom.exists(): return True,"I2C1 and EEPROM 0x50 detected"
        m=re.search(r"/(\d+)-0050/eeprom$",str(eeprom)); bus=m.group(1) if m else "1"
        adapters=[Path(f"/sys/class/i2c-adapter/i2c-{bus}"),Path(f"/sys/bus/i2c/devices/i2c-{bus}")]
        if not any(p.exists() for p in adapters):
            return False,f"I2C{bus} is not enabled. EEPROM requires I2C1. Enable the I2C1 overlay in boot config and reboot."
        dev=Path(f"/sys/bus/i2c/devices/{bus}-0050")
        if not dev.exists(): return False,f"EEPROM 0x50 not detected on I2C{bus}"
        return False,f"EEPROM device exists on I2C{bus}, but sysfs node is missing: {eeprom}"

    def require_eeprom_access(self, operation):
        ok, detail = self.i2c1_status()
        if ok:
            return True
        self.popup(
            "EEPROM is not initialized\n\n"
            + detail
            + "\n\nOther board configuration functions remain available.",
            wait=True,
        )
        self.status = "EEPROM is not initialized"
        curses.beep()
        return False

    def edit_instance(self,key):
        try:
            if key=="serial_number":
                self.cfg.serial_number=int(self.prompt("Serial number",self.cfg.serial_number),0)
                if not (0 <= self.cfg.serial_number <= 0xffffffff):
                    raise ValueError("Serial number must be 0..4294967295")
            elif key=="mfg_date":
                default=self.cfg.mfg_date or datetime.date.today().isoformat()
                value=self.prompt("Mfg date YYYY-MM-DD",default)
                datetime.date.fromisoformat(value)
                self.cfg.mfg_date=value
            elif key=="mac_count":
                self.status="Use Generate MACs or View MACs"
        except Exception as e:
            self.status=f"Invalid value: {e}"; curses.beep()

    def generate_macs(self):
        if self.cfg.macs:
            if not self.confirm_yes("MAC addresses already exist. Generate new MAC addresses and replace them?"):
                self.status="MAC generation cancelled"; return
        # Locally administered unicast base; generate one random 40-bit suffix,
        # then allocate a contiguous block of 8 addresses.
        suffix=secrets.randbits(40)
        base=(0x02 << 40) | suffix
        # Clear multicast bit, set local-admin bit in first octet.
        first=((base >> 40) & 0xff)
        first=(first | 0x02) & 0xfe
        base=(first << 40) | (base & ((1 << 40)-1))
        # Avoid wrap into a different first octet.
        low=base & ((1 << 40)-1)
        if low > ((1 << 40)-MAX_MACS):
            low -= MAX_MACS
            base=(first << 40)|low
        self.cfg.macs=[(base+i).to_bytes(6,"big") for i in range(MAX_MACS)]
        self.status=f"Generated {MAX_MACS} MAC addresses"

    def view_macs(self):
        if not self.cfg.macs:
            self.popup("No MAC addresses generated",wait=True); return
        text="MAC addresses\n\n"+ "\n".join(
            f"{i+1}: {format_mac(mac)}" for i,mac in enumerate(self.cfg.macs)
        )
        self.popup(text,wait=True)

    def load_defaults(self):
        if not self.confirm_yes("Load default board configuration? Current BOARD CONFIG will be replaced."):
            self.status = "Load defaults cancelled"
            return
        self.cfg = BoardConfig(platform="rk3308", product_id=0, product_rev=1, board_name="", comment="",
                               enabled={"i2c1"}, rtc_i2c1="none",
                               serial_number=0, mfg_date="", macs=[])
        self.blob = None
        self.status = "Default board configuration loaded"

    def validate_downloaded_db(self, data):
        if not isinstance(data, dict):
            raise ValueError("YAML root must be a mapping")
        boards = data.get("boards")
        if not isinstance(boards, list):
            raise ValueError("YAML must contain a boards list")
        for n, board in enumerate(boards, start=1):
            if not isinstance(board, dict):
                raise ValueError(f"Board #{n} is not a mapping")
            for key in ("id", "rev", "name"):
                if key not in board:
                    raise ValueError(f"Board #{n} has no {key}")
            int(board["id"]); int(board["rev"])
        return len(boards)

    def load_db_github(self):
        try:
            self.status = "Downloading board DB from GitHub..."
            self.draw()
            req = urllib.request.Request(GITHUB_DB_URL, headers={"User-Agent": "napi-board-config/12"})
            with urllib.request.urlopen(req, timeout=15) as response:
                raw = response.read()
            data = yaml.safe_load(raw.decode("utf-8"))
            count = self.validate_downloaded_db(data)
            if not self.confirm_yes(f"Replace local board DB with {count} boards downloaded from GitHub?"):
                self.status = "GitHub DB update cancelled"
                return
            self.db.save(data)
            self.status = f"Local DB updated from GitHub: {count} boards; backup created"
        except Exception as e:
            self.status = f"GitHub DB update error: {e}"
            curses.beep()

    def read_eeprom(self):
        if not self.require_eeprom_access("load"): return
        if not self.confirm_yes("Load board configuration from EEPROM? Current BOARD CONFIG will be replaced."):
            self.status="EEPROM load cancelled"; return
        try:
            self.cfg=decode_config(self.eeprom_path.read_bytes())
            self.cfg.comment=""
            self.cfg.enabled.add("i2c1")
            self.blob=encode_config(self.cfg)
            self.status="EEPROM loaded successfully"
        except Exception as e:
            self.status=f"EEPROM load error: {e}"; curses.beep()

    def write_eeprom(self):
        if not self.require_eeprom_access("write"): return
        try:
            blob=encode_config(self.cfg)
            if not self.confirm_yes(f"WRITE EEPROM {self.eeprom_path}? ID={self.cfg.product_id}, rev={self.cfg.product_rev}."):
                self.status="EEPROM write cancelled"; return
            with open(self.eeprom_path,"r+b",buffering=0) as f:
                f.seek(0); f.write(blob); f.flush(); os.fsync(f.fileno())
            with open(self.eeprom_path,"rb",buffering=0) as f:
                verify=f.read(len(blob))
            decode_config(verify)
            self.blob=verify; self.status="EEPROM written and verified"
        except Exception as e:
            self.status=f"EEPROM write error: {e}"; curses.beep()

    def write_env(self):
        try:
            path=Path(ENV_TARGETS[self.env_target])
            pdef=platform_db().get(self.cfg.platform)
            prefix=str(pdef.get("overlay_prefix","")).strip()
            line="overlays="+" ".join(overlays_for(self.cfg))
            prefix_line="overlay_prefix="+prefix if prefix else None

            if not self.confirm_yes(f"WRITE boot file {path} with: {line}."):
                self.status="Boot file write cancelled"; return

            old=path.read_text(encoding="utf-8") if path.exists() else ""
            stamp=time.strftime("%Y%m%d-%H%M%S")
            backup=path.with_name(path.name+f".bak-{stamp}")
            if path.exists():
                shutil.copy2(path,backup)

            result=[]
            overlays_written=False
            prefix_written=False
            for ln in old.splitlines():
                if ln.startswith("overlay_prefix=") and prefix_line:
                    if not prefix_written:
                        result.append(prefix_line)
                        prefix_written=True
                    continue
                if ln.startswith("overlays="):
                    if not overlays_written:
                        result.append(line)
                        overlays_written=True
                    continue
                result.append(ln)

            if prefix_line and not prefix_written:
                result.append(prefix_line)
            if not overlays_written:
                result.append(line)

            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text("\n".join(result).rstrip()+"\n",encoding="utf-8")
            self.status=f"Written {path}; backup: {backup.name if old else 'none'}"
        except Exception as e:
            self.status=f"Boot file write error: {e}"; curses.beep()

    def choose_profile(self,title):
        db=self.db.load(); boards=db.get("boards",[])
        if not boards:
            self.popup("Local board database is empty."); return None
        pos=0
        while True:
            h,w=self.stdscr.getmaxyx()
            ph=min(max(8,len(boards)+5),max(8,h-2))
            pw=min(max(50, min(w-2, 78)), w-2)
            y0=max(0,(h-ph)//2); x0=max(0,(w-pw)//2)
            win=curses.newwin(ph,pw,y0,x0); win.keypad(True)
            win.erase(); win.box()
            try: win.addnstr(1,2,title,max(1,pw-4),curses.A_BOLD)
            except curses.error: pass
            visible=max(1,ph-4)
            top=max(0,min(pos-visible+1,max(0,len(boards)-visible)))
            for row,i in enumerate(range(top,min(len(boards),top+visible)),start=2):
                b=boards[i]
                text=f"ID={b.get('id')} rev={b.get('rev')}  {b.get('name')}"
                attr=curses.A_REVERSE if i==pos else curses.A_NORMAL
                try: win.addnstr(row,2,text,max(1,pw-4),attr)
                except curses.error: pass
            try: win.addnstr(ph-2,2,"Enter select   Esc/q cancel",max(1,pw-4),curses.A_DIM)
            except curses.error: pass
            win.refresh()
            ch=win.getch()
            if ch==3: raise KeyboardInterrupt
            if ch in (27,ord("q"),ord("Q")):
                self.status="Board load cancelled" if "Load" in title else "Selection cancelled"
                return None
            if ch in (curses.KEY_UP,ord("k")): pos=(pos-1)%len(boards)
            elif ch in (curses.KEY_DOWN,ord("j")): pos=(pos+1)%len(boards)
            elif ch in (10,13,curses.KEY_ENTER): return boards[pos]

    def profile_load(self):
        b=self.choose_profile("Load board from local DB")
        if b:
            serial,mfg,macs=self.cfg.serial_number,self.cfg.mfg_date,list(self.cfg.macs)
            self.cfg=self.db.to_config(b)
            self.cfg.serial_number,self.cfg.mfg_date,self.cfg.macs=serial,mfg,macs
            self.blob=None
            self.status=f"Loaded board: {self.cfg.board_name}"

    def profile_add(self):
        try:
            if not self.confirm_yes(f"Save board ID={self.cfg.product_id} rev={self.cfg.product_rev} to local DB?"):
                self.status="Local DB save cancelled"; return
            replaced=self.db.add_or_replace(self.cfg)
            self.status="Board updated in local DB (backup created)" if replaced else "Board added to local DB (backup created)"
        except Exception as e:
            self.status=f"Local DB error: {e}"; curses.beep()

    def profile_delete(self):
        b=self.choose_profile("Delete board from local DB")
        if not b: return
        if self.confirm_yes(f"Delete board ID={b.get('id')} rev={b.get('rev')} {b.get('name')} from local DB?"):
            ok=self.db.delete(b.get("id"),b.get("rev"))
            self.status="Board deleted (backup created)" if ok else "Board not found"
        else:
            self.status="Delete cancelled"

    def popup(self,text,wait=True):
        lines=text.splitlines(); h,w=self.stdscr.getmaxyx()
        ph=min(max(6,h-2),max(8,len(lines)+4))
        pw=min(max(20,w-2),max(52,min(max((len(x) for x in lines),default=40)+4,w-2)))
        ph=min(ph,h); pw=min(pw,w)
        y0=max(0,(h-ph)//2); x0=max(0,(w-pw)//2)
        win=curses.newwin(ph,pw,y0,x0); win.box()
        for i,line in enumerate(lines[:max(0,ph-3)]):
            try: win.addnstr(i+1,2,line,max(1,pw-4))
            except curses.error: pass
        if wait:
            try: win.addnstr(ph-2,2,"Press any key",max(1,pw-4),curses.A_DIM)
            except curses.error: pass
        win.refresh()
        if wait:
            ch=win.getch()
            if ch==3: raise KeyboardInterrupt

def main():
    p = argparse.ArgumentParser(description="NAPI board EEPROM/profile configurator")
    p.add_argument("--eeprom", default=DEFAULT_EEPROM)
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--dump", action="store_true")
    args = p.parse_args()

    if args.dump:
        cfg = decode_config(Path(args.eeprom).read_bytes())
        print(human_config(cfg))
        return

    try:
        curses.wrapper(lambda stdscr: UI(stdscr, args.eeprom, args.db).run())
    except KeyboardInterrupt:
        raise SystemExit(130)

if __name__ == "__main__":
    main()

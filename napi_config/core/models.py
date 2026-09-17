import struct
from dataclasses import dataclass, field

MAGIC = b"NAPI"
FORMAT_VERSION = 2
NAME_LEN = 32
MAX_MACS = 8
EEPROM_HEADER = struct.Struct("<4sBBIHIIBBBB32s")
EEPROM_SIZE = EEPROM_HEADER.size + MAX_MACS * 6 + 4
EEPROM_MAC_COUNT_OFFSET = 0x17
EEPROM_MAC_OFFSET = EEPROM_HEADER.size
EEPROM_CRC_OFFSET = EEPROM_SIZE - 4

@dataclass(frozen=True)
class InterfaceDef:
    key: str
    title: str
    bit: int

INTERFACES = [
    InterfaceDef("i2c0", "I2C0", 9),
    InterfaceDef("i2c1", "I2C1", 10),
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
    enabled: set[str] = field(default_factory=set)
    rtc_i2c1: str = "none"
    serial_number: int = 0
    mfg_date: str = ""
    macs: list[bytes] = field(default_factory=list)

    def mask(self) -> int:
        enabled = set(self.enabled)
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

        rtc = "none"
        for name, bit in RTC_BITS.items():
            if mask & (1 << bit):
                rtc = name
                break

        # Backward compatibility with v5 EEPROM: old rtc bit meant DS1338.
        if rtc == "none" and (mask & (1 << LEGACY_RTC_BIT)):
            rtc = "ds1338"

        return cls(platform="rk3308", product_id=product_id, product_rev=product_rev, board_name=board_name, comment="", enabled=enabled, rtc_i2c1=rtc)

import hashlib
from dataclasses import dataclass
from enum import IntEnum


OTP_ERROR = 'MAC generation failed: invalid or unavailable RK3308 OTP ID'
MAC_NAMESPACE = b'NAPI-MAC-v1\x00'
MAC_SOURCE = 'RK3308 OTP + SHA-256 (NAPI-MAC-v1)'


class MacSlot(IntEnum):
    NATIVE_ETHERNET = 0
    W5500_SPI1 = 1
    W5500_SPI2 = 2
    USB_ETHERNET_1 = 3
    USB_ETHERNET_2 = 4
    RESERVED_1 = 5
    RESERVED_2 = 6
    RESERVED_3 = 7


MAC_SLOT_LABELS = (
    'Native Ethernet', 'W5500 SPI1', 'W5500 SPI2',
    'USB Ethernet #1', 'USB Ethernet #2', 'Reserved', 'Reserved', 'Reserved',
)


def validate_otp(otp):
    if not isinstance(otp, bytes) or len(otp) != 5 or otp in (bytes(5), b'\xff' * 5):
        raise ValueError(OTP_ERROR)
    return otp


def generate_macs(otp):
    base = bytearray(hashlib.sha256(MAC_NAMESPACE + validate_otp(otp)).digest()[:5])
    base[4] &= 0xF8
    return [bytes([0x02, *base[:4], base[4] | slot]) for slot in MacSlot]


@dataclass(frozen=True)
class MacPolicy:
    set_native_eth_mac: bool = False

    @classmethod
    def from_document(cls, data):
        if not isinstance(data, dict) or set(data) - {'set_native_eth_mac'}:
            raise ValueError('Invalid MAC settings')
        value = data.get('set_native_eth_mac', False)
        if type(value) is not bool:
            raise ValueError('set_native_eth_mac must be a boolean')
        return cls(value)


def mac_assignments(macs, policy):
    # Slots are fixed, irrespective of enabled board interfaces.
    slots = [MacSlot.W5500_SPI1, MacSlot.W5500_SPI2,
             MacSlot.USB_ETHERNET_1, MacSlot.USB_ETHERNET_2]
    if policy.set_native_eth_mac:
        slots.insert(0, MacSlot.NATIVE_ETHERNET)
    return [{'slot': int(slot) + 1, 'purpose': MAC_SLOT_LABELS[slot],
             'mac': ':'.join(f'{byte:02X}' for byte in macs[slot])}
            for slot in slots if slot < len(macs)]

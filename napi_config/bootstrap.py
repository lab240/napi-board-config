from pathlib import Path
import sys
from .core.platforms import PlatformCatalog
from .core.service import BoardService
from .hardware.eeprom import LinuxEeprom
from .hardware.linux import LinuxProbe
from .hardware.boot import LinuxBootFiles
from .hardware.otp import LinuxOtp, DEFAULT_OTP
from .core.mac import MacPolicy
from .storage.files import YamlProfiles, JsonConfigurations, ProfileDownload, load_platforms, load_settings

ROOT = Path(__file__).resolve().parent.parent
if not (ROOT / 'platforms.yaml').exists():
    ROOT = Path(sys.prefix) / 'share' / 'napi-config'
DEFAULT_EEPROM = '/sys/bus/i2c/devices/1-0050/eeprom'
DEFAULT_DB = str(ROOT / 'boards.yaml')
DEFAULT_PLATFORMS = str(ROOT / 'platforms.yaml')
DEFAULT_SETTINGS = str(ROOT / 'settings.yaml')


def create_service(eeprom_path=DEFAULT_EEPROM, db_path=DEFAULT_DB, platforms_path=DEFAULT_PLATFORMS, boot_dir='/boot',
                   otp_path=DEFAULT_OTP, settings_path=DEFAULT_SETTINGS):
    return BoardService(PlatformCatalog(load_platforms(platforms_path)), LinuxEeprom(eeprom_path),
                        YamlProfiles(db_path), LinuxBootFiles(boot_dir), JsonConfigurations(), LinuxProbe(eeprom_path),
                        ProfileDownload(), LinuxOtp(otp_path), MacPolicy.from_document(load_settings(settings_path)))

import errno
import fcntl
import os
import time

from ..core.mac import OTP_ERROR


DEFAULT_OTP = '/sys/bus/nvmem/devices/rockchip-otp0/nvmem'
OTP_OFFSET = 20
OTP_LENGTH = 5


class LinuxOtp:
    def __init__(self, path=DEFAULT_OTP, timeout=2.0):
        self.path = path
        self.timeout = timeout

    def read_id(self):
        try:
            with open(self.path, 'rb', buffering=0) as stream:
                deadline = time.monotonic() + self.timeout
                while True:
                    try:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except OSError as exc:
                        if exc.errno not in (errno.EAGAIN, errno.EACCES):
                            raise
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError('RK3308 OTP lock timeout') from exc
                        time.sleep(min(0.02, remaining))
                try:
                    os.lseek(stream.fileno(), OTP_OFFSET, os.SEEK_SET)
                    otp = os.read(stream.fileno(), OTP_LENGTH)
                    if len(otp) != OTP_LENGTH:
                        raise ValueError(OTP_ERROR)
                    return otp
                finally:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        except (OSError, ValueError) as exc:
            raise ValueError(OTP_ERROR) from exc

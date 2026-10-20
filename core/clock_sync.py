from __future__ import annotations

import socket
import struct
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .errors import BiliRewardError

NTP_EPOCH = 2208988800
BEIJING = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class TimeSample:
    epoch: float
    monotonic: float
    offset: float
    round_trip: float


def _timestamp(value: float) -> bytes:
    seconds = value + NTP_EPOCH
    return struct.pack("!II", int(seconds) & 0xFFFFFFFF, int((seconds % 1) * 2**32))


def query_ntp(server: str, timeout: float = 2.0) -> TimeSample:
    """One SNTP exchange, with origin, leap, stratum and timing validation."""
    try:
        addresses = socket.getaddrinfo(server, 123, type=socket.SOCK_DGRAM)
        family, kind, protocol, _, address = addresses[0]
        with socket.socket(family, kind, protocol) as sock:
            sock.settimeout(timeout)
            sock.connect(address)
            t1, m1 = time.time(), time.monotonic()
            packet = bytearray(48)
            packet[0] = 0x23  # Version 4, client mode.
            packet[40:48] = _timestamp(t1)
            sock.send(packet)
            response = sock.recv(512)
            t4, m4 = time.time(), time.monotonic()
    except (OSError, IndexError) as exc:
        raise BiliRewardError(
            f"[校时] 无法连接 {server}: {type(exc).__name__}"
        ) from exc
    if len(response) < 48:
        raise BiliRewardError("[校时] NTP 返回长度不足")
    leap, version, mode = response[0] >> 6, (response[0] >> 3) & 7, response[0] & 7
    if leap == 3 or version not in (3, 4) or mode != 4 or not 1 <= response[1] <= 15:
        raise BiliRewardError("[校时] NTP 服务器未同步、拒绝服务或返回模式无效")
    if response[24:32] != packet[40:48]:
        raise BiliRewardError("[校时] NTP 请求标识不匹配")
    if abs((t4 - t1) - (m4 - m1)) > 0.25:
        raise BiliRewardError("[校时] 采样期间系统时间发生跳变，请重试")

    def decode(raw: bytes) -> float:
        seconds, fraction = struct.unpack("!II", raw)
        if seconds == 0 and fraction == 0:
            raise BiliRewardError("[校时] NTP 时间戳为空")
        # Select the closest NTP era, including the 2036 wraparound.
        era = round((t4 + NTP_EPOCH - seconds) / 2**32)
        return seconds + era * 2**32 - NTP_EPOCH + fraction / 2**32

    t2, t3 = decode(response[32:40]), decode(response[40:48])
    round_trip = (m4 - m1) - (t3 - t2)
    if t3 < t2 or round_trip < -0.002 or round_trip > timeout:
        raise BiliRewardError("[校时] NTP 往返时间无效")
    offset = ((t2 - t1) + (t3 - t4)) / 2
    return TimeSample(t4 + offset, m4, offset, max(0.0, round_trip))


class NetworkClock:
    """Application-only correction; never changes the Windows system clock."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sample: TimeSample | None = None

    def now(self) -> float:
        with self._lock:
            sample = self._sample
        return (
            time.time()
            if sample is None
            else sample.epoch + time.monotonic() - sample.monotonic
        )

    def age(self) -> float:
        with self._lock:
            sample = self._sample
        return float("inf") if sample is None else time.monotonic() - sample.monotonic

    def sync(self, server: str, timeout: float = 2.0) -> TimeSample:
        sample = query_ntp(server, timeout)
        with self._lock:
            self._sample = sample
        return sample


def next_scheduled_time(now: float, time_of_day: str, delay_ms: int) -> float:
    current = datetime.fromtimestamp(now, BEIJING)
    hour, minute, second = map(int, time_of_day.split(":"))
    target = current.replace(hour=hour, minute=minute, second=second, microsecond=0)
    target += timedelta(milliseconds=delay_ms)
    if target.timestamp() <= now:
        target += timedelta(days=1)
    return target.timestamp()

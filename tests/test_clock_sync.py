import socket
import unittest
from datetime import datetime
from unittest.mock import Mock, patch

from core.clock_sync import (
    BEIJING,
    NetworkClock,
    TimeSample,
    _timestamp,
    next_scheduled_time,
    query_ntp,
)
from core.errors import BiliRewardError


class ClockTests(unittest.TestCase):
    def exchange(self, mutate=lambda data: data, base=1700000000.0):
        sock = Mock()
        sock.__enter__ = Mock(return_value=sock)
        sock.__exit__ = Mock(return_value=False)

        def response(*args):
            data = bytearray(48)
            data[0], data[1] = 0x24, 2
            data[24:32] = sock.send.call_args.args[0][40:48]
            data[32:40] = _timestamp(base + 0.25)
            data[40:48] = _timestamp(base + 0.25)
            return bytes(mutate(data))

        sock.recv.side_effect = response
        with (
            patch(
                "core.clock_sync.socket.getaddrinfo",
                return_value=[
                    (socket.AF_INET, socket.SOCK_DGRAM, 17, "", ("127.0.0.1", 123))
                ],
            ),
            patch("core.clock_sync.socket.socket", return_value=sock),
            patch("core.clock_sync.time.time", side_effect=[base, base + 0.1]),
            patch("core.clock_sync.time.monotonic", side_effect=[10.0, 10.1]),
        ):
            return query_ntp("test.example", 2)

    def test_ntp_offset_and_round_trip(self):
        sample = self.exchange()
        self.assertAlmostEqual(sample.offset, 0.2, places=5)
        self.assertAlmostEqual(sample.round_trip, 0.1, places=5)

    def test_ntp_era_after_2036(self):
        sample = self.exchange(base=datetime(2036, 2, 8, tzinfo=BEIJING).timestamp())
        self.assertAlmostEqual(sample.offset, 0.2, places=5)

    def test_rejects_bad_origin(self):
        def mutate(data):
            data[24] ^= 1
            return data

        with self.assertRaisesRegex(BiliRewardError, "标识"):
            self.exchange(mutate)

    def test_rejects_unsynced_and_rate_limited_servers(self):
        for header, stratum in ((0xE4, 2), (0x24, 0), (0x23, 2)):

            def mutate(data):
                data[0], data[1] = header, stratum
                return data

            with self.assertRaises(BiliRewardError):
                self.exchange(mutate)

    def test_rejects_truncated_packet(self):
        with self.assertRaises(BiliRewardError):
            self.exchange(lambda data: data[:20])

    def test_clock_uses_monotonic_after_sync(self):
        clock = NetworkClock()
        with patch(
            "core.clock_sync.query_ntp", return_value=TimeSample(100, 10, 0.25, 0.1)
        ):
            clock.sync("test.example")
        with (
            patch("core.clock_sync.time.monotonic", return_value=12),
            patch("core.clock_sync.time.time", return_value=9000),
        ):
            self.assertEqual(clock.now(), 102)
            self.assertEqual(clock.age(), 2)

    def test_midnight_is_next_beijing_day_with_100ms_delay(self):
        now = datetime(2026, 10, 20, 10, tzinfo=BEIJING).timestamp()
        target = next_scheduled_time(now, "00:00:00", 100)
        expected = datetime(2026, 10, 21, tzinfo=BEIJING).timestamp() + 0.1
        self.assertAlmostEqual(target, expected, places=5)

    def test_start_inside_delay_window_uses_today(self):
        midnight = datetime(2026, 10, 20, tzinfo=BEIJING).timestamp()
        self.assertAlmostEqual(
            next_scheduled_time(midnight + 0.05, "00:00:00", 100), midnight + 0.1
        )
        self.assertAlmostEqual(
            next_scheduled_time(midnight + 0.11, "00:00:00", 100),
            midnight + 86400 + 0.1,
        )

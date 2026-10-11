"""Tests for matching Modbus replies to their request.

Run from the repository root:  python -m unittest discover tests
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from utils import modbus_crc16, find_modbus_frame  # noqa: E402
from parsers import parse_controller_charging_info  # noqa: E402
from data_validator import DataValidator  # noqa: E402


def frame(addr, words, payload=None, func=0x03):
    payload = payload if payload is not None else bytes(range(2 * words))
    body = bytes([addr, func, 2 * words]) + payload
    lo, hi = modbus_crc16(body)
    return body + bytes([lo, hi])


class FrameMatching(unittest.TestCase):
    def test_plain_reply(self):
        reply = frame(0xFF, 34)
        self.assertEqual(find_modbus_frame(reply, 255, 34), reply)

    def test_late_reply_in_front_is_skipped(self):
        stale = frame(0xFF, 1, b"\x00\x10")  # late reply to register 26
        reply = frame(0xFF, 34)
        self.assertEqual(find_modbus_frame(stale + reply, 255, 34), reply)

    def test_incomplete_reply_waits(self):
        self.assertIsNone(find_modbus_frame(frame(0xFF, 34)[:40], 255, 34))

    def test_other_device_on_hub_is_ignored(self):
        inverter = frame(32, 10)
        battery = frame(48, 10, bytes(20))
        self.assertEqual(find_modbus_frame(inverter + battery, 48, 10), battery)
        self.assertIsNone(find_modbus_frame(inverter, 48, 10))

    def test_exception_reply(self):
        body = bytes([48, 0x83, 2])
        exc = body + bytes(modbus_crc16(body))
        self.assertEqual(find_modbus_frame(exc, 48, 10), exc)

    def test_bad_crc_rejected(self):
        bad = bytearray(frame(0xFF, 34))
        bad[10] ^= 1
        self.assertIsNone(find_modbus_frame(bytes(bad), 255, 34))

    def test_regression_17920_wh(self):
        # 2026-10-10 23:45: the reg-26 reply arrived late and the charging
        # block was parsed one byte off: 70 W became 17920 Wh generated.
        payload = bytearray(68)
        payload[30:32] = (70).to_bytes(2, "big")    # max_charging_power_today
        payload[38:40] = (192).to_bytes(2, "big")   # power_generation_today
        buffer = frame(0xFF, 1, b"\x00\x10") + frame(0xFF, 34, bytes(payload))
        self.assertEqual(parse_controller_charging_info(buffer)["power_generation_today"], 17920)
        parsed = parse_controller_charging_info(find_modbus_frame(buffer, 255, 34))
        self.assertEqual(parsed["power_generation_today"], 192)
        self.assertEqual(parsed["max_charging_power_today"], 70)


class ValidatorStepChange(unittest.TestCase):
    def test_new_level_accepted_after_three_readings(self):
        v = DataValidator("Rover", "controller")
        out = [v.validate_data({"pv_voltage": x})[0]["pv_voltage"]
               for x in (0, 0, 19, 19.2, 19.1, 19.3)]
        self.assertEqual(out, [0, 0, 0, 0, 19.1, 19.3])

    def test_one_off_spike_still_rejected(self):
        v = DataValidator("Rover", "controller")
        out = [v.validate_data({"power_generation_total": x})[0]["power_generation_total"]
               for x in (792383, 15346944, 792390)]
        self.assertEqual(out, [792383, 792383, 792390])


if __name__ == "__main__":
    unittest.main()

"""Parser tests against register dumps read from real devices (2026-10-11):
Rover 40 (BT-1), RBT100LFP12SH-G1 battery and RINVTPGH110111S inverter (BT-2 hub).

Run from the repository root:  python -m unittest discover tests
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from utils import modbus_crc16  # noqa: E402
from parsers import DeviceType, parse_response  # noqa: E402


def frame(addr, hexdata):
    payload = bytes.fromhex(hexdata)
    body = bytes([addr, 0x03, len(payload)]) + payload
    return body + bytes(modbus_crc16(body))


ROVER_0100 = ("003e007d00e40b0600000000000000bd009e001e0000007b007d01040052001b0001000200010018000c"
              "0a6300434aae0000ea2f00002da4000c17570002350700020000")
ROVER_E004 = "000400a0009b0090009000900084007e007d0073006a"
ROVER_E002 = "00c80c00" + ROVER_E004
BATTERY_5200 = ("00250023001e0019022601f4003200002ee0271017700094008e00780064025801f4ff9cff06"
                "c568cd38d508")
BATTERY_5042 = "0050007d00002ace0001825e0147009400641388d8f0"
BATTERY_5035 = "0000000100460000000100460000"
BATTERY_5110 = ("505054414830313035303330363233313031303030313032"
                "5242543130304c4650313253482d4731" "30313139")


class Controller(unittest.TestCase):
    def test_charging_info(self):
        d = parse_response(DeviceType.CONTROLLER, 256, frame(0xFF, ROVER_0100))
        self.assertEqual(d["battery_percentage"], 62)
        self.assertEqual(d["battery_voltage"], 12.5)
        self.assertEqual(d["pv_power"], 30)
        self.assertEqual(d["battery_min_voltage_today"], 12.3)
        self.assertEqual(d["battery_max_voltage_today"], 12.5)
        self.assertEqual(d["max_charging_current_today"], 2.6)
        self.assertEqual(d["max_discharging_current_today"], 0.82)
        self.assertEqual(d["power_generation_today"], 24)
        self.assertEqual(d["operating_days"], 2659)
        self.assertEqual(d["over_discharge_count"], 67)
        self.assertEqual(d["charging_amp_hours_total"], 59951)
        self.assertEqual(d["discharging_amp_hours_total"], 11684)
        self.assertEqual(d["power_generation_total"], 792407)
        self.assertEqual(d["power_consumption_total"], 144647)
        self.assertEqual(d["charging_status"], "mppt")

    def test_settings(self):
        d = parse_response(DeviceType.CONTROLLER, 57348, frame(0xFF, ROVER_E004))
        self.assertEqual(d["battery_type"], "lithium")
        self.assertEqual(d["overvoltage_threshold"], 16.0)
        self.assertEqual(d["boost_voltage"], 14.4)
        self.assertEqual(d["float_voltage"], 14.4)
        self.assertEqual(d["over_discharge_voltage"], 11.5)
        self.assertEqual(d["discharge_limit_voltage"], 10.6)

    def test_settings_from_e002(self):
        d = parse_response(DeviceType.CONTROLLER, 57346, frame(0xFF, ROVER_E002))
        self.assertEqual(d["nominal_battery_capacity"], 200)
        self.assertEqual(d["system_voltage"], 12)
        self.assertEqual(d["battery_type"], "lithium")
        self.assertEqual(d["boost_voltage"], 14.4)
        self.assertEqual(d["discharge_limit_voltage"], 10.6)

    def test_versions(self):
        d = parse_response(DeviceType.CONTROLLER, 20, frame(0xFF, "0003000100000002"))
        self.assertEqual(d, {"firmware_version": "3.0.1", "hardware_version": "0.0.2"})


class Battery(unittest.TestCase):
    def test_info_with_cycles_and_limits(self):
        d = parse_response(DeviceType.BATTERY, 5042, frame(48, BATTERY_5042))
        self.assertEqual(d["current"], 0.8)
        self.assertEqual(d["voltage"], 12.5)
        self.assertEqual(d["cycle_count"], 327)
        self.assertEqual(d["charge_voltage_limit"], 14.8)
        self.assertEqual(d["discharge_voltage_limit"], 10.0)
        self.assertEqual(d["charge_current_limit"], 50.0)
        self.assertEqual(d["discharge_current_limit"], 100.0)

    def test_more_temperatures(self):
        d = parse_response(DeviceType.BATTERY, 5035, frame(48, BATTERY_5035))
        self.assertEqual(d, {"environment_temperature": 7.0, "heater_temperature": 7.0})

    def test_device_info(self):
        d = parse_response(DeviceType.BATTERY, 5110, frame(48, BATTERY_5110))
        self.assertEqual(d["serial_number"], "PPTAH01050306231")
        self.assertEqual(d["model"], "RBT100LFP12SH-G1")
        self.assertEqual(d["firmware_version"], "0119")


    def test_limits(self):
        d = parse_response(DeviceType.BATTERY, 5200, frame(48, BATTERY_5200))
        self.assertEqual(d["cell_over_voltage_limit"], 3.7)
        self.assertEqual(d["cell_high_voltage_limit"], 3.5)
        self.assertEqual(d["cell_under_voltage_limit"], 2.5)
        self.assertEqual(d["charge_low_temperature_limit"], 5.0)
        self.assertEqual(d["charge_under_temperature_limit"], 0.0)
        self.assertEqual(d["module_high_voltage_limit"], 14.2)
        self.assertEqual(d["discharge_low_temperature_limit"], -10.0)
        self.assertEqual(d["discharge_under_temperature_limit"], -25.0)
        self.assertEqual(d["discharge_over2_current_limit"], 150.0)
        self.assertEqual(d["discharge_high_current_limit"], 110.0)

    def test_status_now(self):
        # read 2026-10-11: MOSFETs on, using battery power, charge+discharge enabled
        d = parse_response(DeviceType.BATTERY, 5100, frame(48, "000000000000000000000000000e0000000000c0"))
        self.assertEqual((d["alarm_count"], d["warning_count"]), (0, 0))
        self.assertEqual((d["charge_mosfet"], d["discharge_mosfet"]), ("on", "on"))
        self.assertTrue(d["charge_enabled"] and d["discharge_enabled"])

    def test_warnings_are_in_5107_and_cell_errors_in_5108(self):
        # 5107 bit1 = cell high voltage warning; 5108 bit2 = cell 3 voltage error
        d = parse_response(DeviceType.BATTERY, 5100, frame(48, "0000000000000000000000000000000200040000"))
        self.assertEqual(d["warnings"], ["cell_high_voltage"])
        self.assertEqual(d["cell_voltage_alarms"], ["cell_3_voltage_error"])

    def test_other_alarm_bit_positions(self):
        # 5104-5105: bits 25-24 = heater temperature 1, 10 = above higher limit
        d = parse_response(DeviceType.BATTERY, 5100, frame(48, "0000000000000000020000000000000000000000"))
        self.assertEqual(d["protection_alarms"], ["heater_temp_1_high"])


class Inverter(unittest.TestCase):
    def test_mode(self):
        self.assertEqual(parse_response(DeviceType.INVERTER, 4102, frame(32, "0001")),
                         {"inverter_mode": "normal"})


if __name__ == "__main__":
    unittest.main()

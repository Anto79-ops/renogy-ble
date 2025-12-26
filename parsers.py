"""
Renogy device data parsers.
Contains register definitions and parsing logic for:
- Rover/Wanderer charge controllers (BT-1)
- LiFePO4 batteries (BT-2)
- Inverters (BT-2)
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Callable
from enum import Enum

from utils import bytes_to_int, bytes_to_ascii, parse_temperature

logger = logging.getLogger(__name__)


class DeviceType(Enum):
    CONTROLLER = "controller"
    BATTERY = "battery"
    INVERTER = "inverter"


# ============================================================================
# CHARGE CONTROLLER (Rover/Wanderer) - BT-1/BT-2 Module
# Register map based on Renogy SRNE protocol
# ============================================================================

CONTROLLER_CHARGING_STATE = {
    0: 'deactivated',
    1: 'activated',
    2: 'mppt',
    3: 'equalizing',
    4: 'boost',
    5: 'floating',
    6: 'current_limiting'
}

CONTROLLER_LOAD_STATE = {
    0: 'off',
    1: 'on'
}

CONTROLLER_BATTERY_TYPE = {
    1: 'open',
    2: 'sealed',
    3: 'gel',
    4: 'lithium',
    5: 'custom'
}

# Controller register sections to read
CONTROLLER_REGISTERS = [
    {'name': 'device_info', 'register': 12, 'words': 8},
    {'name': 'device_id', 'register': 26, 'words': 1},
    {'name': 'charging_info', 'register': 256, 'words': 34},
    {'name': 'faults', 'register': 289, 'words': 2},  # 0x0121-0x0122: Fault and warning bits
    {'name': 'battery_type', 'register': 57348, 'words': 1},
    # Historical data (optional)
    {'name': 'historical', 'register': 60000, 'words': 21},
]


def parse_controller_device_info(data: bytes, offset: int = 3) -> Dict[str, Any]:
    """Parse controller device info (registers 12-19)."""
    result = {}
    if len(data) < offset + 16:
        return result
    
    # Model name (8 bytes ASCII starting at offset)
    result['model'] = bytes_to_ascii(data, offset, 16).strip('\x00')
    return result


def parse_controller_device_id(data: bytes, offset: int = 3) -> Dict[str, Any]:
    """Parse controller device ID (register 26)."""
    result = {}
    if len(data) < offset + 2:
        return result
    result['device_id'] = bytes_to_int(data, offset, 1)
    return result


def parse_controller_charging_info(data: bytes, offset: int = 3) -> Dict[str, Any]:
    """
    Parse controller charging info (registers 256-289).
    This is the main data section with battery, PV, load, and controller info.
    """
    result = {}
    if len(data) < offset + 68:
        logger.warning(f"Charging info data too short: {len(data)} bytes")
        return result
    
    # Battery data
    result['battery_percentage'] = bytes_to_int(data, offset + 0, 2)
    result['battery_voltage'] = bytes_to_int(data, offset + 2, 2, scale=0.1)
    result['battery_current'] = bytes_to_int(data, offset + 4, 2, scale=0.01)
    
    # Temperature handling (can be signed)
    battery_temp_raw = bytes_to_int(data, offset + 7, 1)
    controller_temp_raw = bytes_to_int(data, offset + 6, 1)
    result['battery_temperature'] = parse_temperature(battery_temp_raw)
    result['controller_temperature'] = parse_temperature(controller_temp_raw)
    
    # Load data
    result['load_voltage'] = bytes_to_int(data, offset + 8, 2, scale=0.1)
    result['load_current'] = bytes_to_int(data, offset + 10, 2, scale=0.01)
    result['load_power'] = bytes_to_int(data, offset + 12, 2)
    
    # PV (Solar panel) data
    result['pv_voltage'] = bytes_to_int(data, offset + 14, 2, scale=0.1)
    result['pv_current'] = bytes_to_int(data, offset + 16, 2, scale=0.01)
    result['pv_power'] = bytes_to_int(data, offset + 18, 2)
    
    # Daily statistics
    result['max_charging_power_today'] = bytes_to_int(data, offset + 30, 2)
    result['max_discharging_power_today'] = bytes_to_int(data, offset + 32, 2)
    result['charging_amp_hours_today'] = bytes_to_int(data, offset + 34, 2)
    result['discharging_amp_hours_today'] = bytes_to_int(data, offset + 36, 2)
    result['power_generation_today'] = bytes_to_int(data, offset + 38, 2)
    result['power_consumption_today'] = bytes_to_int(data, offset + 40, 2)
    
    # Cumulative totals (4 bytes)
    result['power_generation_total'] = bytes_to_int(data, offset + 56, 4)
    
    # Status
    load_status_byte = bytes_to_int(data, offset + 64, 1)
    result['load_status'] = CONTROLLER_LOAD_STATE.get((load_status_byte >> 7) & 1, 'unknown')
    
    charging_status_byte = bytes_to_int(data, offset + 65, 1)
    result['charging_status'] = CONTROLLER_CHARGING_STATE.get(charging_status_byte, 'unknown')
    
    return result


def parse_controller_battery_type(data: bytes, offset: int = 3) -> Dict[str, Any]:
    """Parse controller battery type (register 57348 / 0xE004)."""
    result = {}
    if len(data) < offset + 2:
        return result
    
    battery_type_val = bytes_to_int(data, offset, 2)
    result['battery_type'] = CONTROLLER_BATTERY_TYPE.get(battery_type_val, 'unknown')
    return result


def parse_controller_faults(data: bytes, offset: int = 3) -> Dict[str, Any]:
    """
    Parse controller fault and warning information (registers 0x0121-0x0122 / 289-290).
    
    This is a 32-bit value where each bit represents a specific fault/warning:
    - Bit 31: Reserved
    - Bit 30: Charge MOS short circuit
    - Bit 29: Anti-reverse MOS short circuit
    - Bit 28: Solar panel reversely connected
    - Bit 27: Solar panel working point over-voltage
    - Bit 26: Solar panel counter-current
    - Bit 25: PV input side over-voltage
    - Bit 24: PV input side short circuit
    - Bit 23: PV input overpower
    - Bit 22: Ambient temperature too high
    - Bit 21: Controller temperature too high
    - Bit 20: Load overpower/over-current
    - Bit 19: Load short circuit
    - Bit 18: Battery under-voltage warning
    - Bit 17: Battery over-voltage
    - Bit 16: Battery over-discharge
    - Bits 0-15: Reserved
    """
    result = {
        'faults': [],
        'warnings': [],
        'fault_count': 0,
        'warning_count': 0
    }
    
    if len(data) < offset + 4:
        logger.warning(f"Fault data too short: {len(data)} bytes")
        return result
    
    # Read 4 bytes (2 registers) as 32-bit value
    # High word first (register 0x0121), then low word (register 0x0122)
    high_word = bytes_to_int(data, offset, 2)
    low_word = bytes_to_int(data, offset + 2, 2)
    fault_bits = (high_word << 16) | low_word
    
    # Parse fault bits (high word - bits 16-31)
    # Bit 30: Charge MOS short circuit
    if fault_bits & (1 << 30):
        result['faults'].append('charge_mos_short_circuit')
    
    # Bit 29: Anti-reverse MOS short circuit
    if fault_bits & (1 << 29):
        result['faults'].append('anti_reverse_mos_short')
    
    # Bit 28: Solar panel reversely connected
    if fault_bits & (1 << 28):
        result['faults'].append('solar_panel_reversed')
    
    # Bit 27: Solar panel working point over-voltage
    if fault_bits & (1 << 27):
        result['faults'].append('pv_working_point_overvoltage')
    
    # Bit 26: Solar panel counter-current
    if fault_bits & (1 << 26):
        result['faults'].append('pv_counter_current')
    
    # Bit 25: PV input side over-voltage
    if fault_bits & (1 << 25):
        result['faults'].append('pv_input_overvoltage')
    
    # Bit 24: PV input side short circuit
    if fault_bits & (1 << 24):
        result['faults'].append('pv_input_short_circuit')
    
    # Bit 23: PV input overpower
    if fault_bits & (1 << 23):
        result['faults'].append('pv_input_overpower')
    
    # Bit 22: Ambient temperature too high
    if fault_bits & (1 << 22):
        result['faults'].append('ambient_temp_too_high')
    
    # Bit 21: Controller temperature too high
    if fault_bits & (1 << 21):
        result['faults'].append('controller_temp_too_high')
    
    # Bit 20: Load overpower or over-current
    if fault_bits & (1 << 20):
        result['faults'].append('load_overpower')
    
    # Bit 19: Load short circuit
    if fault_bits & (1 << 19):
        result['faults'].append('load_short_circuit')
    
    # Bit 18: Battery under-voltage warning (this is a warning, not a fault)
    if fault_bits & (1 << 18):
        result['warnings'].append('battery_undervoltage')
    
    # Bit 17: Battery over-voltage
    if fault_bits & (1 << 17):
        result['faults'].append('battery_overvoltage')
    
    # Bit 16: Battery over-discharge
    if fault_bits & (1 << 16):
        result['faults'].append('battery_over_discharge')
    
    result['fault_count'] = len(result['faults'])
    result['warning_count'] = len(result['warnings'])
    
    # Log for debugging
    if fault_bits != 0:
        logger.debug(f"Controller fault bits: 0x{fault_bits:08X}, faults: {result['faults']}, warnings: {result['warnings']}")
    
    return result


def parse_controller_historical(data: bytes, offset: int = 3) -> Dict[str, Any]:
    """Parse controller historical data (7 days)."""
    result = {}
    if len(data) < offset + 42:
        return result
    
    # 7 days of power generation (Wh)
    daily_generation = []
    for i in range(7):
        val = bytes_to_int(data, offset + i * 2, 2)
        daily_generation.append(val)
    result['daily_power_generation'] = daily_generation
    
    # 7 days of charge Ah
    daily_charge_ah = []
    for i in range(7):
        val = bytes_to_int(data, offset + 14 + i * 2, 2)
        daily_charge_ah.append(val)
    result['daily_charge_ah'] = daily_charge_ah
    
    # 7 days of max power
    daily_max_power = []
    for i in range(7):
        val = bytes_to_int(data, offset + 28 + i * 2, 2)
        daily_max_power.append(val)
    result['daily_max_power'] = daily_max_power
    
    return result


# ============================================================================
# BATTERY (LiFePO4) - BT-2 Module
# Register map based on Renogy/RongSi BMS Modbus Protocol V1.7
# Reference: cyrils/renogy-bt BatteryClient.py
# ============================================================================

BATTERY_REGISTERS = [
    {'name': 'cell_info', 'register': 5000, 'words': 17},      # Cell voltages and count
    {'name': 'temp_info', 'register': 5017, 'words': 17},      # Temperature sensors
    {'name': 'battery_info', 'register': 5042, 'words': 8},    # Current, voltage, capacity
    {'name': 'status_info', 'register': 5100, 'words': 10},    # Alarms and status flags  
    {'name': 'device_info', 'register': 5122, 'words': 8},     # Battery name/model
]


def parse_battery_cell_info(data: bytes, offset: int = 3) -> Dict[str, Any]:
    """
    Parse battery cell information (registers 5000-5016).
    Includes cell count and cell voltages.
    """
    result = {}
    if len(data) < offset + 4:
        return result
    
    # Cell count (register 5000)
    cell_count = bytes_to_int(data, offset, 2)
    result['cell_count'] = min(cell_count, 16)  # Max 16 cells
    
    # Cell voltages (registers 5001-5016, 0.1V units)
    cell_voltages = []
    for i in range(result['cell_count']):
        if offset + 2 + i * 2 + 2 <= len(data):
            voltage = bytes_to_int(data, offset + 2 + i * 2, 2, scale=0.1)
            cell_voltages.append(round(voltage, 2))
    result['cell_voltages'] = cell_voltages
    
    return result


def parse_battery_temp_info(data: bytes, offset: int = 3) -> Dict[str, Any]:
    """
    Parse battery temperature information (registers 5017-5033).
    Includes temperature sensor count and values.
    """
    result = {}
    if len(data) < offset + 4:
        return result
    
    # Temperature sensor count (register 5017)
    temp_count = bytes_to_int(data, offset, 2)
    result['temperature_count'] = min(temp_count, 8)
    
    # Temperature values (0.1°C scale, signed)
    temperatures = []
    for i in range(result['temperature_count']):
        if offset + 2 + i * 2 + 2 <= len(data):
            temp_raw = bytes_to_int(data, offset + 2 + i * 2, 2, signed=True)
            temperatures.append(round(temp_raw * 0.1, 1))
    result['temperatures'] = temperatures
    
    # Use first temperature as main battery temperature
    if temperatures:
        result['battery_temperature'] = temperatures[0]
    
    return result


def parse_battery_info(data: bytes, offset: int = 3) -> Dict[str, Any]:
    """
    Parse battery main information (registers 5042-5049).
    Current, voltage, remaining/total capacity.
    """
    result = {}
    if len(data) < offset + 12:
        logger.warning(f"Battery info data too short: {len(data)} bytes (need {offset + 12})")
        return result
    
    # Current (register 5042, 0.01A, signed)
    result['current'] = bytes_to_int(data, offset, 2, scale=0.01, signed=True)
    
    # Module voltage (register 5043, 0.1V)
    result['voltage'] = bytes_to_int(data, offset + 2, 2, scale=0.1)
    
    # Remaining capacity (registers 5044-5045, 0.001Ah -> Ah)
    result['remaining_capacity'] = bytes_to_int(data, offset + 4, 4, scale=0.001)
    
    # Total capacity (registers 5046-5047, 0.001Ah -> Ah)
    result['total_capacity'] = bytes_to_int(data, offset + 8, 4, scale=0.001)
    
    # SOC calculation
    if result.get('total_capacity', 0) > 0:
        result['soc'] = round((result.get('remaining_capacity', 0) / result['total_capacity']) * 100, 1)
    else:
        result['soc'] = 0
    
    # Power calculation (W)
    result['power'] = round(result.get('voltage', 0) * result.get('current', 0), 1)
    
    return result


def parse_battery_alarm_info(data: bytes, offset: int = 3) -> Dict[str, Any]:
    """
    Parse battery alarm/status flags (registers 5100-5109).
    Contains cell voltage alarms, temperature alarms, protection status, and heater status.
    
    Alarm codes:
    - 00: normal
    - 01: below lower limit (protection triggered)
    - 10: above higher limit (protection triggered)
    - 11: other alarm
    """
    result = {}
    result['cell_voltage_alarms'] = []
    result['cell_temperature_alarms'] = []
    result['protection_alarms'] = []
    result['warnings'] = []
    
    if len(data) < offset + 20:
        logger.warning(f"Battery alarm data too short: {len(data)} bytes")
        result['alarm_count'] = 0
        result['warning_count'] = 0
        return result
    
    # Registers 5100-5101 (4 bytes) - Cell Voltage Alarm Info
    # Each cell uses 2 bits: Bit[2n+1:2n] for cell n+1
    cell_voltage_alarm = bytes_to_int(data, offset, 4)
    for cell in range(16):
        alarm_code = (cell_voltage_alarm >> (cell * 2)) & 0x03
        if alarm_code == 1:
            result['cell_voltage_alarms'].append(f'cell_{cell+1}_undervoltage')
        elif alarm_code == 2:
            result['cell_voltage_alarms'].append(f'cell_{cell+1}_overvoltage')
        elif alarm_code == 3:
            result['cell_voltage_alarms'].append(f'cell_{cell+1}_alarm')
    
    # Registers 5102-5103 (4 bytes) - Cell Temperature Alarm Info
    cell_temp_alarm = bytes_to_int(data, offset + 4, 4)
    for cell in range(16):
        alarm_code = (cell_temp_alarm >> (cell * 2)) & 0x03
        if alarm_code == 1:
            result['cell_temperature_alarms'].append(f'cell_{cell+1}_undertemp')
        elif alarm_code == 2:
            result['cell_temperature_alarms'].append(f'cell_{cell+1}_overtemp')
        elif alarm_code == 3:
            result['cell_temperature_alarms'].append(f'cell_{cell+1}_temp_alarm')
    
    # Registers 5104-5105 (4 bytes) - Other Alarm Info
    # BMS board temp, environment temps, heater temps, charge/discharge current
    other_alarm = bytes_to_int(data, offset + 8, 4)
    alarm_names = [
        ('bms_board_temp', 0), ('bms_board_temp', 2),
        ('env_temp_1', 4), ('env_temp_1', 6),
        ('env_temp_2', 8), ('env_temp_2', 10),
        ('heater_temp_1', 12), ('heater_temp_1', 14),
        ('heater_temp_2', 16), ('heater_temp_2', 18),
        ('charge_current', 20), ('charge_current', 22),
        ('discharge_current', 24), ('discharge_current', 26),
    ]
    for name, bit_pos in alarm_names:
        alarm_code = (other_alarm >> bit_pos) & 0x03
        if alarm_code == 1:
            result['protection_alarms'].append(f'{name}_low')
        elif alarm_code == 2:
            result['protection_alarms'].append(f'{name}_high')
        elif alarm_code == 3:
            result['protection_alarms'].append(f'{name}_alarm')
    
    # Register 5106 (2 bytes) - Status1
    status1 = bytes_to_int(data, offset + 12, 2)
    if status1 & (1 << 15):
        result['protection_alarms'].append('module_undervoltage')
    if status1 & (1 << 14):
        result['protection_alarms'].append('charge_overtemp')
    if status1 & (1 << 13):
        result['protection_alarms'].append('charge_undertemp')
    if status1 & (1 << 12):
        result['protection_alarms'].append('discharge_overtemp')
    if status1 & (1 << 11):
        result['protection_alarms'].append('discharge_undertemp')
    if status1 & (1 << 10):
        result['protection_alarms'].append('discharge_overcurrent1')
    if status1 & (1 << 9):
        result['protection_alarms'].append('charge_overcurrent1')
    if status1 & (1 << 8):
        result['protection_alarms'].append('cell_overvoltage')
    if status1 & (1 << 7):
        result['protection_alarms'].append('cell_undervoltage')
    if status1 & (1 << 6):
        result['protection_alarms'].append('module_overvoltage')
    if status1 & (1 << 5):
        result['protection_alarms'].append('discharge_overcurrent2')
    if status1 & (1 << 4):
        result['protection_alarms'].append('charge_overcurrent2')
    # Bit 3: Using battery module power
    result['using_battery_power'] = bool(status1 & (1 << 3))
    # Bit 2: Discharge MOSFET
    result['discharge_mosfet'] = 'on' if status1 & (1 << 2) else 'off'
    # Bit 1: Charge MOSFET
    result['charge_mosfet'] = 'on' if status1 & (1 << 1) else 'off'
    # Bit 0: Short circuit
    if status1 & (1 << 0):
        result['protection_alarms'].append('short_circuit')
    
    # Register 5107 (2 bytes) - Status2
    status2 = bytes_to_int(data, offset + 14, 2)
    # Bit 15: Effective charge current
    result['effective_charge'] = bool(status2 & (1 << 15))
    # Bit 14: Effective discharge current
    result['effective_discharge'] = bool(status2 & (1 << 14))
    # Bit 13: Heater On
    result['heater_on'] = bool(status2 & (1 << 13))
    # Bit 12: Reserved
    # Bit 11: Fully charged
    result['fully_charged'] = bool(status2 & (1 << 11))
    # Bit 10: Reserved
    # Bit 9: Reserved
    # Bit 8: Buzzer
    result['buzzer_on'] = bool(status2 & (1 << 8))
    
    # Register 5108 (2 bytes) - Status3 (Warnings)
    status3 = bytes_to_int(data, offset + 16, 2)
    if status3 & (1 << 7):
        result['warnings'].append('discharge_high_temp')
    if status3 & (1 << 6):
        result['warnings'].append('discharge_low_temp')
    if status3 & (1 << 5):
        result['warnings'].append('charge_high_temp')
    if status3 & (1 << 4):
        result['warnings'].append('charge_low_temp')
    if status3 & (1 << 3):
        result['warnings'].append('module_high_voltage')
    if status3 & (1 << 2):
        result['warnings'].append('module_low_voltage')
    if status3 & (1 << 1):
        result['warnings'].append('cell_high_voltage')
    if status3 & (1 << 0):
        result['warnings'].append('cell_low_voltage')
    # Bits 8-15: Cell voltage errors (cells 11-16)
    for i in range(8):
        if status3 & (1 << (8 + i)):
            result['warnings'].append(f'cell_{11+i}_voltage_error')
    
    # Register 5109 (2 bytes) - Charge/Discharge Status
    status4 = bytes_to_int(data, offset + 18, 2)
    # Bit 7: Discharge enable
    result['discharge_enabled'] = bool(status4 & (1 << 7))
    # Bit 6: Charge enable
    result['charge_enabled'] = bool(status4 & (1 << 6))
    # Bit 5: Charge immediately
    result['charge_immediately'] = bool(status4 & (1 << 5))
    # Bit 4: Charge immediately (duplicate?)
    # Bit 3: Full charge request
    result['full_charge_request'] = bool(status4 & (1 << 3))
    
    # Combine all alarms
    all_alarms = result['cell_voltage_alarms'] + result['cell_temperature_alarms'] + result['protection_alarms']
    result['alarms'] = all_alarms
    result['alarm_count'] = len(all_alarms)
    result['warning_count'] = len(result['warnings'])
    
    return result


def parse_battery_device_info(data: bytes, offset: int = 3) -> Dict[str, Any]:
    """Parse battery device info (registers 5122-5129)."""
    result = {}
    if len(data) < offset + 16:
        return result
    
    # Battery name/model (ASCII, registers 5122-5129, 16 bytes)
    result['model'] = bytes_to_ascii(data, offset, 16).strip('\x00')
    
    return result


# ============================================================================
# INVERTER - BT-2 Module
# Register map based on Renogy Inverter Modbus Protocol V1.8
# ============================================================================

INVERTER_CHARGING_STATE = {
    0: 'not_charging',
    1: 'constant_current',
    2: 'constant_voltage',
    4: 'float',
    6: 'battery_activation',
    7: 'battery_disconnect'
}

INVERTER_MACHINE_STATE = {
    0: 'power_on_delay',
    1: 'waiting',
    2: 'initialization',
    3: 'soft_start',
    4: 'mains_operation',
    5: 'inverter_operation',
    6: 'inverter_to_mains',
    7: 'mains_to_inverter',
    10: 'shutdown',
    11: 'fault'
}

INVERTER_REGISTERS = [
    # Start with the basic status registers that most inverters support
    {'name': 'main_status', 'register': 4000, 'words': 10},  # Basic input/output status
    {'name': 'device_info', 'register': 4303, 'words': 24},  # Company, model, version
    # Note: The following registers only exist on bidirectional energy storage inverters
    # Simple inverters like RINVTPGH110111S will return error code 2 (Illegal Data Address)
    # {'name': 'settings', 'register': 4441, 'words': 4},    # Output priority, frequency, AC range, power saving
    # {'name': 'pv_info', 'register': 4327, 'words': 7},     # PV/solar data - may not exist
    # {'name': 'settings_status', 'register': 4398, 'words': 20},  # May not exist on all models
    # {'name': 'statistics', 'register': 4543, 'words': 25},  # May not exist on all models
]

# Inverter mode mapping (register 4102)
INVERTER_MODE = {
    0x00: 'unknown',
    0x01: 'normal',
    0x02: 'eco',  # Sleep/Hibernation mode = eco mode
    0x03: 'shutdown',
    0x04: 'restore',
}

# Output priority mapping (register 4441)
INVERTER_OUTPUT_PRIORITY = {
    0: 'solar',
    1: 'line',
    2: 'sbu',  # Solar-Battery-Utility
}


def parse_inverter_main_status(data: bytes, offset: int = 3) -> Dict[str, Any]:
    """
    Parse inverter main status (registers 4000-4009).
    Includes AC input/output, battery voltage, temperature, status.
    """
    result = {}
    if len(data) < offset + 18:
        return result
    
    def safe_value(raw: int, scale: float = 1.0, max_valid: int = 65000) -> float:
        """Return scaled value, or 0 if value is 0xFFFF (no data/disconnected)."""
        if raw >= max_valid:  # 0xFFFF or close to it means no data
            return 0.0
        return round(raw * scale, 2)
    
    # AC Input (registers 4000-4001) - may be 0xFFFF if no AC input
    input_v_raw = bytes_to_int(data, offset, 2)
    input_c_raw = bytes_to_int(data, offset + 2, 2)
    result['input_voltage'] = safe_value(input_v_raw, 0.1)
    result['input_current'] = safe_value(input_c_raw, 0.01)
    
    # AC Output (registers 4002-4004)
    result['output_voltage'] = bytes_to_int(data, offset + 4, 2, scale=0.1)
    result['output_current'] = bytes_to_int(data, offset + 6, 2, scale=0.01)
    result['output_frequency'] = bytes_to_int(data, offset + 8, 2, scale=0.01)
    
    # Battery and temperature (registers 4005-4006)
    result['battery_voltage'] = bytes_to_int(data, offset + 10, 2, scale=0.1)
    result['temperature'] = bytes_to_int(data, offset + 12, 2, scale=0.1)
    
    # Device status flags (registers 4007-4008)
    # Register 4007 = High Word (bits 31-16), Register 4008 = Low Word (bits 15-0)
    if len(data) >= offset + 18:
        status_high = bytes_to_int(data, offset + 14, 2)  # Bits 31-16
        status_low = bytes_to_int(data, offset + 16, 2)   # Bits 15-0
        
        result['faults'] = []
        
        # High word status/faults (bits 31-16, so bit 31 = bit 15 in this word)
        # Bit 31: Input UVP (undervoltage protection)
        if status_high & (1 << 15):
            result['faults'].append('input_uvp')
        # Bit 30: Input OVP (overvoltage protection)
        if status_high & (1 << 14):
            result['faults'].append('input_ovp')
        # Bit 29: Output OPP (overload protection)
        if status_high & (1 << 13):
            result['faults'].append('output_overload')
        # Bit 28: DC/DC overload
        if status_high & (1 << 12):
            result['faults'].append('dcdc_overload')
        # Bit 27: DC/DC overcurrent (hardware)
        if status_high & (1 << 11):
            result['faults'].append('dcdc_overcurrent')
        # Bit 26: Bus overvoltage
        if status_high & (1 << 10):
            result['faults'].append('bus_overvoltage')
        # Bit 25: PEN/Ground fault
        if status_high & (1 << 9):
            result['faults'].append('ground_fault')
        # Bit 24: OTP (over-temperature protection)
        if status_high & (1 << 8):
            result['faults'].append('over_temperature')
        # Bit 23: Output short circuit
        if status_high & (1 << 7):
            result['faults'].append('output_short_circuit')
        # Bit 22: Output UVP
        if status_high & (1 << 6):
            result['faults'].append('output_uvp')
        # Bit 21: Output OVP
        if status_high & (1 << 5):
            result['faults'].append('output_ovp')
        # Bit 20: Sleep mode / Low power hibernation / ECO mode
        eco_mode_active = bool(status_high & (1 << 4))
        result['eco_mode'] = eco_mode_active
        
        # Low word faults (bits 15-0)
        # Bit 15: Utility Fail
        if status_low & (1 << 15):
            result['faults'].append('utility_fail')
        # Bit 14: Battery Low
        if status_low & (1 << 14):
            result['faults'].append('battery_low')
        # Bit 13: APR (Automatic Power Resume)
        if status_low & (1 << 13):
            result['faults'].append('apr_active')
        # Bit 12: UPS Fail
        if status_low & (1 << 12):
            result['faults'].append('ups_fail')
        # Bit 11: UPS Type (1=Line-Interactive, 0=On-line) - not a fault
        result['ups_line_interactive'] = bool(status_low & (1 << 11))
        # Bit 10: Test in progress - not a fault
        result['test_in_progress'] = bool(status_low & (1 << 10))
        # Bit 9: Shutdown active
        if status_low & (1 << 9):
            result['faults'].append('shutdown_active')
        # Bit 8: Beeper on - not a fault
        result['beeper_on'] = bool(status_low & (1 << 8))
        # Bit 7: Fan locked
        if status_low & (1 << 7):
            result['faults'].append('fan_locked')
        # Bit 6: Inverter overload
        if status_low & (1 << 6):
            result['faults'].append('inverter_overload')
        # Bit 5: Inverter short circuit
        if status_low & (1 << 5):
            result['faults'].append('inverter_short_circuit')
        # Bit 4: Battery bad
        if status_low & (1 << 4):
            result['faults'].append('battery_bad')
        
        result['fault_count'] = len(result['faults'])
        
        # Log the status bits for debugging
        logger.debug(f"Inverter status - High: 0x{status_high:04X}, Low: 0x{status_low:04X}, ECO mode: {eco_mode_active}")
    
    # Input frequency (register 4009) - may be 0xFFFF if no AC input
    if len(data) >= offset + 20:
        input_freq_raw = bytes_to_int(data, offset + 18, 2)
        result['input_frequency'] = safe_value(input_freq_raw, 0.01)
    
    # Calculate power - only if we have valid input values
    if result.get('input_voltage', 0) > 0 and result.get('input_current', 0) > 0:
        result['input_power'] = round(result['input_voltage'] * result['input_current'], 1)
    else:
        result['input_power'] = 0.0
    result['output_power'] = round(result.get('output_voltage', 0) * result.get('output_current', 0), 1)
    
    return result


def parse_inverter_device_info(data: bytes, offset: int = 3) -> Dict[str, Any]:
    """Parse inverter device info (registers 4303-4326)."""
    result = {}
    if len(data) < offset + 48:
        return result
    
    # Company name (registers 4303-4310, 16 bytes ASCII)
    result['manufacturer'] = bytes_to_ascii(data, offset, 16)
    
    # Model (registers 4311-4318, 16 bytes ASCII)
    result['model'] = bytes_to_ascii(data, offset + 16, 16)
    
    # Version (registers 4319-4326, 16 bytes ASCII)
    result['firmware_version'] = bytes_to_ascii(data, offset + 32, 16)
    
    return result


def parse_inverter_pv_info(data: bytes, offset: int = 3) -> Dict[str, Any]:
    """
    Parse inverter PV/solar info (registers 4327-4333).
    For inverters with built-in MPPT charger.
    """
    result = {}
    if len(data) < offset + 12:
        return result
    
    # Battery SOC (register 4327)
    result['battery_soc'] = bytes_to_int(data, offset, 2)
    
    # Charging current (register 4328, 0.1A)
    result['charge_current'] = bytes_to_int(data, offset + 2, 2, scale=0.1)
    
    # PV voltage and current (registers 4329-4330)
    result['pv_voltage'] = bytes_to_int(data, offset + 4, 2, scale=0.1)
    result['pv_current'] = bytes_to_int(data, offset + 6, 2, scale=0.1)
    
    # PV power (register 4331, 1W)
    result['pv_power'] = bytes_to_int(data, offset + 8, 2)
    
    # Charge state (register 4332)
    if len(data) >= offset + 12:
        charge_state = bytes_to_int(data, offset + 10, 2) & 0xFF
        result['charging_status'] = INVERTER_CHARGING_STATE.get(charge_state, 'unknown')
    
    return result


def parse_inverter_settings_status(data: bytes, offset: int = 3) -> Dict[str, Any]:
    """Parse inverter settings and status (registers 4398-4417)."""
    result = {}
    if len(data) < offset + 30:
        return result
    
    # Machine state (register 4405)
    if len(data) >= offset + 16:
        machine_state = bytes_to_int(data, offset + 14, 2)
        result['machine_state'] = INVERTER_MACHINE_STATE.get(machine_state, 'unknown')
    
    # Bus voltage (register 4407)
    if len(data) >= offset + 20:
        result['bus_voltage'] = bytes_to_int(data, offset + 18, 2, scale=0.1)
    
    # Load current and power (registers 4408-4410)
    if len(data) >= offset + 26:
        result['load_current'] = bytes_to_int(data, offset + 20, 2, scale=0.1)
        result['load_active_power'] = bytes_to_int(data, offset + 22, 2)
        result['load_apparent_power'] = bytes_to_int(data, offset + 24, 2)
    
    # Load percentage (register 4413)
    if len(data) >= offset + 32:
        result['load_percentage'] = bytes_to_int(data, offset + 30, 2)
    
    return result


def parse_inverter_statistics(data: bytes, offset: int = 3) -> Dict[str, Any]:
    """Parse inverter energy statistics (registers 4543-4567)."""
    result = {}
    if len(data) < offset + 10:
        return result
    
    # Battery charge Ah today (register 4543)
    result['battery_charge_ah_today'] = bytes_to_int(data, offset, 2)
    
    # Battery discharge Ah today (register 4544)
    result['battery_discharge_ah_today'] = bytes_to_int(data, offset + 2, 2)
    
    # PV generation today (register 4545, 0.1kWh)
    result['pv_generation_today'] = bytes_to_int(data, offset + 4, 2, scale=0.1)
    
    # Load consumption today (register 4546, 0.1kWh)
    result['load_consumption_today'] = bytes_to_int(data, offset + 6, 2, scale=0.1)
    
    # Cumulative totals (4-byte values)
    if len(data) >= offset + 30:
        # Battery charge Ah total (registers 4550-4551)
        result['battery_charge_ah_total'] = bytes_to_int(data, offset + 14, 4)
        
        # Battery discharge Ah total (registers 4552-4553)
        result['battery_discharge_ah_total'] = bytes_to_int(data, offset + 18, 4)
        
        # PV generation total (registers 4554-4555, 0.1kWh)
        result['pv_generation_total'] = bytes_to_int(data, offset + 22, 4, scale=0.1)
        
        # Load consumption total (registers 4556-4557, 0.1kWh)
        result['load_consumption_total'] = bytes_to_int(data, offset + 26, 4, scale=0.1)
    
    return result


def parse_inverter_settings(data: bytes, offset: int = 3) -> Dict[str, Any]:
    """
    Parse inverter settings (registers 4441-4444).
    Includes output priority, frequency, AC range, and power saving mode.
    """
    result = {}
    if len(data) < offset + 8:
        return result
    
    # Output priority (register 4441)
    output_priority = bytes_to_int(data, offset, 2)
    result['output_priority'] = INVERTER_OUTPUT_PRIORITY.get(output_priority, 'unknown')
    
    # Output frequency setting (register 4442, 0.01Hz scale)
    output_freq = bytes_to_int(data, offset + 2, 2)
    result['output_frequency_setting'] = round(output_freq * 0.01, 1)
    
    # AC voltage range (register 4443)
    ac_range = bytes_to_int(data, offset + 4, 2)
    result['ac_voltage_range'] = 'wide' if ac_range == 0 else 'narrow'
    
    # Power saving mode (register 4444) - eco mode
    power_saving = bytes_to_int(data, offset + 6, 2)
    result['power_saving_mode'] = power_saving == 1
    result['eco_mode'] = power_saving == 1  # Alias for UI
    
    return result


# ============================================================================
# PARSER DISPATCH
# ============================================================================

# Map register addresses to parser functions for each device type
PARSERS = {
    DeviceType.CONTROLLER: {
        12: parse_controller_device_info,
        26: parse_controller_device_id,
        256: parse_controller_charging_info,
        289: parse_controller_faults,  # 0x0121: Fault and warning bits
        57348: parse_controller_battery_type,
        60000: parse_controller_historical,
    },
    DeviceType.BATTERY: {
        5000: parse_battery_cell_info,
        5017: parse_battery_temp_info,
        5042: parse_battery_info,
        5100: parse_battery_alarm_info,
        5122: parse_battery_device_info,
    },
    DeviceType.INVERTER: {
        4000: parse_inverter_main_status,
        4303: parse_inverter_device_info,
        4327: parse_inverter_pv_info,
        4398: parse_inverter_settings_status,
        4441: parse_inverter_settings,
        4543: parse_inverter_statistics,
    }
}

# Register definitions for each device type
REGISTER_DEFINITIONS = {
    DeviceType.CONTROLLER: CONTROLLER_REGISTERS,
    DeviceType.BATTERY: BATTERY_REGISTERS,
    DeviceType.INVERTER: INVERTER_REGISTERS,
}


def parse_response(device_type: DeviceType, register: int, data: bytes) -> Dict[str, Any]:
    """
    Parse a Modbus response based on device type and register.
    
    Args:
        device_type: Type of Renogy device
        register: Starting register address
        data: Raw response bytes
        
    Returns:
        Dictionary of parsed values
    """
    if device_type not in PARSERS:
        logger.warning(f"Unknown device type: {device_type}")
        return {}
    
    if register not in PARSERS[device_type]:
        logger.warning(f"Unknown register {register} for {device_type.value}")
        return {}
    
    parser_func = PARSERS[device_type][register]
    try:
        result = parser_func(data)
        logger.debug(f"Parsed {device_type.value} register {register}: {result}")
        return result
    except Exception as e:
        logger.error(f"Error parsing {device_type.value} register {register}: {e}")
        return {}


def get_registers_for_device(device_type: DeviceType) -> List[Dict]:
    """Get the list of registers to read for a device type."""
    return REGISTER_DEFINITIONS.get(device_type, [])

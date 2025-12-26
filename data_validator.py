"""
Data validation module for Renogy devices.

Implements spike detection and validation to filter out erroneous readings,
particularly from the Rover 40 charge controller which can occasionally
produce invalid data spikes.
"""

import logging
from typing import Dict, Any, Optional, List, Tuple
from datetime import datetime

logger = logging.getLogger(__name__)


# Sensor validation limits for Rover 40 controller
# Format: sensor_key: (min_value, max_value, max_change_per_update)
# max_change_per_update helps detect invalid spikes in data
CONTROLLER_VALIDATION_LIMITS = {
    # Battery sensors
    "battery_voltage": (0, 20, 5),  # 0-20V, max change 5V
    "battery_current": (-100, 100, 50),  # -100 to 100A, max change 50A
    "battery_percentage": (0, 100, 50),  # 0-100%, max change 50%
    "battery_temperature": (-40, 85, 20),  # -40 to 85°C, max change 20°C
    "charging_amp_hours_today": (0, 10000, 200),  # 0-10000Ah, max change 200
    "discharging_amp_hours_today": (0, 10000, 200),  # 0-10000Ah, max change 200
    
    # PV (solar panel) sensors
    "pv_voltage": (0, 25, 10),  # 0-25V, max change 10V
    "pv_current": (0, 100, 50),  # 0-100A, max change 50A
    "pv_power": (0, 5000, 2000),  # 0-5000W, max change 2000W
    "max_charging_power_today": (0, 5000, 5000),  # 0-5000W, no max change
    "power_generation_today": (0, 50000, 50000),  # 0-50000Wh, no max change (cumulative)
    "power_generation_total": (0, 1000000000, 100000),  # Large range for total
    
    # Load sensors
    "load_voltage": (0, 20, 20),  # 0-20V, max change 20V
    "load_current": (0, 20, 20),  # 0-20A, max change 20A
    "load_power": (0, 3000, 1500),  # 0-3000W, max change 1500W
    "power_consumption_today": (0, 50000, 50000),  # 0-50000Wh, no max change (cumulative)
    "max_discharging_power_today": (0, 3000, 3000),  # 0-3000W, no max change
    
    # Controller sensors
    "controller_temperature": (-40, 85, 20),  # -40 to 85°C, max change 20°C
}


class DataValidator:
    """
    Validates sensor data and detects invalid spikes.
    Maintains history of last known good values and rejection logs.
    """
    
    def __init__(self, device_name: str, device_type: str = 'controller'):
        """
        Initialize the data validator.
        
        Args:
            device_name: Name of the device for logging
            device_type: Type of device ('controller', 'battery', 'inverter')
        """
        self.device_name = device_name
        self.device_type = device_type
        self._last_good_values: Dict[str, float] = {}
        self._rejection_log: List[Dict[str, Any]] = []
        self._max_rejection_log = 100  # Keep last 100 rejections
        
        # Select validation limits based on device type
        if device_type == 'controller':
            self._limits = CONTROLLER_VALIDATION_LIMITS
        else:
            self._limits = {}  # No validation for other device types
    
    def validate_data(self, data: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """
        Validate sensor data and replace invalid values with last known good values.
        
        Args:
            data: Dictionary of sensor readings
            
        Returns:
            Tuple of (validated_data, list_of_rejections)
            - validated_data: Data with invalid values replaced
            - rejections: List of rejection details for this update
        """
        if not self._limits:
            # No validation configured for this device type
            return data, []
        
        validated = data.copy()
        rejections = []
        
        for key, value in data.items():
            if key not in self._limits:
                continue
            
            if not isinstance(value, (int, float)):
                continue
            
            min_val, max_val, max_change = self._limits[key]
            rejection_reason = None
            
            # Check absolute limits
            if value < min_val:
                rejection_reason = f"below_minimum (value={value}, min={min_val})"
            elif value > max_val:
                rejection_reason = f"above_maximum (value={value}, max={max_val})"
            
            # Check rate of change if we have a previous value
            if rejection_reason is None and key in self._last_good_values:
                last_value = self._last_good_values[key]
                change = abs(value - last_value)
                if change > max_change:
                    rejection_reason = f"spike_detected (value={value}, last={last_value}, change={change:.2f}, max_change={max_change})"
            
            if rejection_reason:
                # Log the rejection
                rejection = {
                    'timestamp': datetime.now().isoformat(),
                    'sensor': key,
                    'rejected_value': value,
                    'reason': rejection_reason,
                    'last_good_value': self._last_good_values.get(key),
                }
                rejections.append(rejection)
                self._add_to_rejection_log(rejection)
                
                logger.warning(
                    f"[{self.device_name}] Data rejected: {key}={value} - {rejection_reason}"
                )
                
                # Use last known good value if available
                if key in self._last_good_values:
                    validated[key] = self._last_good_values[key]
                else:
                    # No previous good value, keep the value but flag it
                    pass
            else:
                # Value is valid, update last known good
                self._last_good_values[key] = value
        
        return validated, rejections
    
    def _add_to_rejection_log(self, rejection: Dict[str, Any]):
        """Add a rejection to the log, maintaining max size."""
        self._rejection_log.append(rejection)
        if len(self._rejection_log) > self._max_rejection_log:
            self._rejection_log = self._rejection_log[-self._max_rejection_log:]
    
    def get_rejection_stats(self) -> Dict[str, Any]:
        """
        Get statistics about recent rejections.
        
        Returns:
            Dictionary with rejection statistics for MQTT publishing
        """
        if not self._rejection_log:
            return {
                'total_rejections': 0,
                'recent_rejections': [],
                'rejection_counts_by_sensor': {},
            }
        
        # Count rejections by sensor
        counts: Dict[str, int] = {}
        for r in self._rejection_log:
            sensor = r['sensor']
            counts[sensor] = counts.get(sensor, 0) + 1
        
        # Get last 5 rejections for display
        recent = self._rejection_log[-5:]
        
        return {
            'total_rejections': len(self._rejection_log),
            'recent_rejections': recent,
            'rejection_counts_by_sensor': counts,
            'last_rejection_time': self._rejection_log[-1]['timestamp'] if self._rejection_log else None,
        }
    
    def get_last_rejection(self) -> Optional[Dict[str, Any]]:
        """Get the most recent rejection, if any."""
        return self._rejection_log[-1] if self._rejection_log else None
    
    def clear_rejection_log(self):
        """Clear the rejection log."""
        self._rejection_log = []


class DataValidatorManager:
    """
    Manages data validators for multiple devices.
    """
    
    def __init__(self):
        self._validators: Dict[str, DataValidator] = {}
    
    def get_validator(self, device_name: str, device_type: str) -> DataValidator:
        """
        Get or create a validator for a device.
        
        Args:
            device_name: Unique name for the device
            device_type: Type of device
            
        Returns:
            DataValidator instance for this device
        """
        key = f"{device_name}_{device_type}"
        if key not in self._validators:
            self._validators[key] = DataValidator(device_name, device_type)
        return self._validators[key]
    
    def validate_device_data(self, device_name: str, device_type: str, 
                             data: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """
        Validate data for a device.
        
        Args:
            device_name: Name of the device
            device_type: Type of device
            data: Sensor data to validate
            
        Returns:
            Tuple of (validated_data, rejections)
        """
        validator = self.get_validator(device_name, device_type)
        return validator.validate_data(data)
    
    def get_all_rejection_stats(self) -> Dict[str, Dict[str, Any]]:
        """Get rejection stats for all devices."""
        return {
            name: v.get_rejection_stats() 
            for name, v in self._validators.items()
        }

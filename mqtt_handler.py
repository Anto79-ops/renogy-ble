"""
MQTT module for Renogy BT Monitor.
Supports Home Assistant MQTT Discovery for automatic device/entity setup.
"""

import asyncio
import json
import logging
from typing import Dict, List, Optional, Any
from dataclasses import dataclass

import paho.mqtt.client as mqtt
from paho.mqtt.client import MQTTMessage

from parsers import DeviceType

logger = logging.getLogger(__name__)


@dataclass
class MQTTConfig:
    """MQTT broker configuration."""
    host: str
    port: int = 1883
    username: Optional[str] = None
    password: Optional[str] = None
    discovery_prefix: str = "homeassistant"
    topic_prefix: str = "renogy"
    client_id: str = "renogy_monitor"


# Sensor definitions for Home Assistant discovery
# Each sensor has: key (from parsed data), name, device_class, unit, state_class, icon
CONTROLLER_SENSORS = [
    # Battery sensors
    {'key': 'battery_percentage', 'name': 'Battery', 'device_class': 'battery', 'unit': '%', 'state_class': 'measurement', 'icon': 'mdi:battery'},
    {'key': 'battery_voltage', 'name': 'Battery Voltage', 'device_class': 'voltage', 'unit': 'V', 'state_class': 'measurement'},
    {'key': 'battery_current', 'name': 'Battery Current', 'device_class': 'current', 'unit': 'A', 'state_class': 'measurement'},
    {'key': 'battery_temperature', 'name': 'Battery Temperature', 'device_class': 'temperature', 'unit': '°C', 'state_class': 'measurement'},
    {'key': 'battery_type', 'name': 'Battery Type', 'device_class': None, 'unit': None, 'icon': 'mdi:battery-outline'},
    
    # PV/Solar sensors
    {'key': 'pv_voltage', 'name': 'PV Voltage', 'device_class': 'voltage', 'unit': 'V', 'state_class': 'measurement'},
    {'key': 'pv_current', 'name': 'PV Current', 'device_class': 'current', 'unit': 'A', 'state_class': 'measurement'},
    {'key': 'pv_power', 'name': 'PV Power', 'device_class': 'power', 'unit': 'W', 'state_class': 'measurement'},
    
    # Load sensors
    {'key': 'load_voltage', 'name': 'Load Voltage', 'device_class': 'voltage', 'unit': 'V', 'state_class': 'measurement'},
    {'key': 'load_current', 'name': 'Load Current', 'device_class': 'current', 'unit': 'A', 'state_class': 'measurement'},
    {'key': 'load_power', 'name': 'Load Power', 'device_class': 'power', 'unit': 'W', 'state_class': 'measurement'},
    {'key': 'load_status', 'name': 'Load Status', 'device_class': None, 'unit': None, 'icon': 'mdi:power-plug'},
    
    # Controller sensors
    {'key': 'controller_temperature', 'name': 'Controller Temperature', 'device_class': 'temperature', 'unit': '°C', 'state_class': 'measurement'},
    {'key': 'charging_status', 'name': 'Charging Status', 'device_class': None, 'unit': None, 'icon': 'mdi:battery-charging'},
    
    # Daily statistics
    {'key': 'max_charging_power_today', 'name': 'Max Charging Power Today', 'device_class': 'power', 'unit': 'W', 'state_class': 'measurement'},
    {'key': 'max_discharging_power_today', 'name': 'Max Discharging Power Today', 'device_class': 'power', 'unit': 'W', 'state_class': 'measurement'},
    {'key': 'charging_amp_hours_today', 'name': 'Charging Ah Today', 'device_class': None, 'unit': 'Ah', 'state_class': 'total_increasing'},
    {'key': 'discharging_amp_hours_today', 'name': 'Discharging Ah Today', 'device_class': None, 'unit': 'Ah', 'state_class': 'total_increasing'},
    {'key': 'power_generation_today', 'name': 'Power Generation Today', 'device_class': 'energy', 'unit': 'Wh', 'state_class': 'total_increasing'},
    {'key': 'power_consumption_today', 'name': 'Power Consumption Today', 'device_class': 'energy', 'unit': 'Wh', 'state_class': 'total_increasing'},
    
    # Totals
    {'key': 'power_generation_total', 'name': 'Power Generation Total', 'device_class': 'energy', 'unit': 'Wh', 'state_class': 'total_increasing'},
    
    # Faults and Warnings - with json_attributes for details
    {'key': 'fault_count', 'name': 'Active Faults', 'device_class': None, 'unit': None, 'icon': 'mdi:alert-circle',
     'json_attributes': ['faults']},
    {'key': 'warning_count', 'name': 'Active Warnings', 'device_class': None, 'unit': None, 'icon': 'mdi:alert-outline',
     'json_attributes': ['warnings']},
]

BATTERY_SENSORS = [
    # Main battery metrics
    {'key': 'voltage', 'name': 'Voltage', 'device_class': 'voltage', 'unit': 'V', 'state_class': 'measurement'},
    {'key': 'current', 'name': 'Current', 'device_class': 'current', 'unit': 'A', 'state_class': 'measurement'},
    {'key': 'power', 'name': 'Power', 'device_class': 'power', 'unit': 'W', 'state_class': 'measurement'},
    {'key': 'soc', 'name': 'State of Charge', 'device_class': 'battery', 'unit': '%', 'state_class': 'measurement'},
    {'key': 'remaining_capacity', 'name': 'Remaining Capacity', 'device_class': None, 'unit': 'Ah', 'state_class': 'measurement'},
    {'key': 'total_capacity', 'name': 'Total Capacity', 'device_class': None, 'unit': 'Ah', 'state_class': 'measurement'},
    # Note: cycle_count requires register 5048 which may not be read by current register groups
    # {'key': 'cycle_count', 'name': 'Cycle Count', 'device_class': None, 'unit': 'cycles', 'state_class': 'total_increasing', 'icon': 'mdi:counter'},
    
    # Temperature sensors
    {'key': 'battery_temperature', 'name': 'Battery Temperature', 'device_class': 'temperature', 'unit': '°C', 'state_class': 'measurement'},
    
    # Cell info
    {'key': 'cell_count', 'name': 'Cell Count', 'device_class': None, 'unit': None, 'icon': 'mdi:battery-outline'},
    {'key': 'temperature_count', 'name': 'Temperature Sensor Count', 'device_class': None, 'unit': None, 'icon': 'mdi:thermometer'},
    
    # Alarms - with json_attributes for alarm details
    {'key': 'alarm_count', 'name': 'Active Alarms', 'device_class': None, 'unit': None, 'icon': 'mdi:alert',
     'json_attributes': ['alarms', 'cell_voltage_alarms', 'cell_temperature_alarms', 'protection_alarms']},
    {'key': 'warning_count', 'name': 'Active Warnings', 'device_class': None, 'unit': None, 'icon': 'mdi:alert-outline',
     'json_attributes': ['warnings']},
]

# Binary sensors for battery
BATTERY_BINARY_SENSORS = [
    {'key': 'heater_on', 'name': 'Heater', 'device_class': 'heat', 'icon': 'mdi:radiator'},
]

INVERTER_SENSORS = [
    # AC Input
    {'key': 'input_voltage', 'name': 'AC Input Voltage', 'device_class': 'voltage', 'unit': 'V', 'state_class': 'measurement'},
    {'key': 'input_current', 'name': 'AC Input Current', 'device_class': 'current', 'unit': 'A', 'state_class': 'measurement'},
    {'key': 'input_power', 'name': 'AC Input Power', 'device_class': 'power', 'unit': 'W', 'state_class': 'measurement'},
    {'key': 'input_frequency', 'name': 'AC Input Frequency', 'device_class': 'frequency', 'unit': 'Hz', 'state_class': 'measurement'},
    
    # AC Output
    {'key': 'output_voltage', 'name': 'AC Output Voltage', 'device_class': 'voltage', 'unit': 'V', 'state_class': 'measurement'},
    {'key': 'output_current', 'name': 'AC Output Current', 'device_class': 'current', 'unit': 'A', 'state_class': 'measurement'},
    {'key': 'output_power', 'name': 'AC Output Power', 'device_class': 'power', 'unit': 'W', 'state_class': 'measurement'},
    {'key': 'output_frequency', 'name': 'AC Output Frequency', 'device_class': 'frequency', 'unit': 'Hz', 'state_class': 'measurement'},
    # Note: These sensors require register 4441+ which is only available on energy storage inverters
    # {'key': 'output_frequency_setting', 'name': 'Output Frequency Setting', 'device_class': 'frequency', 'unit': 'Hz', 'state_class': 'measurement'},
    # {'key': 'output_priority', 'name': 'Output Priority', 'device_class': None, 'unit': None, 'icon': 'mdi:priority-high'},
    # {'key': 'ac_voltage_range', 'name': 'AC Voltage Range', 'device_class': None, 'unit': None, 'icon': 'mdi:sine-wave'},
    
    # Battery
    {'key': 'battery_voltage', 'name': 'Battery Voltage', 'device_class': 'voltage', 'unit': 'V', 'state_class': 'measurement'},
    # Note: battery_soc requires register 4327 which may not exist on basic inverters
    # {'key': 'battery_soc', 'name': 'Battery SOC', 'device_class': 'battery', 'unit': '%', 'state_class': 'measurement'},
    
    # Temperature and status
    {'key': 'temperature', 'name': 'Temperature', 'device_class': 'temperature', 'unit': '°C', 'state_class': 'measurement'},
    # Note: machine_state requires register 4398+ which may not exist on basic inverters
    # {'key': 'machine_state', 'name': 'Machine State', 'device_class': None, 'unit': None, 'icon': 'mdi:state-machine'},
    # {'key': 'charging_status', 'name': 'Charging Status', 'device_class': None, 'unit': None, 'icon': 'mdi:battery-charging'},
    
    # PV/Solar - only for solar inverters
    # {'key': 'pv_voltage', 'name': 'PV Voltage', 'device_class': 'voltage', 'unit': 'V', 'state_class': 'measurement'},
    # {'key': 'pv_current', 'name': 'PV Current', 'device_class': 'current', 'unit': 'A', 'state_class': 'measurement'},
    # {'key': 'pv_power', 'name': 'PV Power', 'device_class': 'power', 'unit': 'W', 'state_class': 'measurement'},
    # {'key': 'charge_current', 'name': 'Charge Current', 'device_class': 'current', 'unit': 'A', 'state_class': 'measurement'},
    
    # Load - only for energy storage inverters
    # {'key': 'load_current', 'name': 'Load Current', 'device_class': 'current', 'unit': 'A', 'state_class': 'measurement'},
    # {'key': 'load_active_power', 'name': 'Load Active Power', 'device_class': 'power', 'unit': 'W', 'state_class': 'measurement'},
    # {'key': 'load_apparent_power', 'name': 'Load Apparent Power', 'device_class': 'apparent_power', 'unit': 'VA', 'state_class': 'measurement'},
    # {'key': 'load_percentage', 'name': 'Load Percentage', 'device_class': None, 'unit': '%', 'state_class': 'measurement'},
    
    # Statistics - only for energy storage inverters
    # {'key': 'pv_generation_today', 'name': 'PV Generation Today', 'device_class': 'energy', 'unit': 'kWh', 'state_class': 'total_increasing'},
    # {'key': 'load_consumption_today', 'name': 'Load Consumption Today', 'device_class': 'energy', 'unit': 'kWh', 'state_class': 'total_increasing'},
    # {'key': 'pv_generation_total', 'name': 'PV Generation Total', 'device_class': 'energy', 'unit': 'kWh', 'state_class': 'total_increasing'},
    # {'key': 'load_consumption_total', 'name': 'Load Consumption Total', 'device_class': 'energy', 'unit': 'kWh', 'state_class': 'total_increasing'},
    
    # Fault - with json_attributes for fault details
    {'key': 'fault_count', 'name': 'Active Faults', 'device_class': None, 'unit': None, 'icon': 'mdi:alert-circle',
     'json_attributes': ['faults']},
]

# Binary sensors for inverter
# eco_mode is read from device status register 4007-4008, Bit 20 (Sleep/ECO mode)
INVERTER_BINARY_SENSORS = [
    {'key': 'eco_mode', 'name': 'ECO Mode', 'device_class': None, 'icon': 'mdi:leaf'},
    {'key': 'beeper_on', 'name': 'Beeper', 'device_class': None, 'icon': 'mdi:volume-high'},
]

SENSOR_DEFINITIONS = {
    'controller': CONTROLLER_SENSORS,
    'battery': BATTERY_SENSORS,
    'inverter': INVERTER_SENSORS,
}

BINARY_SENSOR_DEFINITIONS = {
    'battery': BATTERY_BINARY_SENSORS,
    'inverter': INVERTER_BINARY_SENSORS,
}


class MQTTHandler:
    """
    Handles MQTT communication and Home Assistant discovery.
    """
    
    def __init__(self, config: MQTTConfig):
        self.config = config
        self.client = mqtt.Client(client_id=config.client_id)
        self._connected = False
        self._discovery_sent: Dict[str, set] = {}  # Track which sensors have been announced
        
        # Setup callbacks
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        
        # Setup authentication
        if config.username and config.password:
            self.client.username_pw_set(config.username, config.password)
    
    def connect(self) -> bool:
        """Connect to the MQTT broker."""
        try:
            logger.info(f"Connecting to MQTT broker at {self.config.host}:{self.config.port}")
            self.client.connect(self.config.host, self.config.port, keepalive=60)
            self.client.loop_start()
            
            # Wait for connection
            for _ in range(50):  # 5 second timeout
                if self._connected:
                    return True
                asyncio.get_event_loop().run_until_complete(asyncio.sleep(0.1))
            
            logger.error("MQTT connection timeout")
            return False
            
        except Exception as e:
            logger.error(f"MQTT connection error: {e}")
            return False
    
    def disconnect(self):
        """Disconnect from the MQTT broker."""
        self.client.loop_stop()
        self.client.disconnect()
        self._connected = False
        logger.info("Disconnected from MQTT broker")
    
    def _on_connect(self, client, userdata, flags, rc):
        """Callback when connected to broker."""
        if rc == 0:
            logger.info("Connected to MQTT broker")
            self._connected = True
        else:
            logger.error(f"MQTT connection failed with code {rc}")
            self._connected = False
    
    def _on_disconnect(self, client, userdata, rc):
        """Callback when disconnected from broker."""
        logger.warning(f"Disconnected from MQTT broker (rc={rc})")
        self._connected = False
    
    def _on_message(self, client, userdata, message: MQTTMessage):
        """Callback for received messages."""
        logger.debug(f"Received message on {message.topic}: {message.payload}")
    
    def _get_device_id(self, device_name: str, mac_address: str) -> str:
        """Generate a unique device ID from name and MAC."""
        # Use last 6 chars of MAC (without colons) for unique ID
        mac_suffix = mac_address.replace(":", "").lower()[-6:]
        # Sanitize device name for use in topics
        safe_name = device_name.lower().replace(" ", "_").replace("-", "_")
        return f"{safe_name}_{mac_suffix}"
    
    def send_discovery(self, device_name: str, mac_address: str, 
                       device_type: str, model: str = None):
        """
        Send Home Assistant MQTT discovery messages for a device.
        
        Args:
            device_name: Friendly name for the device
            mac_address: Device MAC address
            device_type: Type of device (controller, battery, inverter)
            model: Device model name (optional)
        """
        device_id = self._get_device_id(device_name, mac_address)
        
        # Initialize discovery tracking for this device
        if device_id not in self._discovery_sent:
            self._discovery_sent[device_id] = set()
        
        # Device info for HA
        device_info = {
            "identifiers": [device_id],
            "name": device_name,
            "manufacturer": "Renogy",
            "model": model or device_type.title(),
            "sw_version": "1.0",
        }
        
        # Get sensor definitions for this device type
        sensors = SENSOR_DEFINITIONS.get(device_type, [])
        
        for sensor in sensors:
            sensor_key = sensor['key']
            
            # Skip if already discovered
            if sensor_key in self._discovery_sent[device_id]:
                continue
            
            # Build unique ID
            unique_id = f"{device_id}_{sensor_key}"
            
            # State topic
            state_topic = f"{self.config.topic_prefix}/{device_id}/state"
            
            # Build discovery payload (HA 2026.4+ compatible)
            # Use default filter to prevent template warnings when attribute is missing
            discovery_payload = {
                "name": sensor['name'],
                "unique_id": unique_id,
                "state_topic": state_topic,
                "value_template": f"{{{{ value_json.{sensor_key} | default(none) }}}}",
                "device": device_info,
            }
            
            # Add optional fields
            if sensor.get('device_class'):
                discovery_payload["device_class"] = sensor['device_class']
            if sensor.get('unit'):
                discovery_payload["unit_of_measurement"] = sensor['unit']
            if sensor.get('state_class'):
                discovery_payload["state_class"] = sensor['state_class']
            if sensor.get('icon'):
                discovery_payload["icon"] = sensor['icon']
            
            # Add json_attributes_topic if sensor has json_attributes defined
            # This allows alarm/fault details to be shown as entity attributes
            if sensor.get('json_attributes'):
                discovery_payload["json_attributes_topic"] = state_topic
                # Create a template that extracts only the relevant attributes
                attr_templates = {}
                for attr in sensor['json_attributes']:
                    attr_templates[attr] = f"{{{{ value_json.{attr} | default([]) | tojson }}}}"
                discovery_payload["json_attributes_template"] = (
                    "{ " + ", ".join([f'"{attr}": {attr_templates[attr]}' for attr in sensor['json_attributes']]) + " }"
                )
            
            # Discovery topic
            discovery_topic = (
                f"{self.config.discovery_prefix}/sensor/{device_id}/"
                f"{sensor_key}/config"
            )
            
            # Publish discovery message
            result = self.client.publish(
                discovery_topic,
                json.dumps(discovery_payload),
                qos=1,
                retain=True
            )
            
            if result.rc == mqtt.MQTT_ERR_SUCCESS:
                self._discovery_sent[device_id].add(sensor_key)
                logger.debug(f"Sent discovery for {device_name}/{sensor['name']}")
            else:
                logger.warning(f"Failed to send discovery for {sensor_key}")
        
        # Send binary sensor discovery
        binary_sensors = BINARY_SENSOR_DEFINITIONS.get(device_type, [])
        state_topic = f"{self.config.topic_prefix}/{device_id}/state"
        
        for sensor in binary_sensors:
            sensor_key = sensor['key']
            
            if sensor_key in self._discovery_sent[device_id]:
                continue
            
            unique_id = f"{device_id}_{sensor_key}"
            
            # Use default filter to prevent template warnings when attribute is missing
            discovery_payload = {
                "name": sensor['name'],
                "unique_id": unique_id,
                "state_topic": state_topic,
                "value_template": f"{{{{ 'ON' if value_json.{sensor_key} | default(false) else 'OFF' }}}}",
                "payload_on": "ON",
                "payload_off": "OFF",
                "device": device_info,
            }
            
            if sensor.get('device_class'):
                discovery_payload["device_class"] = sensor['device_class']
            if sensor.get('icon'):
                discovery_payload["icon"] = sensor['icon']
            
            discovery_topic = (
                f"{self.config.discovery_prefix}/binary_sensor/{device_id}/"
                f"{sensor_key}/config"
            )
            
            result = self.client.publish(
                discovery_topic,
                json.dumps(discovery_payload),
                qos=1,
                retain=True
            )
            
            if result.rc == mqtt.MQTT_ERR_SUCCESS:
                self._discovery_sent[device_id].add(sensor_key)
                logger.debug(f"Sent binary_sensor discovery for {device_name}/{sensor['name']}")
        
        # For batteries, cell voltage discovery happens dynamically in publish_state
        # based on actual cell_count from device data
        
        logger.info(f"Discovery messages sent for {device_name}")
    
    def publish_state(self, device_name: str, mac_address: str, data: Dict[str, Any]):
        """
        Publish device state to MQTT.
        
        Args:
            device_name: Friendly name for the device
            mac_address: Device MAC address
            data: Dictionary of sensor values
        """
        device_id = self._get_device_id(device_name, mac_address)
        state_topic = f"{self.config.topic_prefix}/{device_id}/state"
        
        # Dynamically create cell voltage discovery if we have cell data
        if 'cell_count' in data and 'cell_voltages' in data:
            cell_count = data.get('cell_count', 0)
            device_type = data.get('__device_type', 'battery')
            model = data.get('model', 'Renogy Battery')
            self._send_dynamic_cell_discovery(device_id, device_name, mac_address, 
                                              device_type, model, cell_count)
        
        # Also create temperature sensors dynamically if present
        if 'temperature_count' in data and 'temperatures' in data:
            temp_count = data.get('temperature_count', 0)
            device_type = data.get('__device_type', 'battery')
            model = data.get('model', 'Renogy Battery')
            self._send_dynamic_temperature_discovery(device_id, device_name, mac_address,
                                                      device_type, model, temp_count)
        
        # Clean data for JSON serialization
        clean_data = {}
        for key, value in data.items():
            if key.startswith('__'):
                continue  # Skip internal fields
            if isinstance(value, (list, dict)):
                clean_data[key] = value
            elif value is not None:
                clean_data[key] = value
        
        payload = json.dumps(clean_data)
        
        result = self.client.publish(
            state_topic,
            payload,
            qos=1,
            retain=True  # Retain state so entities survive HA reboots
        )
        
        if result.rc == mqtt.MQTT_ERR_SUCCESS:
            logger.debug(f"Published state for {device_name}")
        else:
            logger.warning(f"Failed to publish state for {device_name}")
    
    def _send_dynamic_cell_discovery(self, device_id: str, device_name: str,
                                      mac_address: str, device_type: str,
                                      model: str, cell_count: int):
        """Send discovery for cell voltages based on actual cell count."""
        state_topic = f"{self.config.topic_prefix}/{device_id}/state"
        
        # Device info for discovery
        device_info = {
            "identifiers": [device_id],
            "name": device_name,
            "manufacturer": "Renogy",
            "model": model or "Battery",
        }
        
        for i in range(cell_count):
            sensor_key = f"cell_{i+1}_voltage"
            
            if sensor_key in self._discovery_sent.get(device_id, set()):
                continue
            
            unique_id = f"{device_id}_{sensor_key}"
            
            discovery_payload = {
                "name": f"Cell {i + 1} Voltage",
                "unique_id": unique_id,
                "state_topic": state_topic,
                "value_template": f"{{{{ value_json.cell_voltages[{i}] | default(none) }}}}",
                "device": device_info,
                "device_class": "voltage",
                "unit_of_measurement": "V",
                "state_class": "measurement",
                "availability_topic": f"{self.config.topic_prefix}/{device_id}/availability",
            }
            
            discovery_topic = (
                f"{self.config.discovery_prefix}/sensor/{device_id}/"
                f"{sensor_key}/config"
            )
            
            result = self.client.publish(
                discovery_topic,
                json.dumps(discovery_payload),
                qos=1,
                retain=True
            )
            
            if result.rc == mqtt.MQTT_ERR_SUCCESS:
                if device_id not in self._discovery_sent:
                    self._discovery_sent[device_id] = set()
                self._discovery_sent[device_id].add(sensor_key)
    
    def _send_dynamic_temperature_discovery(self, device_id: str, device_name: str,
                                             mac_address: str, device_type: str,
                                             model: str, temp_count: int):
        """Send discovery for temperature sensors based on actual count."""
        state_topic = f"{self.config.topic_prefix}/{device_id}/state"
        
        # Device info for discovery
        device_info = {
            "identifiers": [device_id],
            "name": device_name,
            "manufacturer": "Renogy",
            "model": model or "Battery",
        }
        
        for i in range(temp_count):
            sensor_key = f"temperature_{i+1}"
            
            if sensor_key in self._discovery_sent.get(device_id, set()):
                continue
            
            unique_id = f"{device_id}_{sensor_key}"
            
            discovery_payload = {
                "name": f"Temperature {i + 1}",
                "unique_id": unique_id,
                "state_topic": state_topic,
                "value_template": f"{{{{ value_json.temperatures[{i}] | default(none) }}}}",
                "device": device_info,
                "device_class": "temperature",
                "unit_of_measurement": "°C",
                "state_class": "measurement",
                "availability_topic": f"{self.config.topic_prefix}/{device_id}/availability",
            }
            
            discovery_topic = (
                f"{self.config.discovery_prefix}/sensor/{device_id}/"
                f"{sensor_key}/config"
            )
            
            result = self.client.publish(
                discovery_topic,
                json.dumps(discovery_payload),
                qos=1,
                retain=True
            )
            
            if result.rc == mqtt.MQTT_ERR_SUCCESS:
                if device_id not in self._discovery_sent:
                    self._discovery_sent[device_id] = set()
                self._discovery_sent[device_id].add(sensor_key)
    
    def publish_availability(self, device_name: str, mac_address: str, 
                             available: bool):
        """
        Publish device availability status.
        
        Args:
            device_name: Friendly name for the device
            mac_address: Device MAC address
            available: Whether the device is available
        """
        device_id = self._get_device_id(device_name, mac_address)
        availability_topic = f"{self.config.topic_prefix}/{device_id}/availability"
        
        payload = "online" if available else "offline"
        
        self.client.publish(
            availability_topic,
            payload,
            qos=1,
            retain=True
        )
    
    def publish_validation_stats(self, device_name: str, mac_address: str, 
                                  stats: Dict[str, Any]):
        """
        Publish data validation/rejection statistics to MQTT.
        
        This allows monitoring of data quality issues in Home Assistant.
        Creates a separate sensor for tracking validation rejections.
        
        Args:
            device_name: Friendly name for the device
            mac_address: Device MAC address
            stats: Dictionary with rejection statistics
        """
        device_id = self._get_device_id(device_name, mac_address)
        
        # Create discovery for validation sensor if not already sent
        validation_sensor_key = "validation_stats"
        if validation_sensor_key not in self._discovery_sent.get(device_id, set()):
            device_info = {
                "identifiers": [device_id],
                "name": device_name,
                "manufacturer": "Renogy",
            }
            
            state_topic = f"{self.config.topic_prefix}/{device_id}/validation"
            
            discovery_payload = {
                "name": "Data Validation",
                "unique_id": f"{device_id}_validation_stats",
                "state_topic": state_topic,
                "value_template": "{{ value_json.total_rejections }}",
                "json_attributes_topic": state_topic,
                "json_attributes_template": "{{ value_json | tojson }}",
                "device": device_info,
                "icon": "mdi:alert-check",
                "unit_of_measurement": "rejections",
            }
            
            discovery_topic = (
                f"{self.config.discovery_prefix}/sensor/{device_id}/"
                f"{validation_sensor_key}/config"
            )
            
            result = self.client.publish(
                discovery_topic,
                json.dumps(discovery_payload),
                qos=1,
                retain=True
            )
            
            if result.rc == mqtt.MQTT_ERR_SUCCESS:
                if device_id not in self._discovery_sent:
                    self._discovery_sent[device_id] = set()
                self._discovery_sent[device_id].add(validation_sensor_key)
                logger.debug(f"Sent validation stats discovery for {device_name}")
        
        # Publish the validation stats
        validation_topic = f"{self.config.topic_prefix}/{device_id}/validation"
        
        # Format the stats for publishing
        publish_stats = {
            'total_rejections': stats.get('total_rejections', 0),
            'rejection_counts_by_sensor': stats.get('rejection_counts_by_sensor', {}),
            'last_rejection_time': stats.get('last_rejection_time'),
        }
        
        # Include recent rejections with more readable format
        recent = stats.get('recent_rejections', [])
        if recent:
            publish_stats['recent_rejections'] = [
                {
                    'time': r.get('timestamp', ''),
                    'sensor': r.get('sensor', ''),
                    'value': r.get('rejected_value', ''),
                    'reason': r.get('reason', ''),
                }
                for r in recent[-3:]  # Last 3 rejections
            ]
        
        self.client.publish(
            validation_topic,
            json.dumps(publish_stats),
            qos=1,
            retain=True
        )
        
        logger.debug(f"Published validation stats for {device_name}: {stats.get('total_rejections', 0)} rejections")
    
    @property
    def is_connected(self) -> bool:
        """Check if connected to MQTT broker."""
        return self._connected

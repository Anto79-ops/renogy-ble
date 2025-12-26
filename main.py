#!/usr/bin/env python3
"""
Renogy BT Monitor - Main Application

A standalone Python application for monitoring Renogy solar devices
via Bluetooth and publishing data to an MQTT broker (Home Assistant).

Supports:
- Renogy Rover/Wanderer/Adventurer charge controllers (BT-1/BT-2)
- Renogy LiFePO4 smart batteries (BT-2)
- Renogy inverters (BT-2)

Usage:
    python main.py [config.yaml]
    python main.py --scan          # Scan for nearby devices
    python main.py --help          # Show help
"""

import argparse
import asyncio
import logging
import signal
import sys
import os
from typing import Dict, Any, Optional
from pathlib import Path

import yaml

from ble_client import BLEDeviceManager, DeviceConfig, scan_for_devices, set_bt_adapter, get_bt_adapter
from mqtt_handler import MQTTHandler, MQTTConfig
from data_validator import DataValidatorManager

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class RenogyMonitor:
    """
    Main application class that coordinates BLE device polling
    and MQTT publishing.
    """
    
    def __init__(self, config_path: str):
        self.config_path = config_path
        self.config: Dict[str, Any] = {}
        self.mqtt_handler: Optional[MQTTHandler] = None
        self.device_manager: Optional[BLEDeviceManager] = None
        self.data_validator = DataValidatorManager()  # For spike detection
        self._running = False
        self._shutdown_event: Optional[asyncio.Event] = None  # Created in run()
        
    def load_config(self) -> bool:
        """Load configuration from YAML file."""
        try:
            with open(self.config_path, 'r') as f:
                self.config = yaml.safe_load(f)
            
            # Validate required sections
            required_sections = ['mqtt', 'devices']
            for section in required_sections:
                if section not in self.config:
                    logger.error(f"Missing required config section: {section}")
                    return False
            
            logger.info(f"Configuration loaded from {self.config_path}")
            return True
            
        except FileNotFoundError:
            logger.error(f"Configuration file not found: {self.config_path}")
            return False
        except yaml.YAMLError as e:
            logger.error(f"Error parsing configuration: {e}")
            return False
        except Exception as e:
            logger.error(f"Error loading configuration: {e}")
            return False
    
    def setup_logging(self):
        """Configure logging based on config file."""
        log_config = self.config.get('logging', {})
        
        # Set log level
        level_str = log_config.get('level', 'INFO').upper()
        level = getattr(logging, level_str, logging.INFO)
        logging.getLogger().setLevel(level)
        
        # Configure file logging if specified
        log_file = log_config.get('file')
        if log_file:
            try:
                file_handler = logging.FileHandler(log_file)
                file_handler.setFormatter(logging.Formatter(
                    '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
                ))
                logging.getLogger().addHandler(file_handler)
                logger.info(f"Logging to file: {log_file}")
            except Exception as e:
                logger.warning(f"Could not setup file logging: {e}")
        
        # Disable console logging if requested
        if not log_config.get('console', True):
            for handler in logging.getLogger().handlers:
                if isinstance(handler, logging.StreamHandler):
                    logging.getLogger().removeHandler(handler)
    
    def setup_mqtt(self) -> bool:
        """Initialize MQTT handler."""
        mqtt_config = self.config.get('mqtt', {})
        
        config = MQTTConfig(
            host=mqtt_config.get('host', 'localhost'),
            port=mqtt_config.get('port', 1883),
            username=mqtt_config.get('username'),
            password=mqtt_config.get('password'),
            discovery_prefix=mqtt_config.get('discovery_prefix', 'homeassistant'),
            topic_prefix=mqtt_config.get('topic_prefix', 'renogy'),
        )
        
        self.mqtt_handler = MQTTHandler(config)
        
        if not self.mqtt_handler.connect():
            logger.error("Failed to connect to MQTT broker")
            return False
        
        return True
    
    def setup_devices(self) -> bool:
        """Initialize BLE device manager."""
        # Configure Bluetooth adapter
        bt_config = self.config.get('bluetooth', {})
        adapter = bt_config.get('adapter', 'hci0')
        set_bt_adapter(adapter)
        logger.info(f"Using Bluetooth adapter: {adapter}")
        
        device_configs = []
        
        for dev in self.config.get('devices', []):
            try:
                config = DeviceConfig(
                    name=dev['name'],
                    mac_address=dev['mac_address'],
                    alias=dev.get('alias', ''),
                    device_type=dev['type'],
                    device_id=dev.get('device_id', 255),
                    adapter=dev.get('adapter', 'bt2'),
                )
                device_configs.append(config)
                logger.info(f"Configured device: {config.name} ({config.device_type})")
            except KeyError as e:
                logger.error(f"Missing required device field: {e}")
                return False
        
        if not device_configs:
            logger.error("No devices configured")
            return False
        
        self.device_manager = BLEDeviceManager(
            device_configs,
            on_data_callback=self._on_device_data
        )
        
        return True
    
    async def _on_device_data(self, device_key: str, data: Dict[str, Any]):
        """Callback when new data is received from a device."""
        device_data = self.device_manager.get_device_data(device_key)
        if not device_data:
            logger.warning(f"No device data found for key: {device_key}")
            return
        
        config = device_data.config
        
        # Validate data and detect spikes (only for controllers)
        validated_data, rejections = self.data_validator.validate_device_data(
            config.name,
            config.device_type,
            data
        )
        
        # If there were rejections, publish rejection stats
        if rejections:
            validator = self.data_validator.get_validator(config.name, config.device_type)
            rejection_stats = validator.get_rejection_stats()
            self.mqtt_handler.publish_validation_stats(
                config.name,
                config.mac_address,
                rejection_stats
            )
        
        # Send discovery if first time
        model = validated_data.get('model', config.device_type.title())
        self.mqtt_handler.send_discovery(
            config.name,
            config.mac_address,
            config.device_type,
            model
        )
        
        # Publish validated state
        self.mqtt_handler.publish_state(
            config.name,
            config.mac_address,
            validated_data
        )
        
        # Update availability
        self.mqtt_handler.publish_availability(
            config.name,
            config.mac_address,
            device_data.is_available
        )
    
    async def run(self):
        """Main run loop with persistent connections."""
        self._running = True
        self._shutdown_event = asyncio.Event()  # Create in the running loop
        
        poll_config = self.config.get('polling', {})
        poll_interval = poll_config.get('interval', 60)
        
        logger.info(f"Starting Renogy Monitor (poll interval: {poll_interval}s)")
        
        try:
            # Connect to all BT modules on startup
            logger.info("Establishing persistent connections to all BT modules...")
            connected = await self.device_manager.connect_all()
            total = len(self.device_manager._connections)
            logger.info(f"Connected to {connected}/{total} BT modules")
            
            if connected == 0:
                logger.error("Failed to connect to any BT modules!")
                # Continue anyway - will retry on each poll
            
            while self._running and not self._shutdown_event.is_set():
                # Poll all devices (connections are persistent)
                logger.debug("Polling devices...")
                results = await self.device_manager.poll_once()
                
                # Log results summary
                available_count = 0
                all_devices = self.device_manager.get_all_device_data()
                for device_key, device_data in all_devices.items():
                    if device_data.is_available:
                        available_count += 1
                        logger.info(f"{device_data.config.name}: Online")
                    else:
                        logger.warning(f"{device_data.config.name}: Unavailable")
                
                logger.info(f"Poll complete: {available_count}/{len(all_devices)} devices online")
                
                # Wait for next poll interval or shutdown
                try:
                    await asyncio.wait_for(
                        self._shutdown_event.wait(),
                        timeout=poll_interval
                    )
                    # If we get here, shutdown was signaled
                    break
                except asyncio.TimeoutError:
                    pass  # Normal timeout, continue polling
                    
        except asyncio.CancelledError:
            logger.info("Run loop cancelled")
        except Exception as e:
            logger.error(f"Error in run loop: {e}")
            import traceback
            traceback.print_exc()
            raise
    
    async def shutdown(self):
        """Graceful shutdown."""
        logger.info("Shutting down...")
        self._running = False
        if self._shutdown_event:
            self._shutdown_event.set()
        
        # Disconnect from devices
        if self.device_manager:
            await self.device_manager.stop()
        
        # Disconnect from MQTT
        if self.mqtt_handler:
            # Send offline status for all devices
            if self.device_manager:
                for mac, device_data in self.device_manager.get_all_device_data().items():
                    self.mqtt_handler.publish_availability(
                        device_data.config.name,
                        device_data.config.mac_address,
                        False
                    )
            self.mqtt_handler.disconnect()
        
        logger.info("Shutdown complete")
    
    def start(self):
        """Entry point to start the monitor."""
        # Load configuration
        if not self.load_config():
            return 1
        
        # Setup logging
        self.setup_logging()
        
        # Setup MQTT
        if not self.setup_mqtt():
            return 1
        
        # Setup devices
        if not self.setup_devices():
            return 1
        
        # Setup signal handlers
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        def signal_handler():
            logger.info("Received shutdown signal")
            loop.create_task(self.shutdown())
        
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, signal_handler)
            except NotImplementedError:
                # Signal handlers not supported on Windows
                pass
        
        # Run the main loop
        try:
            loop.run_until_complete(self.run())
        except KeyboardInterrupt:
            logger.info("Interrupted by user")
            loop.run_until_complete(self.shutdown())
        finally:
            loop.close()
        
        return 0


async def scan_devices(show_all: bool = False):
    """Scan for nearby Renogy BLE devices."""
    print("\n" + "=" * 60)
    print("Scanning for Renogy BLE devices...")
    print("Make sure your devices are powered on and in range.")
    print("=" * 60 + "\n")
    
    devices = await scan_for_devices(timeout=15.0, show_all=show_all)
    
    if devices:
        print(f"\nFound {len(devices)} device(s):\n")
        print("-" * 60)
        for i, dev in enumerate(devices, 1):
            print(f"{i}. Name: {dev['name']}")
            print(f"   MAC Address: {dev['address']}")
            if dev.get('rssi'):
                print(f"   Signal Strength: {dev['rssi']} dBm")
            print()
        print("-" * 60)
        print("\nAdd the Renogy devices (BT-TH-xxx) to your config.yaml file.")
    else:
        print("\nNo devices found.")
        print("\nTips:")
        print("  - Make sure devices are powered on")
        print("  - Check that BT-1/BT-2 module is connected and has LED lit")
        print("  - Move closer to the devices")
        print("  - Try running with sudo if on Linux")
        print("  - Try: python main.py --scan-all   (to see all BLE devices)")
    
    return 0


def create_sample_config(output_path: str = "config.yaml"):
    """Create a sample configuration file."""
    sample_config = """# Renogy BT Monitor Configuration
# Update with your device and MQTT broker settings

# MQTT Broker Configuration
mqtt:
  host: "homeassistant.local"  # MQTT broker hostname or IP
  port: 1883
  username: ""  # Leave empty if no auth
  password: ""
  discovery_prefix: "homeassistant"
  topic_prefix: "renogy"

# Polling Configuration
polling:
  interval: 60  # Seconds between polls (10-600)
  retry_attempts: 3
  retry_delay: 5

# Device Configuration
# Run 'python main.py --scan' to discover devices
devices:
  - name: "Solar Controller"
    mac_address: "XX:XX:XX:XX:XX:XX"  # Replace with your device MAC
    alias: "BT-TH-XXXXXXXX"
    type: "controller"  # controller, battery, or inverter
    device_id: 255
    adapter: "bt1"

# Logging Configuration
logging:
  level: "INFO"  # DEBUG, INFO, WARNING, ERROR
  file: ""  # Optional log file path
  console: true
"""
    
    with open(output_path, 'w') as f:
        f.write(sample_config)
    
    print(f"Sample configuration created: {output_path}")
    print("Edit this file with your device and MQTT settings.")
    return 0


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Renogy BT Monitor - Monitor Renogy solar devices via Bluetooth",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py config.yaml       Run with specified config file
  python main.py --scan            Scan for nearby Renogy devices
  python main.py --create-config   Create a sample configuration file
  
Supported Devices:
  - Renogy Rover/Wanderer/Adventurer charge controllers
  - Renogy LiFePO4 smart batteries
  - Renogy inverters (with BT-2 module)

For more information, see the README.md file.
        """
    )
    
    parser.add_argument(
        'config',
        nargs='?',
        default='config.yaml',
        help='Path to configuration file (default: config.yaml)'
    )
    
    parser.add_argument(
        '--scan',
        action='store_true',
        help='Scan for nearby Renogy BLE devices'
    )
    
    parser.add_argument(
        '--scan-all',
        action='store_true',
        help='Scan and show ALL BLE devices (for debugging)'
    )
    
    parser.add_argument(
        '--adapter',
        type=str,
        default='hci0',
        help='Bluetooth adapter to use (default: hci0, use hci1 for USB adapter)'
    )
    
    parser.add_argument(
        '--create-config',
        action='store_true',
        help='Create a sample configuration file'
    )
    
    parser.add_argument(
        '--debug',
        action='store_true',
        help='Enable debug logging'
    )
    
    args = parser.parse_args()
    
    # Handle debug flag
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
        logger.debug("Debug logging enabled")
    
    # Handle scan mode
    if args.scan:
        set_bt_adapter(args.adapter)
        print(f"Using Bluetooth adapter: {args.adapter}")
        return asyncio.run(scan_devices(show_all=False))
    
    # Handle scan-all mode
    if args.scan_all:
        set_bt_adapter(args.adapter)
        print(f"Using Bluetooth adapter: {args.adapter}")
        return asyncio.run(scan_devices(show_all=True))
    
    # Handle config creation
    if args.create_config:
        return create_sample_config()
    
    # Check if config file exists
    if not os.path.exists(args.config):
        logger.error(f"Configuration file not found: {args.config}")
        logger.info("Run 'python main.py --create-config' to create a sample config")
        return 1
    
    # Start the monitor
    monitor = RenogyMonitor(args.config)
    return monitor.start()


if __name__ == "__main__":
    sys.exit(main())

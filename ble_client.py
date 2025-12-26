"""
BLE communication module for Renogy devices.
Uses persistent connections for reliability.
"""

import asyncio
import logging
from typing import Dict, List, Optional, Callable, Any
from dataclasses import dataclass, field
from datetime import datetime

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError

from utils import create_modbus_read_request, validate_modbus_response
from parsers import DeviceType, parse_response, get_registers_for_device

logger = logging.getLogger(__name__)

# Renogy BLE Service and Characteristic UUIDs
NOTIFY_CHAR_UUID = "0000fff1-0000-1000-8000-00805f9b34fb"
WRITE_CHAR_UUID = "0000ffd1-0000-1000-8000-00805f9b34fb"

# Timeouts
CONNECTION_TIMEOUT = 30.0
NOTIFICATION_TIMEOUT = 5.0
REQUEST_DELAY = 0.5
RECONNECT_DELAY = 10.0

# Default Bluetooth adapter
DEFAULT_BT_ADAPTER = "hci0"


def get_bt_adapter() -> str:
    """Get the configured Bluetooth adapter."""
    return getattr(get_bt_adapter, '_adapter', DEFAULT_BT_ADAPTER)


def set_bt_adapter(adapter: str):
    """Set the Bluetooth adapter to use."""
    get_bt_adapter._adapter = adapter
    logger.info(f"Bluetooth adapter set to: {adapter}")


@dataclass
class DeviceConfig:
    """Configuration for a Renogy BLE device."""
    name: str
    mac_address: str
    alias: str
    device_type: str  # 'controller', 'battery', 'inverter'
    device_id: int = 255
    adapter: str = 'bt2'
    
    def get_device_type_enum(self) -> DeviceType:
        type_map = {
            'controller': DeviceType.CONTROLLER,
            'battery': DeviceType.BATTERY,
            'inverter': DeviceType.INVERTER,
        }
        return type_map.get(self.device_type.lower(), DeviceType.CONTROLLER)


@dataclass
class DeviceData:
    """Stores data collected from a device."""
    config: DeviceConfig
    data: Dict[str, Any] = field(default_factory=dict)
    last_update: Optional[datetime] = None
    is_available: bool = False
    consecutive_failures: int = 0
    
    def update(self, new_data: Dict[str, Any]):
        self.data.update(new_data)
        self.last_update = datetime.now()
        self.is_available = True
        self.consecutive_failures = 0
    
    def mark_failed(self):
        self.consecutive_failures += 1
        if self.consecutive_failures >= 3:
            self.is_available = False


class PersistentBLEConnection:
    """
    Manages a persistent BLE connection to a Renogy BT module.
    Handles automatic reconnection and supports Hub mode (multiple devices on one BT module).
    """
    
    def __init__(self, mac_address: str, device_configs: List[DeviceConfig]):
        self.mac_address = mac_address
        self.device_configs = device_configs
        self.client: Optional[BleakClient] = None
        self._connected = False
        self._notify_char = None
        self._write_char = None
        self._notification_data = bytearray()
        self._notification_event: Optional[asyncio.Event] = None  # Created lazily
        self._lock: Optional[asyncio.Lock] = None  # Created lazily
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        
    @property
    def is_connected(self) -> bool:
        return self._connected and self.client and self.client.is_connected
    
    def _ensure_async_primitives(self):
        """Ensure asyncio primitives are created in the current event loop."""
        current_loop = asyncio.get_event_loop()
        if self._loop is not current_loop:
            self._loop = current_loop
            self._notification_event = asyncio.Event()
            self._lock = asyncio.Lock()
    
    def _notification_handler(self, sender: int, data: bytearray):
        """Handle incoming notification data."""
        logger.debug(f"[{self.mac_address}] Notification: {data.hex()}")
        self._notification_data.extend(data)
        self._notification_event.set()
    
    async def connect(self) -> bool:
        """Establish connection to the BT module."""
        self._ensure_async_primitives()
        
        if self.is_connected:
            return True
        
        adapter = get_bt_adapter()
        
        for attempt in range(3):
            try:
                if attempt > 0:
                    logger.info(f"[{self.mac_address}] Retry {attempt + 1}/3...")
                    await asyncio.sleep(5.0)
                
                logger.info(f"[{self.mac_address}] Connecting using {adapter}...")
                
                # Find the device
                device = await BleakScanner.find_device_by_address(
                    self.mac_address,
                    timeout=CONNECTION_TIMEOUT,
                    adapter=adapter
                )
                
                if not device:
                    # Try a full scan
                    logger.debug(f"[{self.mac_address}] Not found, scanning...")
                    devices = await BleakScanner.discover(timeout=10.0, adapter=adapter)
                    for d in devices:
                        if d.address.upper() == self.mac_address.upper():
                            device = d
                            break
                
                if not device:
                    logger.warning(f"[{self.mac_address}] Device not found")
                    continue
                
                # Connect
                self.client = BleakClient(
                    device, 
                    timeout=CONNECTION_TIMEOUT,
                    disconnected_callback=self._on_disconnect
                )
                await self.client.connect()
                
                if not self.client.is_connected:
                    logger.warning(f"[{self.mac_address}] Connection failed")
                    continue
                
                # Discover characteristics
                await self._setup_characteristics()
                
                # Subscribe to notifications
                await self.client.start_notify(self._notify_char, self._notification_handler)
                
                self._connected = True
                logger.info(f"[{self.mac_address}] Connected successfully")
                return True
                
            except BleakError as e:
                logger.warning(f"[{self.mac_address}] BLE error (attempt {attempt + 1}): {e}")
            except Exception as e:
                logger.warning(f"[{self.mac_address}] Error (attempt {attempt + 1}): {e}")
            
            # Cleanup failed attempt
            if self.client:
                try:
                    await self.client.disconnect()
                except:
                    pass
                self.client = None
        
        logger.error(f"[{self.mac_address}] Failed to connect after 3 attempts")
        return False
    
    def _on_disconnect(self, client: BleakClient):
        """Callback when disconnected."""
        logger.warning(f"[{self.mac_address}] Disconnected!")
        self._connected = False
    
    async def _setup_characteristics(self):
        """Find the write and notify characteristics."""
        self._notify_char = None
        self._write_char = None
        
        # Standard Renogy BLE UUIDs
        STANDARD_WRITE = "0000ffd1-0000-1000-8000-00805f9b34fb"
        STANDARD_NOTIFY = "0000fff1-0000-1000-8000-00805f9b34fb"
        
        logger.debug(f"[{self.mac_address}] Discovering characteristics...")
        for service in self.client.services:
            logger.debug(f"[{self.mac_address}]   Service: {service.uuid}")
            for char in service.characteristics:
                props = ','.join(char.properties)
                logger.debug(f"[{self.mac_address}]     Char: {char.uuid} [{props}]")
                
                # Check for exact match to standard UUIDs first
                if char.uuid.lower() == STANDARD_WRITE.lower():
                    self._write_char = char.uuid
                    logger.info(f"[{self.mac_address}] Found standard write char: {char.uuid}")
                elif char.uuid.lower() == STANDARD_NOTIFY.lower():
                    self._notify_char = char.uuid
                    logger.info(f"[{self.mac_address}] Found standard notify char: {char.uuid}")
        
        # Fall back to defaults if not found
        if not self._notify_char:
            self._notify_char = NOTIFY_CHAR_UUID
            logger.warning(f"[{self.mac_address}] Using default notify char: {self._notify_char}")
        if not self._write_char:
            self._write_char = WRITE_CHAR_UUID
            logger.warning(f"[{self.mac_address}] Using default write char: {self._write_char}")
        
        logger.info(f"[{self.mac_address}] Final - Write: {self._write_char}, Notify: {self._notify_char}")
    
    async def disconnect(self):
        """Disconnect from the device."""
        self._connected = False
        if self.client:
            try:
                await self.client.stop_notify(self._notify_char)
            except:
                pass
            try:
                await self.client.disconnect()
            except:
                pass
            self.client = None
        logger.info(f"[{self.mac_address}] Disconnected")
    
    async def read_registers(self, device_id: int, register: int, word_count: int) -> Optional[bytes]:
        """
        Read registers from a device on this BT module.
        
        Args:
            device_id: Modbus device ID
            register: Starting register address
            word_count: Number of words to read
            
        Returns:
            Response bytes or None on failure
        """
        self._ensure_async_primitives()
        
        async with self._lock:
            if not self.is_connected:
                logger.warning(f"[{self.mac_address}] Not connected, attempting reconnect...")
                if not await self.connect():
                    return None
            
            # Clear notification buffer
            self._notification_data.clear()
            self._notification_event.clear()
            
            # Create and send request
            request = create_modbus_read_request(device_id, 0x03, register, word_count)
            logger.debug(f"[{self.mac_address}] Sending request (dev={device_id}, reg={register}, words={word_count}): {request.hex()}")
            
            try:
                await self.client.write_gatt_char(self._write_char, request)
                logger.debug(f"[{self.mac_address}] Request sent to {self._write_char}")
            except Exception as e:
                logger.error(f"[{self.mac_address}] Write failed: {e}")
                self._connected = False
                return None
            
            # Wait for response
            try:
                await asyncio.wait_for(self._notification_event.wait(), timeout=NOTIFICATION_TIMEOUT)
                logger.debug(f"[{self.mac_address}] Notification received")
            except asyncio.TimeoutError:
                logger.warning(f"[{self.mac_address}] Timeout waiting for response (reg={register}, dev_id={device_id})")
                return None
            
            # Wait a bit for all data to arrive
            await asyncio.sleep(0.3)
            
            response = bytes(self._notification_data)
            if response:
                logger.debug(f"[{self.mac_address}] Response ({len(response)} bytes): {response.hex()}")
            else:
                logger.debug(f"[{self.mac_address}] Response: empty")
            
            return response if len(response) >= 5 else None
    
    async def poll_device(self, config: DeviceConfig) -> Dict[str, Any]:
        """
        Poll a specific device on this BT module.
        
        Args:
            config: Device configuration
            
        Returns:
            Dictionary of parsed data
        """
        # Convert string device_type to enum
        device_type_enum = config.get_device_type_enum()
        registers = get_registers_for_device(device_type_enum)
        logger.debug(f"[{config.name}] Device type: {device_type_enum}, Reading {len(registers)} register groups")
        
        if not registers:
            logger.error(f"[{config.name}] No registers defined for device type: {config.device_type}")
            return {}
        
        all_data = {}
        
        for reg_info in registers:
            logger.debug(f"[{config.name}] Reading {reg_info['name']} (reg={reg_info['register']}, words={reg_info['words']})")
            
            response = await self.read_registers(
                config.device_id,
                reg_info['register'],
                reg_info['words']
            )
            
            if response:
                if validate_modbus_response(response, config.device_id):
                    parsed = parse_response(device_type_enum, reg_info['register'], response)
                    all_data.update(parsed)
                    logger.debug(f"[{config.name}] {reg_info['name']}: parsed {len(parsed)} fields")
                else:
                    logger.warning(f"[{config.name}] Invalid response for {reg_info['name']}: {response.hex()}")
            else:
                logger.debug(f"[{config.name}] No response for {reg_info['name']}")
            
            await asyncio.sleep(REQUEST_DELAY)
        
        if all_data:
            all_data['__device'] = config.name
            all_data['__mac_address'] = config.mac_address
            all_data['__device_type'] = config.device_type
            logger.info(f"[{config.name}] Got {len(all_data) - 3} data fields")
        else:
            logger.warning(f"[{config.name}] No data received from any registers")
        
        return all_data


class BLEDeviceManager:
    """
    Manages persistent BLE connections to multiple Renogy BT modules.
    """
    
    def __init__(self, device_configs: List[DeviceConfig], on_data_callback: Callable = None):
        # Group devices by MAC address (for Hub mode)
        self._connections: Dict[str, PersistentBLEConnection] = {}
        self._device_data: Dict[str, DeviceData] = {}
        
        # Create connections and device data
        devices_by_mac: Dict[str, List[DeviceConfig]] = {}
        for config in device_configs:
            mac = config.mac_address.upper()
            if mac not in devices_by_mac:
                devices_by_mac[mac] = []
            devices_by_mac[mac].append(config)
            
            # Create device data entry
            device_key = f"{mac}_{config.device_type}"
            self._device_data[device_key] = DeviceData(config=config)
        
        # Create persistent connections
        for mac, configs in devices_by_mac.items():
            self._connections[mac] = PersistentBLEConnection(mac, configs)
            if len(configs) > 1:
                logger.info(f"Hub mode: {len(configs)} devices on {mac}")
        
        self.on_data_callback = on_data_callback
        self._running = False
        
        logger.info(f"Device manager: {len(self._device_data)} devices on {len(self._connections)} BT modules")
    
    async def connect_all(self) -> int:
        """
        Connect to all BT modules.
        
        Returns:
            Number of successful connections
        """
        connected = 0
        for mac, connection in self._connections.items():
            logger.info(f"Connecting to BT module: {mac}")
            if await connection.connect():
                connected += 1
            else:
                logger.error(f"Failed to connect to: {mac}")
            # Delay between connections to different modules
            await asyncio.sleep(3.0)
        
        return connected
    
    async def disconnect_all(self):
        """Disconnect from all BT modules."""
        for connection in self._connections.values():
            await connection.disconnect()
    
    async def poll_all(self) -> Dict[str, Dict[str, Any]]:
        """
        Poll all devices.
        
        Returns:
            Dictionary mapping device keys to data
        """
        results = {}
        
        for mac, connection in self._connections.items():
            # Check if connected, reconnect if needed
            if not connection.is_connected:
                logger.warning(f"[{mac}] Not connected, reconnecting...")
                if not await connection.connect():
                    logger.error(f"[{mac}] Reconnection failed")
                    # Mark all devices on this module as failed
                    for config in connection.device_configs:
                        device_key = f"{mac}_{config.device_type}"
                        self._device_data[device_key].mark_failed()
                    continue
            
            # Poll each device on this module
            for config in connection.device_configs:
                device_key = f"{mac}_{config.device_type}"
                logger.info(f"Polling: {config.name} (type={config.device_type}, id={config.device_id})")
                
                try:
                    data = await connection.poll_device(config)
                    
                    if data:
                        meaningful_keys = [k for k in data.keys() if not k.startswith('__')]
                        logger.info(f"  {config.name}: {len(meaningful_keys)} data points")
                        
                        self._device_data[device_key].update(data)
                        results[device_key] = data
                        
                        if self.on_data_callback:
                            await self.on_data_callback(device_key, data)
                    else:
                        logger.warning(f"  {config.name}: No data")
                        self._device_data[device_key].mark_failed()
                        
                except Exception as e:
                    logger.error(f"  {config.name}: Error - {e}")
                    self._device_data[device_key].mark_failed()
                
                # Small delay between devices
                await asyncio.sleep(1.0)
            
            # Delay between BT modules
            await asyncio.sleep(2.0)
        
        return results
    
    async def poll_once(self) -> Dict[str, Dict[str, Any]]:
        """Poll all devices once."""
        return await self.poll_all()
    
    def get_device_data(self, device_key: str) -> Optional[DeviceData]:
        """Get data for a specific device."""
        return self._device_data.get(device_key)
    
    def get_all_device_data(self) -> Dict[str, DeviceData]:
        """Get data for all devices."""
        return self._device_data
    
    async def start(self, poll_interval: int = 60):
        """Start the polling loop with persistent connections."""
        self._running = True
        
        # Initial connection to all BT modules
        connected = await self.connect_all()
        logger.info(f"Connected to {connected}/{len(self._connections)} BT modules")
        
        # Polling loop - stay connected and just poll
        while self._running:
            await self.poll_all()
            
            # Wait for next poll
            await asyncio.sleep(poll_interval)
    
    async def stop(self):
        """Stop polling and disconnect."""
        self._running = False
        await self.disconnect_all()
        logger.info("Device manager stopped")


async def scan_for_devices(timeout: float = 15.0, show_all: bool = False, adapter: str = None) -> List[Dict]:
    """
    Scan for nearby Renogy BLE devices.
    """
    if adapter is None:
        adapter = get_bt_adapter()
    
    logger.info(f"Scanning for BLE devices on {adapter} (timeout: {timeout}s)...")
    
    try:
        devices = await BleakScanner.discover(timeout=timeout, adapter=adapter)
    except Exception as e:
        logger.error(f"Scan failed: {e}")
        return []
    
    results = []
    for device in devices:
        name = device.name or ""
        
        if show_all or name.startswith("BT-TH") or "Renogy" in name.upper():
            results.append({
                'name': name,
                'address': device.address,
                'rssi': device.rssi if hasattr(device, 'rssi') else None
            })
    
    results.sort(key=lambda x: x.get('rssi') or -100, reverse=True)
    
    logger.info(f"Found {len(results)} {'total' if show_all else 'Renogy'} devices")
    return results

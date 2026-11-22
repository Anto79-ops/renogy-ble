"""
BLE communication module for Renogy devices.
Uses persistent connections for reliability.
"""

import asyncio
import glob
import logging
import re
import subprocess
import time
from collections import deque
from typing import Dict, List, Optional, Callable, Any, Tuple
from dataclasses import dataclass, field
from datetime import datetime

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError

from utils import create_modbus_read_request, validate_modbus_response, find_modbus_frame
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

# Automatic recovery. When a BT module has had no working link for this many
# polls in a row, the Bluetooth adapter is reset and every module reconnected
# (the same thing a service restart does). A reset also interrupts the modules
# that still work, so the wait between two resets doubles each time, up to the
# maximum, and starts over once every module is answering again.
RESET_AFTER_FAILED_POLLS = 3
RESET_MIN_INTERVAL = 600.0
RESET_MAX_INTERVAL = 3600.0

# Connection diagnostics: reconnects and read timeouts are counted over this
# many seconds
STATS_WINDOW = 86400.0

# Default Bluetooth adapter
DEFAULT_BT_ADAPTER = "hci0"


def reset_bluetooth_adapter(adapter: str = None) -> bool:
    """
    Reset the Bluetooth adapter to ensure it's in a clean state.
    This helps with connection issues after service restarts.
    
    Args:
        adapter: The adapter name (e.g., 'hci0', 'hci1'). If None, uses configured adapter.
    
    Returns:
        True if reset was successful, False otherwise.
    """
    if adapter is None:
        adapter = get_bt_adapter()
    
    logger.info(f"Resetting Bluetooth adapter {adapter}...")
    
    try:
        # First, check if adapter exists
        result = subprocess.run(
            ['hciconfig', adapter],
            capture_output=True,
            text=True,
            timeout=10
        )
        
        if result.returncode != 0:
            logger.error(f"Adapter {adapter} not found: {result.stderr}")
            # Try to find available adapters
            list_result = subprocess.run(['hciconfig', '-a'], capture_output=True, text=True, timeout=10)
            logger.info(f"Available adapters:\n{list_result.stdout}")
            return False
        
        # A soft-blocked radio (the default for USB adapters on Raspberry Pi OS)
        # cannot be brought up
        _unblock_adapter(adapter)
        
        # Bring adapter down
        logger.debug(f"Bringing {adapter} down...")
        subprocess.run(
            ['sudo', 'hciconfig', adapter, 'down'],
            capture_output=True,
            timeout=10
        )
        time.sleep(2)
        
        # Bring adapter back up
        logger.debug(f"Bringing {adapter} up...")
        subprocess.run(
            ['sudo', 'hciconfig', adapter, 'up'],
            capture_output=True,
            timeout=10
        )
        time.sleep(3)
        
        # Verify adapter is up, retry if needed
        for attempt in range(5):
            result = subprocess.run(
                ['hciconfig', adapter],
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if 'UP RUNNING' in result.stdout:
                logger.info(f"Bluetooth adapter {adapter} reset successfully")
                return True
            
            logger.debug(f"Adapter not ready (attempt {attempt + 1}/5), waiting...")
            
            # Try bringing it up again
            subprocess.run(
                ['sudo', 'hciconfig', adapter, 'up'],
                capture_output=True,
                timeout=10
            )
            time.sleep(2)
        
        # Final check
        result = subprocess.run(
            ['hciconfig', adapter],
            capture_output=True,
            text=True,
            timeout=10
        )
        
        if 'UP RUNNING' in result.stdout:
            logger.info(f"Bluetooth adapter {adapter} is up")
            return True
        else:
            logger.error(f"Adapter {adapter} failed to come up: {result.stdout}")
            return False
            
    except subprocess.TimeoutExpired:
        logger.error(f"Timeout while resetting {adapter}")
        return False
    except FileNotFoundError:
        logger.warning("hciconfig not found, trying bluetoothctl...")
        try:
            # Alternative: use bluetoothctl
            subprocess.run(['sudo', 'bluetoothctl', 'power', 'off'], capture_output=True, timeout=5)
            time.sleep(1)
            subprocess.run(['sudo', 'bluetoothctl', 'power', 'on'], capture_output=True, timeout=5)
            time.sleep(2)
            logger.info("Bluetooth reset via bluetoothctl")
            return True
        except Exception as e:
            logger.error(f"Failed to reset Bluetooth: {e}")
            return False
    except Exception as e:
        logger.error(f"Error resetting Bluetooth adapter: {e}")
        return False


def restart_bluetooth_service() -> bool:
    """
    Restart the Bluetooth service completely.
    Use this as a last resort if adapter reset doesn't work.
    
    Returns:
        True if restart was successful, False otherwise.
    """
    logger.info("Restarting Bluetooth service...")
    
    try:
        # Stop bluetooth service
        subprocess.run(
            ['sudo', 'systemctl', 'restart', 'bluetooth'],
            capture_output=True,
            timeout=30
        )
        time.sleep(3)
        
        # Check if service is running
        result = subprocess.run(
            ['systemctl', 'is-active', 'bluetooth'],
            capture_output=True,
            text=True,
            timeout=10
        )
        
        if result.stdout.strip() == 'active':
            logger.info("Bluetooth service restarted successfully")
            return True
        else:
            logger.warning(f"Bluetooth service status: {result.stdout.strip()}")
            return False
            
    except Exception as e:
        logger.error(f"Error restarting Bluetooth service: {e}")
        return False


def _unblock_adapter(adapter: str):
    """Lift an rfkill soft block on one adapter, if there is one."""
    for path in glob.glob(f'/sys/class/bluetooth/{adapter}/rfkill*'):
        try:
            with open(f'{path}/soft') as f:
                blocked = f.read().strip() == '1'
            if blocked:
                index = path.rsplit('rfkill', 1)[1]
                logger.warning(f"Adapter {adapter} is blocked by rfkill, unblocking it")
                subprocess.run(['sudo', 'rfkill', 'unblock', index], capture_output=True, timeout=10)
                time.sleep(1)
        except Exception as e:
            logger.debug(f"Could not check rfkill for {adapter}: {e}")


def list_bt_adapters() -> Dict[str, str]:
    """The Bluetooth adapters present, as {name: address}, e.g. {'hci0': '2C:CF:...'}."""
    try:
        output = subprocess.run(['hciconfig'], capture_output=True, text=True, timeout=10).stdout
    except Exception as e:
        logger.debug(f"Could not list Bluetooth adapters: {e}")
        return {}
    adapters: Dict[str, str] = {}
    name = None
    for line in output.splitlines():
        match = re.match(r'(hci\d+):', line)
        if match:
            name = match.group(1)
            adapters[name] = ''
        elif name and 'BD Address:' in line:
            adapters[name] = line.split('BD Address:')[1].split()[0].upper()
    return adapters


def resolve_adapter(spec: Optional[str]) -> Optional[str]:
    """
    Turn an adapter setting into the adapter name to use right now.

    The setting is a name ("hci1") or the adapter's own Bluetooth address
    ("AA:BB:CC:DD:EE:02"). Names can swap between boots when there are two
    adapters; an address always means the same hardware.

    Returns:
        The adapter name, or None when that adapter is not present.
    """
    if not spec:
        return None
    spec = str(spec).strip()
    adapters = list_bt_adapters()
    if ':' in spec:
        for name, address in adapters.items():
            if address == spec.upper():
                return name
        return None
    if adapters and spec not in adapters:
        return None
    return spec


def get_bt_adapter() -> str:
    """Get the default Bluetooth adapter (the name in use right now)."""
    spec = getattr(get_bt_adapter, '_adapter', DEFAULT_BT_ADAPTER)
    if ':' not in spec:
        return spec
    return resolve_adapter(spec) or DEFAULT_BT_ADAPTER


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
    adapter: str = 'bt2'  # the BT module type, not the Bluetooth adapter
    # Bluetooth adapter for this device's BT module (name or address);
    # None = the default adapter
    bt_adapter: Optional[str] = None
    
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
        # (device_id, register) of 'once' register groups already read
        self._static_read: set = set()
        # Polls in a row without a working link (see BLEDeviceManager)
        self.failed_cycles = 0
        # Bluetooth adapter: this module's own setting, else the default
        self._adapter_spec = next((c.bt_adapter for c in device_configs if c.bt_adapter), None)
        self._adapter_missing = False
        self.adapter = self._resolve_adapter()
        # Link drops seen while a connection attempt was in progress
        self._attempt_drops = 0
        # Connection diagnostics, published to MQTT by the monitor
        self._ever_connected = False
        self.reconnect_times: deque = deque(maxlen=2000)
        self.timeout_times: Dict[int, deque] = {}
        # Last issue of the module (key None) and of each device on it
        self._issues: Dict[Optional[int], Tuple[datetime, str]] = {}
        
    @property
    def is_connected(self) -> bool:
        return self._connected and self.client and self.client.is_connected
    
    def note_issue(self, text: str, device_id: Optional[int] = None):
        """Remember the latest connection issue, for the whole BT module or
        for one device on it."""
        self._issues[device_id] = (datetime.now().astimezone(), text)
    
    def last_issue(self, device_id: int) -> Optional[Tuple[datetime, str]]:
        """The most recent issue that concerns this device."""
        found = [i for i in (self._issues.get(None), self._issues.get(device_id)) if i]
        return max(found, key=lambda issue: issue[0]) if found else None
    
    def _resolve_adapter(self) -> str:
        """The adapter to use now. Falls back to the default when this
        module's own adapter is not present (for example, unplugged)."""
        default = get_bt_adapter()
        if not self._adapter_spec:
            return default
        name = resolve_adapter(self._adapter_spec)
        if name:
            self._adapter_missing = False
            return name
        if not self._adapter_missing:
            logger.warning(
                f"[{self.mac_address}] Bluetooth adapter {self._adapter_spec} not found, "
                f"using {default} instead"
            )
            self._adapter_missing = True
        return default
    
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
    
    async def connect(self, attempts: int = 3) -> bool:
        """
        Establish connection to the BT module.

        Args:
            attempts: How many times to try. One attempt takes up to about
                35 s when the module does not answer.
        """
        self._ensure_async_primitives()
        
        if self.is_connected:
            return True
        
        adapter = self.adapter = self._resolve_adapter()
        last_error = "BT module not found"
        
        for attempt in range(attempts):
            if attempt > 0:
                logger.info(f"[{self.mac_address}] Retry {attempt + 1}/{attempts}...")
                await asyncio.sleep(5.0)
            
            self._attempt_drops = 0
            try:
                logger.info(f"[{self.mac_address}] Connecting using {adapter}...")
                if await self._connect_once(adapter):
                    self._connected = True
                    logger.info(f"[{self.mac_address}] Connected successfully")
                    if self._ever_connected:
                        self.reconnect_times.append(time.time())
                        self.note_issue("Bluetooth link dropped and was re-established")
                    self._ever_connected = True
                    return True
            except Exception as e:
                # The module was reached but the link did not hold. BlueZ keeps
                # retrying inside one attempt; report how often instead of
                # logging every drop.
                kind = "BLE error" if isinstance(e, BleakError) else "Error"
                detail = str(e) or type(e).__name__
                drops = f" (link dropped {self._attempt_drops}x)" if self._attempt_drops else ""
                last_error = f"{detail}{drops}"
                logger.warning(f"[{self.mac_address}] {kind} (attempt {attempt + 1}): {last_error}")
            
            # Cleanup failed attempt
            if self.client:
                try:
                    await self.client.disconnect()
                except Exception:
                    pass
                self.client = None
        
        logger.error(f"[{self.mac_address}] Failed to connect after {attempts} attempt{'s' if attempts != 1 else ''}")
        self.note_issue(f"Could not connect over {adapter}: {last_error}")
        return False
    
    async def _connect_once(self, adapter: str) -> bool:
        """One connection attempt. Returns False or raises when it fails."""
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
            return False
        
        # Connect
        self.client = BleakClient(
            device, 
            timeout=CONNECTION_TIMEOUT,
            disconnected_callback=self._on_disconnect
        )
        await self.client.connect()
        
        if not self.client.is_connected:
            logger.warning(f"[{self.mac_address}] Connection failed")
            return False
        
        # Discover characteristics
        await self._setup_characteristics()
        
        # Subscribe to notifications
        await self.client.start_notify(self._notify_char, self._notification_handler)
        return True
    
    def _on_disconnect(self, client: BleakClient):
        """Callback when disconnected."""
        if client is not self.client:
            return  # a client from an earlier, abandoned attempt
        if self._connected:
            logger.warning(f"[{self.mac_address}] Disconnected!")
        else:
            # Still connecting: counted and reported once by connect()
            self._attempt_drops += 1
            logger.debug(f"[{self.mac_address}] Link dropped while connecting")
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
            except Exception:
                pass
            try:
                await self.client.disconnect()
            except Exception:
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
            Response bytes or None on failure. Does not reconnect: poll_device
            decides that, so a lost link costs one attempt per poll instead of
            one per register.
        """
        self._ensure_async_primitives()
        
        async with self._lock:
            if not self.is_connected:
                logger.debug(f"[{self.mac_address}] Not connected, skipping reg={register}")
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
            
            # Wait until the reply to THIS request is in the buffer. Bytes that
            # do not belong to it (a late reply to an earlier request) are
            # skipped rather than parsed as this register block.
            loop = asyncio.get_running_loop()
            deadline = loop.time() + NOTIFICATION_TIMEOUT
            frame = None
            while True:
                frame = find_modbus_frame(self._notification_data, device_id, word_count)
                if frame is not None:
                    break
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                self._notification_event.clear()
                try:
                    await asyncio.wait_for(self._notification_event.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    break

            buffered = bytes(self._notification_data)
            if frame is None:
                if buffered:
                    logger.warning(
                        f"[{self.mac_address}] No matching reply (reg={register}, dev_id={device_id}); "
                        f"discarded {len(buffered)} unrelated byte(s): {buffered.hex()}"
                    )
                else:
                    logger.warning(f"[{self.mac_address}] Timeout waiting for response (reg={register}, dev_id={device_id})")
                self.timeout_times.setdefault(device_id, deque(maxlen=2000)).append(time.time())
                self.note_issue(f"No reply to register {register}", device_id)
                return None

            if len(buffered) != len(frame):
                logger.info(
                    f"[{self.mac_address}] Skipped {len(buffered) - len(frame)} stray byte(s) "
                    f"around the reply to reg={register}: {buffered.hex()}"
                )
            logger.debug(f"[{self.mac_address}] Response ({len(frame)} bytes): {frame.hex()}")
            return frame
    
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
        reconnect_tried = False
        
        for reg_info in registers:
            if reg_info.get('once') and (config.device_id, reg_info['register']) in self._static_read:
                continue  # static values: already published, kept by the monitor
            
            if not self.is_connected:
                # The link went down during this poll. Try once to get it back;
                # if that fails, leave the remaining registers for the next poll.
                if reconnect_tried:
                    logger.warning(f"[{config.name}] Link lost again, skipping the rest of this poll")
                    break
                reconnect_tried = True
                logger.warning(f"[{self.mac_address}] Not connected, attempting reconnect...")
                if not await self.connect(attempts=1):
                    logger.warning(f"[{config.name}] Reconnect failed, skipping the rest of this poll")
                    break
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
                    if reg_info.get('once') and parsed:
                        self._static_read.add((config.device_id, reg_info['register']))
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
    
    def __init__(self, device_configs: List[DeviceConfig], on_data_callback: Callable = None,
                 auto_reset: bool = True, reset_after: int = RESET_AFTER_FAILED_POLLS):
        # Automatic adapter reset (see RESET_AFTER_FAILED_POLLS above)
        self.auto_reset = auto_reset
        self.reset_after = max(1, int(reset_after))
        self.adapter_resets = 0
        self._adapter_resets: Dict[str, int] = {}  # automatic resets per adapter
        # Per adapter: [wait before the next reset, earliest time of that reset]
        self._reset_state: Dict[str, List[float]] = {}
        
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
            if len({c.bt_adapter for c in configs if c.bt_adapter}) > 1:
                logger.warning(
                    f"Devices on {mac} name different Bluetooth adapters; "
                    f"using {self._connections[mac].adapter} for all of them"
                )
        
        self.on_data_callback = on_data_callback
        self._running = False
        
        logger.info(f"Device manager: {len(self._device_data)} devices on {len(self._connections)} BT modules")
    
    def adapters(self) -> List[str]:
        """The Bluetooth adapters in use."""
        return sorted({c.adapter for c in self._connections.values()})
    
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
        
        # Modules with a working link go first, so a module that will not
        # reconnect cannot hold back the readings of the others.
        ordered = sorted(self._connections.items(), key=lambda item: not item[1].is_connected)
        
        for mac, connection in ordered:
            # Check if connected, reconnect if needed
            if not connection.is_connected:
                logger.warning(f"[{mac}] Not connected, reconnecting...")
                # Three tries the first time; after a failed poll one try per
                # poll is enough, the next poll is the retry.
                attempts = 3 if connection.failed_cycles == 0 else 1
                if not await connection.connect(attempts=attempts):
                    logger.error(f"[{mac}] Reconnection failed")
                    connection.failed_cycles += 1
                    # Mark all devices on this module as failed
                    for config in connection.device_configs:
                        device_key = f"{mac}_{config.device_type}"
                        self._device_data[device_key].mark_failed()
                    continue
            
            # Poll each device on this module
            got_data = False
            for config in connection.device_configs:
                device_key = f"{mac}_{config.device_type}"
                logger.info(f"Polling: {config.name} (type={config.device_type}, id={config.device_id})")
                
                try:
                    data = await connection.poll_device(config)
                    
                    if data:
                        got_data = True
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
            
            if got_data:
                connection.failed_cycles = 0
            elif not connection.is_connected:
                # Connected, then lost the link before any reading came back
                connection.failed_cycles += 1
            
            # Delay between BT modules
            await asyncio.sleep(2.0)
        
        await self._recover_if_needed()
        
        return results
    
    async def _recover_if_needed(self):
        """Reset a Bluetooth adapter when a module on it has been unreachable too long."""
        if not self.auto_reset:
            return
        
        for adapter in self.adapters():
            members = {mac: c for mac, c in self._connections.items() if c.adapter == adapter}
            state = self._reset_state.setdefault(adapter, [RESET_MIN_INTERVAL, 0.0])
            stuck = [mac for mac, c in members.items() if c.failed_cycles >= self.reset_after]
            if not stuck:
                if all(c.failed_cycles == 0 for c in members.values()):
                    state[0] = RESET_MIN_INTERVAL  # all well: start over
                continue
            
            now = time.monotonic()
            if now < state[1]:
                continue
            
            logger.warning(
                f"No link to {', '.join(stuck)} for {self.reset_after} or more polls in a row: "
                f"resetting Bluetooth adapter {adapter} "
                f"(the next reset is at least {state[0] / 60:.0f} min away)"
            )
            self.adapter_resets += 1
            self._adapter_resets[adapter] = self._adapter_resets.get(adapter, 0) + 1
            state[1] = now + state[0]
            state[0] = min(state[0] * 2, RESET_MAX_INTERVAL)
            
            # Only the modules on this adapter are interrupted
            for connection in members.values():
                await connection.disconnect()
            
            # The reset shells out and sleeps: keep it off the event loop
            if not await asyncio.to_thread(reset_bluetooth_adapter, adapter):
                logger.warning("Bluetooth adapter reset failed, trying service restart...")
                await asyncio.to_thread(restart_bluetooth_service)
                await asyncio.sleep(3.0)
            
            # Reconnect, working modules first. A module that still does not
            # answer keeps its failure count, so only the wait above delays
            # the next reset.
            names = ', '.join(c.name for mac in stuck for c in members[mac].device_configs)
            for mac, connection in sorted(members.items(), key=lambda item: item[0] in stuck):
                logger.info(f"Connecting to BT module: {mac}")
                back = await connection.connect(attempts=1 if mac in stuck else 3)
                if not back:
                    logger.error(f"Failed to connect to: {mac}")
                # Written last, so it is what the diagnostics show: cause and outcome
                connection.note_issue(
                    f"Bluetooth adapter {adapter} was reset because {names} did not answer for "
                    f"{self.reset_after} polls: {'link is back' if back else 'still no link'}"
                )
                await asyncio.sleep(3.0)
    
    async def poll_once(self) -> Dict[str, Dict[str, Any]]:
        """Poll all devices once."""
        return await self.poll_all()
    
    def connection_stats(self, device_key: str) -> Dict[str, Any]:
        """Connection diagnostics of one device, for publishing."""
        device = self._device_data[device_key]
        config = device.config
        connection = self._connections[config.mac_address.upper()]
        cutoff = time.time() - STATS_WINDOW
        issue = connection.last_issue(config.device_id)
        return {
            'adapter': connection.adapter,
            # automatic resets of this device's adapter since the monitor started
            'adapter_resets': self._adapter_resets.get(connection.adapter, 0),
            'reconnects_24h': sum(1 for t in connection.reconnect_times if t >= cutoff),
            'failed_polls': device.consecutive_failures,
            'timeouts_24h': sum(1 for t in connection.timeout_times.get(config.device_id, ()) if t >= cutoff),
            'last_issue': issue[1] if issue else None,
            'last_issue_time': issue[0].isoformat(timespec='seconds') if issue else None,
        }
    
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

"""
Utility functions for Renogy BT Monitor.
Includes Modbus CRC calculation and byte parsing helpers.
"""

import logging
from typing import Union, Optional

logger = logging.getLogger(__name__)


def modbus_crc16(data: bytes) -> tuple:
    """
    Calculate the Modbus CRC16 checksum.
    
    Args:
        data: Bytes to calculate CRC for
        
    Returns:
        Tuple of (crc_low, crc_high) bytes
    """
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return (crc & 0xFF, (crc >> 8) & 0xFF)


def create_modbus_read_request(device_id: int, function_code: int, 
                                register: int, word_count: int) -> bytearray:
    """
    Create a Modbus read request frame.
    
    Frame format:
    [device_id, function_code, register_hi, register_lo, 
     word_count_hi, word_count_lo, crc_lo, crc_hi]
    
    Args:
        device_id: Modbus device address (1-247, or 255 for broadcast)
        function_code: Modbus function code (typically 3 for read holding registers)
        register: Starting register address
        word_count: Number of 16-bit words to read
        
    Returns:
        Bytearray containing the complete Modbus frame
    """
    frame = bytearray([
        device_id,
        function_code,
        (register >> 8) & 0xFF,
        register & 0xFF,
        (word_count >> 8) & 0xFF,
        word_count & 0xFF,
    ])
    crc_low, crc_high = modbus_crc16(frame)
    frame.extend([crc_low, crc_high])
    logger.debug(f"Created Modbus request: register={register}, frame={list(frame)}")
    return frame


def bytes_to_int(data: bytes, offset: int, length: int, 
                  scale: float = 1.0, signed: bool = False) -> Union[int, float]:
    """
    Convert bytes to integer or float value.
    
    Args:
        data: Raw byte data
        offset: Starting byte offset
        length: Number of bytes (1, 2, or 4)
        scale: Scale factor to apply
        signed: Whether to interpret as signed integer
        
    Returns:
        Integer or float value
    """
    if offset + length > len(data):
        logger.warning(f"Data too short: offset={offset}, length={length}, data_len={len(data)}")
        return 0
    
    if length == 1:
        value = data[offset]
        if signed and value > 127:
            value -= 256
    elif length == 2:
        value = (data[offset] << 8) | data[offset + 1]
        if signed and value > 32767:
            value -= 65536
    elif length == 4:
        value = ((data[offset] << 24) | (data[offset + 1] << 16) | 
                 (data[offset + 2] << 8) | data[offset + 3])
        if signed and value > 2147483647:
            value -= 4294967296
    else:
        logger.warning(f"Unsupported byte length: {length}")
        return 0
    
    if scale != 1.0:
        return round(value * scale, 3)
    return value


def bytes_to_ascii(data: bytes, offset: int, length: int) -> str:
    """
    Convert bytes to ASCII string.
    
    Args:
        data: Raw byte data
        offset: Starting byte offset
        length: Number of bytes
        
    Returns:
        ASCII string (null characters and control chars stripped)
    """
    if offset + length > len(data):
        return ""
    
    result = ""
    for i in range(length):
        char = data[offset + i]
        if 32 <= char <= 126:  # Printable ASCII
            result += chr(char)
    return result.strip()


def parse_temperature(raw_value: int, offset: int = 0) -> float:
    """
    Parse temperature value with sign handling.
    Renogy devices sometimes encode temperature with sign bit.
    
    Args:
        raw_value: Raw temperature value
        offset: Optional offset to subtract (e.g., for Kelvin to Celsius)
        
    Returns:
        Temperature in Celsius
    """
    if raw_value > 127:
        return -(raw_value - 128) - offset
    return raw_value - offset


def validate_modbus_response(data: bytes, expected_device_id: int = None) -> bool:
    """
    Validate a Modbus response frame.
    
    Args:
        data: Response data
        expected_device_id: Expected device ID (optional)
        
    Returns:
        True if response is valid
    """
    if len(data) < 5:
        logger.warning(f"Response too short: {len(data)} bytes")
        return False
    
    # Check for error response
    if data[1] & 0x80:
        error_code = data[2] if len(data) > 2 else 0
        logger.warning(f"Modbus error response: function={data[1]}, error_code={error_code}")
        return False
    
    # Validate CRC
    if len(data) >= 5:
        byte_count = data[2]
        expected_len = 3 + byte_count + 2
        if len(data) < expected_len:
            logger.warning(f"Response incomplete: got {len(data)}, expected {expected_len}")
            return False
        
        # Verify CRC
        payload = data[:expected_len - 2]
        received_crc = (data[expected_len - 1] << 8) | data[expected_len - 2]
        crc_low, crc_high = modbus_crc16(payload)
        calculated_crc = (crc_high << 8) | crc_low
        
        if received_crc != calculated_crc:
            logger.warning(f"CRC mismatch: received={hex(received_crc)}, calculated={hex(calculated_crc)}")
            return False
    
    return True


def find_modbus_frame(buffer: bytes, device_id: int, word_count: int,
                      function_code: int = 0x03) -> Optional[bytes]:
    """
    Find the reply to one specific read request inside a notification buffer.

    A reply that arrives late (after its own request timed out) lands in the
    buffer of the NEXT request, in front of the real reply. Taking the buffer
    as-is then parses one frame's bytes as another's registers and publishes
    garbage (seen in practice: max_charging_power_today 70 read as 17920 Wh
    generated). So only a frame that matches the request is accepted:
    slave address (any address when the request used 255), function code,
    byte count == 2 * word_count, and a valid CRC. A Modbus exception reply
    (function | 0x80) for this request is returned as-is so the caller can
    log it.

    Returns:
        The matching frame, or None if the buffer does not contain one (yet)
    """
    want_len = 5 + 2 * word_count
    for i in range(len(buffer)):
        if device_id != 255 and buffer[i] != device_id:
            continue
        if i + 5 > len(buffer):
            break
        func = buffer[i + 1]
        if func == (function_code | 0x80):
            frame = bytes(buffer[i:i + 5])
            if modbus_crc16(frame[:3]) == (frame[3], frame[4]):
                return frame
            continue
        if func != function_code or buffer[i + 2] != 2 * word_count:
            continue
        if i + want_len > len(buffer):
            continue  # matching header, rest not here yet
        frame = bytes(buffer[i:i + want_len])
        if modbus_crc16(frame[:-2]) == (frame[-2], frame[-1]):
            return frame
    return None


def format_mac_address(mac: str) -> str:
    """
    Normalize MAC address format to XX:XX:XX:XX:XX:XX.
    
    Args:
        mac: MAC address in various formats
        
    Returns:
        Normalized MAC address string
    """
    # Remove common separators and convert to uppercase
    mac = mac.replace("-", "").replace(":", "").replace(" ", "").upper()
    
    if len(mac) != 12:
        raise ValueError(f"Invalid MAC address: {mac}")
    
    return ":".join(mac[i:i+2] for i in range(0, 12, 2))

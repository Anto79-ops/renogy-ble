# Renogy BT Monitor (to MQTT)

A standalone Python application for monitoring Renogy solar devices via Bluetooth Low Energy (BLE) and publishing data to an MQTT broker with Home Assistant auto-discovery support. This code was developed for my 3 devices. Renogy Rover 40, Renogy 100 amp hour battery with heating module (RBT100LFP12SH-G1) and 1000 W pure sine wave inverter. Also use 2 BT adapters, BT-1 and BT-2. The battery and inverter are connected via the hub, sharing the same BT-2 module. The BT connections are persistent, meaning once they connect, they will remain connected.  The code also includes alarms and faults for each of the devices as attributes to number of fauls sensors. The code also has a data validator for the charge controller (Rover 40) to prevent spikes in the data. The min, max and max change values can be adjusted in the `data_validator.py` file to suit your system.

## Supported Devices

| Device Type | Model Examples | BT Module | Supported |
|-------------|----------------|-----------|-----------|
| **Charge Controller** | Rover, Wanderer, Adventurer | BT-1, BT-2 | ✅ |
| **Battery** | RBT100LFP12SH-G1 (LiFePO4) | BT-2 | ✅ |
| **Inverter** | RINVTPGH110111S (1000W) | BT-2 | ✅ |

## Features

- 🔌 Connect to multiple Renogy BLE devices simultaneously
- 📡 Publish data to MQTT broker
- 🏠 Home Assistant auto-discovery (devices and entities appear automatically)
- 🔋 Support for charge controllers, batteries, and inverters
- 📊 Comprehensive sensor data (voltage, current, power, SOC, temperature, etc.)
- 🔄 Configurable polling interval
- 📝 Detailed logging
- 🛡️ Automatic reconnection on connection loss

## Hardware Requirements

- **Raspberry Pi Zero 2W** (or any Linux system with Bluetooth)
- **Bluetooth adapter** (built-in or USB, like LM0101 long-range adapter)
- **Renogy BT-1 or BT-2 module(s)** connected to your Renogy devices

## Installation

### 1. System Dependencies (Raspberry Pi OS / Raspbian)

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install Bluetooth and Python dependencies
sudo apt install -y python3 python3-pip python3-venv \
    bluetooth bluez libglib2.0-dev

# Enable Bluetooth service
sudo systemctl enable bluetooth
sudo systemctl start bluetooth
```

### 2. Clone or Download the Application

```bash
# Create directory
mkdir -p ~/renogy_monitor
cd ~/renogy_monitor

# Copy all Python files to this directory
# (main.py, ble_client.py, mqtt_handler.py, parsers.py, utils.py)
```

### 3. Create Python Virtual Environment

```bash
# Create virtual environment
python3 -m venv venv

# Activate virtual environment
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 4. Configure the Application

```bash
# Create sample configuration
python main.py --create-config

# Edit configuration with your settings
nano config.yaml
```

## Configuration

Edit `config.yaml` with your device and MQTT settings:

```yaml
# MQTT Broker Configuration
mqtt:
  host: "homeassistant.local"  # Your Home Assistant IP or hostname
  port: 1883
  username: "your_mqtt_user"   # Leave empty if no authentication
  password: "your_mqtt_pass"
  discovery_prefix: "homeassistant"
  topic_prefix: "renogy"

# Polling Configuration
polling:
  interval: 60  # Seconds between polls (10-600)

# Device Configuration
devices:
  # Charge Controller with BT-1 module
  - name: "Solar Charge Controller"
    mac_address: "XX:XX:XX:XX:XX:XX"
    alias: "BT-TH-FXXXXXXX"
    type: "controller"
    device_id: 255
    adapter: "bt1"

  # Battery with BT-2 module (Hub mode)
  - name: "Main Battery"
    mac_address: "XX:XX:XX:XX:XX:XX"
    alias: "BT-TH-XXXXXXXX"
    type: "battery"
    device_id: 48
    adapter: "bt2"

  # Inverter with BT-2 module (Hub mode)
  - name: "Power Inverter"
    mac_address: "XX:XX:XX:XX:XX:XX"
    alias: "BT-TH-XXXXXXXX"
    type: "inverter"
    device_id: 32
    adapter: "bt2"

# Logging
logging:
  level: "INFO"
  console: true
```

### Device ID Guide

When using **Hub Mode** (multiple devices connected to one BT-2 via Communication Hub):

| Device Type | Standalone | Hub Mode |
|-------------|------------|----------|
| Controller | 255 | 96, 97 |
| Battery | 255 | 48, 49, 50 |
| Inverter | 255, 32 | 32 |

For **separate BT modules** (one per device), use `device_id: 255`.

## Usage

### Scan for Devices

Find your Renogy BT modules:

```bash
# Activate virtual environment
source venv/bin/activate

# Scan for nearby devices
python main.py --scan
```

Example output:
```
Found 2 Renogy device(s):

1. Name: BT-TH-FXXXXXXX
   MAC Address: XX:XX:XX:XX:XX:XX
   Signal Strength: -65 dBm

2. Name: BT-TH-ACCCCCCC
   MAC Address: XX:XX:XX:XX:XX:XX
   Signal Strength: -72 dBm
```

### Run the Monitor

```bash
# Start monitoring (foreground)
python main.py config.yaml

# With debug logging
python main.py config.yaml --debug
```

### Run as a System Service

Create a systemd service for automatic startup:

```bash
sudo nano /etc/systemd/system/renogy-monitor.service
```

Add the following content:

```ini
[Unit]
Description=Renogy BT Monitor
After=network.target bluetooth.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/renogy_monitor
ExecStart=/home/pi/renogy_monitor/venv/bin/python main.py config.yaml
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Enable and start the service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable renogy-monitor
sudo systemctl start renogy-monitor

# Check status
sudo systemctl status renogy-monitor

# View logs
journalctl -u renogy-monitor -f
```

## Home Assistant Integration

### Automatic Discovery

Once the monitor is running and connected to your MQTT broker, devices and entities will automatically appear in Home Assistant under **Settings > Devices & Services > MQTT**.

### Manual MQTT Configuration (Alternative)

If auto-discovery doesn't work, add sensors manually to `configuration.yaml`:

```yaml
mqtt:
  sensor:
    # Battery SOC
    - name: "Solar Battery SOC"
      state_topic: "renogy/solar_charge_controller_xxxxxx/state"
      value_template: "{{ value_json.battery_percentage }}"
      unit_of_measurement: "%"
      device_class: battery

    # PV Power
    - name: "Solar PV Power"
      state_topic: "renogy/solar_charge_controller_xxxxxx/state"
      value_template: "{{ value_json.pv_power }}"
      unit_of_measurement: "W"
      device_class: power

    # Battery Voltage
    - name: "Solar Battery Voltage"
      state_topic: "renogy/solar_charge_controller_xxxxxx/state"
      value_template: "{{ value_json.battery_voltage }}"
      unit_of_measurement: "V"
      device_class: voltage
```

### Energy Dashboard

The sensors are configured with appropriate `state_class` for energy tracking:
- `power_generation_today` - Daily solar generation (Wh)
- `power_generation_total` - Lifetime solar generation (Wh)
- `power_consumption_today` - Daily load consumption (Wh)

## Sensor Reference

### Charge Controller Sensors

| Sensor | Unit | Description |
|--------|------|-------------|
| battery_percentage | % | Battery state of charge |
| battery_voltage | V | Battery voltage |
| battery_current | A | Battery current |
| battery_temperature | °C | Battery temperature |
| pv_voltage | V | Solar panel voltage |
| pv_current | A | Solar panel current |
| pv_power | W | Solar panel power |
| load_voltage | V | Load voltage |
| load_current | A | Load current |
| load_power | W | Load power |
| controller_temperature | °C | Controller temperature |
| charging_status | - | Current charging mode |
| power_generation_today | Wh | Daily generation |
| power_generation_total | Wh | Lifetime generation |

### Battery Sensors

| Sensor | Unit | Description |
|--------|------|-------------|
| voltage | V | Battery voltage |
| current | A | Battery current (+charge/-discharge) |
| power | W | Battery power |
| soc | % | State of charge |
| remaining_capacity | Ah | Remaining capacity |
| total_capacity | Ah | Total capacity |
| cycle_count | cycles | Charge cycle count |
| bms_temperature | °C | BMS board temperature |
| cell_voltages | V | Individual cell voltages |

### Inverter Sensors

| Sensor | Unit | Description |
|--------|------|-------------|
| input_voltage | V | AC input voltage |
| input_current | A | AC input current |
| output_voltage | V | AC output voltage |
| output_current | A | AC output current |
| output_frequency | Hz | Output frequency |
| battery_voltage | V | Battery voltage |
| battery_soc | % | Battery state of charge |
| pv_voltage | V | PV input voltage |
| pv_power | W | PV input power |
| load_percentage | % | Load capacity percentage |
| temperature | °C | Inverter temperature |
| machine_state | - | Operating state |

## Troubleshooting

### Device Not Found

1. Ensure BT module is powered and connected
2. Check Bluetooth service: `sudo systemctl status bluetooth`
3. Verify adapter: `hciconfig`
4. Try running with sudo: `sudo python main.py --scan`

### Connection Timeouts

1. Move closer to the device
2. Increase `CONNECTION_TIMEOUT` in `ble_client.py`
3. Check for Bluetooth interference
4. Try a long-range Bluetooth adapter (like LM0101)

### MQTT Connection Failed

1. Verify broker IP/hostname and port
2. Check username/password
3. Ensure MQTT broker is running
4. Check firewall rules

### No Data Received

1. Verify correct `device_id` (try 255 first)
2. Check device type matches actual device
3. Enable debug logging: `--debug`
4. Check that Renogy app can connect (disconnect app first!)

### Permission Denied (Linux)

```bash
# Add user to bluetooth group
sudo usermod -a -G bluetooth $USER

# Or run with sudo
sudo python main.py config.yaml
```

## Known Limitations

- Only one BLE client can connect to a device at a time (close Renogy app first)
- Raspberry Pi built-in Bluetooth may have limited range
- Some older BT-1 modules may have compatibility issues

## Credits

This project is based on research and code from:
- [cyrils/renogy-bt](https://github.com/cyrils/renogy-bt)
- [Olen/solar-monitor](https://github.com/Olen/solar-monitor)
- [IAmTheMitchell/renogy-ha](https://github.com/IAmTheMitchell/renogy-ha)

Modbus protocol documentation from Renogy/RongSi.
 - https://github.com/cyrils/renogy-bt/discussions/94#discussion-7598651

AI was used to create this project.

## License

MIT License - See LICENSE file for details.

## Disclaimer

This is not an official Renogy product. Use at your own risk. Renogy and all trademarks are property of their respective owners.

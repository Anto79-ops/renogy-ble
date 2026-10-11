# Renogy BT Monitor (to MQTT)

A standalone Python application for monitoring Renogy solar devices via Bluetooth Low Energy (BLE) and publishing data to an MQTT broker with Home Assistant auto-discovery support. This code was developed for my 3 devices. Renogy Rover 40, Renogy 100 amp hour battery with heating module (RBT100LFP12SH-G1) and 1000 W pure sine wave inverter. Also this uses 2 BT adapters, a BT-1 and BT-2. The battery and inverter are connected via the hub, sharing the same BT-2 module. The BT connections are persistent, meaning once they connect, they will remain connected.  The code also includes alarms and faults for each of the devices as attributes and "number of faults" sensors. The code also has a data validator for the charge controller (Rover 40) to prevent spikes in the data. The min, max and max change values can be adjusted in the `data_validator.py` file to suit your system.

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

## Dashboard Example

Included in dashboard yaml file.

Whole Dashboard

<img width="1423" height="875" alt="image" src="https://github.com/user-attachments/assets/e0aeec3c-43be-47bf-aebc-4699dfbbf3a6" />

Battery Alarms

<img width="452" height="631" alt="image" src="https://github.com/user-attachments/assets/c0e2d582-497a-4be2-8be9-bbebbe2b7d1a" />

Data Validation Sensor (last 100 rejections, for Rover 40 Controller only)

<img width="451" height="757" alt="image" src="https://github.com/user-attachments/assets/852480b3-2a67-4048-8d87-28cefb108555" />

## Hardware Requirements

- **Raspberry Pi (Pi Zero 2W, 4 or 5)** (or any Linux system with Bluetooth)
- **Bluetooth adapter** (built-in or USB, like LM0101 long-range adapter)
- **Renogy BT-1 or BT-2 module(s) or both** connected to your Renogy devices

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

# Bluetooth Configuration
bluetooth:
  adapter: "hci0"  # Bluetooth adapter to use (hci0, hci1, etc.)
                   # Run 'hciconfig' to see available adapters
                   # Built-in Pi adapter is usually hci0
                   # USB adapters are usually hci1

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

### Availability

Every entity is marked **unavailable** in Home Assistant when either:
- the monitor itself stops (Pi off, service crashed): the broker publishes the
  `renogy/bridge/availability` last-will message, or
- a device stops answering for 3 polls in a row: `renogy/<device>/availability`
  goes `offline`.

So a dead link shows up as unavailable instead of frozen values. If you have
automations that trigger on a fault or alarm count changing *from* `0`, add
`not_to: [unknown, unavailable]` to those triggers, otherwise going unavailable
looks like a new fault:

```yaml
- trigger: state
  entity_id: sensor.renogy_battery_active_alarms
  from: "0"
  not_to: [unknown, unavailable]
```

### Energy Dashboard

The sensors are configured with appropriate `state_class` for energy tracking:
- `power_generation_today` - Daily solar generation (Wh)
- `power_generation_total` - Lifetime solar generation (Wh)
- `power_consumption_today` - Daily load consumption (Wh)

## Sensor Reference

Sensors marked *diagnostic* appear under **Diagnostic** on the device page.
Static values (model, versions, serial) are read once when the monitor starts.

### Charge Controller (Rover) Sensors

| Sensor | Unit | Register | Description |
|--------|------|----------|-------------|
| battery_percentage | % | 0x0100 | Battery state of charge (voltage based) |
| battery_voltage / battery_current | V / A | 0x0101-0x0102 | Battery voltage and charge current |
| controller_temperature / battery_temperature | °C | 0x0103 | Controller and battery temperature |
| load_voltage / load_current / load_power | V / A / W | 0x0104-0x0106 | DC load output |
| pv_voltage / pv_current / pv_power | V / A / W | 0x0107-0x0109 | Solar panel input |
| battery_min_voltage_today / battery_max_voltage_today | V | 0x010B-0x010C | Today's battery voltage range |
| max_charging_current_today / max_discharging_current_today | A | 0x010D-0x010E | Today's peak currents |
| max_charging_power_today / max_discharging_power_today | W | 0x010F-0x0110 | Today's peak power |
| charging_amp_hours_today / discharging_amp_hours_today | Ah | 0x0111-0x0112 | Today's Ah |
| power_generation_today / power_consumption_today | Wh | 0x0113-0x0114 | Today's energy |
| operating_days | d | 0x0115 | Total operating days |
| over_discharge_count / full_charge_count | - | 0x0116-0x0117 | Lifetime counters (as reported by the controller) |
| charging_amp_hours_total / discharging_amp_hours_total | Ah | 0x0118-0x011B | Lifetime Ah |
| power_generation_total / power_consumption_total | Wh | 0x011C-0x011F | Lifetime energy |
| load_status / charging_status | - | 0x0120 | Load on/off and charging mode |
| fault_count / warning_count | - | 0x0121-0x0122 | Active faults and warnings (details as attributes) |
| nominal_battery_capacity / system_voltage | Ah / V | 0xE002-0xE003 | Configured capacity and system voltage (*diagnostic*) |
| battery_type | - | 0xE004 | Configured battery type |
| over-voltage / charge limit / equalizing / boost (lithium: overcharge) / float (lithium: overcharge recovery) voltages | V | 0xE005-0xE009 | Charge settings (*diagnostic*) |
| boost return / over-discharge recovery / under-voltage warning / over-discharge / discharge limit | V | 0xE00A-0xE00E | Discharge settings (*diagnostic*) |
| firmware_version / hardware_version | - | 0x0014-0x0017 | *diagnostic* |

### Battery Sensors

| Sensor | Unit | Register | Description |
|--------|------|----------|-------------|
| cell_N_voltage | V | 5001-5016 | Cell voltages (one sensor per cell) |
| temperature_N, battery_temperature | °C | 5018-5033 | Cell temperature sensors |
| environment_temperature | °C | 5037 | Ambient temperature sensor |
| heater_temperature | °C | 5040 | Heater temperature (heated models) |
| current / voltage / power | A / V / W | 5042-5043 | Pack current (+charge/-discharge), voltage, power |
| remaining_capacity / total_capacity / soc | Ah / Ah / % | 5044-5047 | Capacity and state of charge |
| cycle_count | cycles | 5048 | Charge cycle count |
| charge / discharge voltage limit | V | 5049-5050 | BMS limits (*diagnostic*) |
| charge / discharge current limit | A | 5051-5052 | BMS limits (*diagnostic*) |
| alarm_count / warning_count | - | 5100-5108 | Active alarms and warnings (details as attributes) |
| heater, charge/discharge MOSFET, fully charged | on/off | 5106-5107 | Binary sensors |
| charging / discharging allowed | on/off | 5109 | Binary sensors |
| serial_number / firmware_version | - | 5110-5117, 5130-5131 | *diagnostic* |
| cell over/high/low/under voltage, charge and discharge high/low temperature | V / °C | 5200-5221 | BMS warning ("high/low") and protection ("over/under") thresholds (*diagnostic*); all 22 thresholds are in the state JSON |

### Inverter Sensors

The RINVTPGH110111S answers only registers 4000-4009 and 4303-4326; the
energy-storage inverter registers (4327+, 4398+, 4441+, 4543+) return
"illegal address".

| Sensor | Unit | Register | Description |
|--------|------|----------|-------------|
| input_voltage / input_current / input_power / input_frequency | V / A / W / Hz | 4000-4001, 4009 | AC input (0 when there is none) |
| output_voltage / output_current / output_power / output_frequency | V / A / W / Hz | 4002-4004 | AC output |
| battery_voltage | V | 4005 | Battery voltage at the inverter |
| temperature | °C | 4006 | Inverter temperature |
| inverter_mode | - | 4102 | normal / eco (sleep) / shutdown |
| fault_count | - | 4007-4008 | Active faults (details as attributes) |
| eco_mode / beeper | on/off | 4007-4008 | Binary sensors |
| firmware_version | - | 4319-4326 | *diagnostic* |

Register maps: [Renogy Modbus documents collected in cyrils/renogy-bt discussion #94](https://github.com/cyrils/renogy-bt/discussions/94)
(Rover Modbus, Lithium Iron Battery BMS Modbus Protocol V1.7, Inverter Modbus Protocol V1.8).

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

### Battery Cell Alarms When Fully Charged

On a sunny day you may see `cell_N_alarm` on every cell from the time the
battery reaches 100% until it starts discharging. The BMS raises it when a cell
passes its *Cell High-Voltage Warning* threshold (3.5 V on the RBT100LFP12SH-G1,
register 5201) and the Rover holds the battery at its boost voltage (for example
14.4 V = 3.6 V per cell). It reflects the charge voltage, not a fault: the
protection trip (*Cell Over-Voltage Protection*, 3.7 V) is not reached. If it bothers you, lower the controller's boost/float
voltage slightly, or ignore battery alarms in your automations while SOC is 100%.

## Known Limitations

- Only one BLE client can connect to a device at a time (close Renogy app first)
- Raspberry Pi built-in Bluetooth may have limited range
- Some older BT-1 modules may have compatibility issues

## Changelog

### 1.3.1 (2026-10-11)
- Fix: stopping the service during a poll hung for about 90 seconds until
  systemd killed it (the poll kept reconnecting after shutdown had closed the
  Bluetooth connections). The poll loop is now cancelled first, then the
  monitor disconnects and marks itself offline.

### 1.3.0 (2026-10-11)
Checked against the Renogy Modbus documents (BMS protocol V1.7, Rover Modbus,
inverter protocol V1.8):
- **Fix: battery warnings were read from the wrong register.** The BMS reports
  warnings (cell/module high and low voltage, charge/discharge temperature) in
  5107 bits 0-7; they were read from 5108, which holds per-cell voltage errors.
  The battery's *Active Warnings* sensor could never show a real warning, and a
  cell error was reported as the wrong warning.
- **Fix: battery "other alarms" (5104-5105)** were decoded from the wrong bit
  positions (BMS board, ambient and heater temperature, charge/discharge current).
- Battery serial number is 16 characters (5110-5117); the rest are version codes.
- New: battery warning and protection thresholds (5200-5221), Rover nominal
  battery capacity and system voltage (0xE002-0xE003), inverter mode (4102).
- Rover boost/float voltage sensors renamed for lithium mode, where these
  registers are the overcharge and overcharge-recovery voltages.

### 1.2.0 (2026-10-11)
- **37 new sensors** from registers the devices already provide (every register
  was checked against the real devices):
  - Rover: today's min/max battery voltage and peak currents, operating days,
    over-discharge and full-charge counts, lifetime charge/discharge Ah and
    lifetime consumption; the boost/float/equalizing and discharge voltage
    settings, firmware and hardware version (diagnostic).
  - Battery: cycle count, ambient and heater temperature, BMS charge/discharge
    voltage and current limits, serial number and firmware (diagnostic), and
    binary sensors for the charge/discharge MOSFETs, charging/discharging
    allowed and fully charged.
  - Inverter: firmware version (diagnostic).
- Most of the Rover values were already in every reply but were not parsed.
- Static registers (model, versions, serial) are read once instead of every poll.
- Removed the "historical" read of register 60000 (0xEA60). It is not the
  7-day history and returned settings.
- Sensor reference in the README rewritten with register numbers. The inverter
  section listed sensors this model does not have.
- Parser tests using register dumps from real devices.


### 1.1.0 (2026-10-11)
- **Fix: garbage readings from late replies.** A reply that arrived after its
  request timed out was parsed as the next register block (for example
  `max_charging_power_today` = 70 published as 17,920 Wh generated today). Replies
  are now matched to the request (slave address, function, byte count, CRC) and
  stray bytes are discarded. This also protects hub mode, where battery and
  inverter share one BT-2.
- **Fix: the data validator could lock up.** A genuine step change larger than
  `max_change` (PV voltage at sunrise) was rejected forever. A new level is now
  accepted after 3 consistent readings in a row.
- **Fix: sensors flashing "unknown".** When one register block failed, all of its
  sensors went unknown until the next poll. The last values are kept instead.
- **New: availability.** Entities go unavailable when the monitor stops (MQTT
  last will) or a device stops answering, instead of showing stale values.
- Faster polling: waits for the complete reply instead of a fixed delay.
- paho-mqtt 2.x callback API (removes the deprecation warning); unused
  `asyncio-mqtt` dependency removed.
- Brings the repository in line with the version running in production
  (Bluetooth adapter reset on startup, battery charge/discharge enable bits fixed).

### 1.0.0 (2025-12-26)
- Initial release.

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

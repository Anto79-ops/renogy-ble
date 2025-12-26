#!/bin/bash
# Renogy BT Monitor Installation Script
# Run as: ./install.sh

set -e

echo "========================================"
echo "Renogy BT Monitor Installation"
echo "========================================"
echo ""

# Check if running as root for system dependencies
if [ "$EUID" -ne 0 ]; then
    echo "Note: Some steps may require sudo access."
    echo ""
fi

# Detect user and home directory
INSTALL_USER="${SUDO_USER:-$USER}"
INSTALL_HOME=$(eval echo ~$INSTALL_USER)
INSTALL_DIR="$INSTALL_HOME/renogy_monitor"

echo "Installing for user: $INSTALL_USER"
echo "Installation directory: $INSTALL_DIR"
echo ""

# Step 1: Install system dependencies
echo "[1/6] Installing system dependencies..."
if command -v apt-get &> /dev/null; then
    sudo apt-get update
    sudo apt-get install -y python3 python3-pip python3-venv \
        bluetooth bluez libglib2.0-dev
else
    echo "Warning: apt-get not found. Please install dependencies manually:"
    echo "  - python3, python3-pip, python3-venv"
    echo "  - bluetooth, bluez, libglib2.0-dev"
fi

# Step 2: Enable Bluetooth service
echo "[2/6] Enabling Bluetooth service..."
sudo systemctl enable bluetooth 2>/dev/null || true
sudo systemctl start bluetooth 2>/dev/null || true

# Step 3: Add user to bluetooth group
echo "[3/6] Adding user to bluetooth group..."
sudo usermod -a -G bluetooth $INSTALL_USER 2>/dev/null || true

# Step 4: Create installation directory
echo "[4/6] Setting up installation directory..."
mkdir -p "$INSTALL_DIR"

# Copy files if not already in the right place
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
if [ "$SCRIPT_DIR" != "$INSTALL_DIR" ]; then
    echo "Copying files to $INSTALL_DIR..."
    cp -f "$SCRIPT_DIR"/*.py "$INSTALL_DIR/" 2>/dev/null || true
    cp -f "$SCRIPT_DIR"/requirements.txt "$INSTALL_DIR/" 2>/dev/null || true
    cp -f "$SCRIPT_DIR"/*.yaml* "$INSTALL_DIR/" 2>/dev/null || true
    cp -f "$SCRIPT_DIR"/README.md "$INSTALL_DIR/" 2>/dev/null || true
fi

# Step 5: Create Python virtual environment
echo "[5/6] Creating Python virtual environment..."
cd "$INSTALL_DIR"

if [ ! -d "venv" ]; then
    python3 -m venv venv
fi

source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# Step 6: Create sample configuration
echo "[6/6] Creating sample configuration..."
if [ ! -f "config.yaml" ]; then
    python main.py --create-config
    echo ""
    echo "Created config.yaml - Please edit with your device settings!"
else
    echo "config.yaml already exists, skipping..."
fi

# Set ownership
sudo chown -R $INSTALL_USER:$INSTALL_USER "$INSTALL_DIR"

echo ""
echo "========================================"
echo "Installation Complete!"
echo "========================================"
echo ""
echo "Next steps:"
echo ""
echo "1. Edit configuration:"
echo "   nano $INSTALL_DIR/config.yaml"
echo ""
echo "2. Scan for devices:"
echo "   cd $INSTALL_DIR"
echo "   source venv/bin/activate"
echo "   python main.py --scan"
echo ""
echo "3. Test the monitor:"
echo "   python main.py config.yaml --debug"
echo ""
echo "4. (Optional) Install as service:"
echo "   sudo cp renogy-monitor.service /etc/systemd/system/"
echo "   sudo systemctl daemon-reload"
echo "   sudo systemctl enable renogy-monitor"
echo "   sudo systemctl start renogy-monitor"
echo ""
echo "For more help, see README.md"
echo ""

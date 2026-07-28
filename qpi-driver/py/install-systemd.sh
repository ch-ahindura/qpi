#!/usr/bin/env bash
set -e

# Interactive systemd installer for qpi-driver
echo "=========================================="
echo "    QPI Driver Systemd Installer          "
echo "=========================================="

if [ "$EUID" -ne 0 ]; then 
  echo "Please run as root (or with sudo) so we can create the systemd service."
  exit 1
fi


# Detect the real user who ran sudo
if [ -n "$SUDO_USER" ]; then
    REAL_USER="$SUDO_USER"
    REAL_HOME=$(getent passwd "$SUDO_USER" | cut -d: -f6)
    if [ -z "$REAL_HOME" ]; then
        REAL_HOME=$(eval echo "~$SUDO_USER")
    fi
else
    REAL_USER=$(whoami)
    REAL_HOME=$HOME
fi

echo "Installing for user: $REAL_USER (Home: $REAL_HOME)"
echo ""

# 1. Prompt for configuration if not provided via environment
while [ -z "$QPI_TOKEN" ]; do read -p "Enter QPI Access Token: " QPI_TOKEN; done
while [ -z "$QPI_ADDR" ]; do read -p "Enter QPI Server Address (e.g. https://qpi.sopherapps.se): " QPI_ADDR; done
while [ -z "$CA_FINGERPRINT" ]; do read -p "Enter CA Fingerprint: " CA_FINGERPRINT; done
# Names the unit file, its journal identifier and its data directory — not the
# driver. A driver's display label is the one an admin typed into the dashboard,
# and the drivers/connect response hands it over.
while [ -z "$SERVICE_NAME" ]; do read -p "Enter a name for this service (e.g. cryostat-1): " SERVICE_NAME; done

# A driver is run by its OPERATION (the --operation flag: process | monitor) on a
# specific DEVICE. A process runs jobs (mock, qiskit_aer, quantify, qblox,
# presto); a monitor reports upward (bluefors_gen1). Both are launched the same
# way: `qpi-driver start --operation <operation> --device <device> … -o key=value`.
[ -z "$OPERATION" ] && read -p "Enter Operation (process, monitor) [process]: " OPERATION
OPERATION=${OPERATION:-process}
[ -z "$DEVICE" ] && read -p "Enter Device (mock, qiskit_aer, quantify, qblox, presto, bluefors_gen1) [mock]: " DEVICE
DEVICE=${DEVICE:-mock}

# A driver's device settings (e.g. bluefors_gen1's base_url/channels, or a process
# device's job_timeout) are passed as generic DRIVER_OPTIONS ("key=value;key=value"),
# rendered as -o flags below. This applies to any operation — a process device reads
# -o keys too, and only the ones this installer manages itself (the data dir and the
# quantify configs) are filled in for it. Leave blank for a device that needs none.
[ -z "$DRIVER_OPTIONS" ] && read -p "Enter $DEVICE options as key=value;key=value (e.g. base_url=http://localhost:49099;channels=mapper.bf.tmc:K), or leave blank: " DRIVER_OPTIONS

# The version of qpi-driver to install.
# This should match the qpi-ui version if provided via environment variable.
QPI_DRIVER_VERSION="${QPI_DRIVER_VERSION:-}"
QPI_DATA_DIR="${QPI_DATA_DIR:-"/var/qpi-driver/${SERVICE_NAME}"}"
QPI_CA_FILE="${QPI_CA_FILE:-"${QPI_DATA_DIR}/qpi.ca.pem"}"
QPI_QUANTIFY_DEVICE_CONFIG="${QPI_QUANTIFY_DEVICE_CONFIG:-"${QPI_DATA_DIR}/quantify.device.yml"}"
QPI_QUANTIFY_HARDWARE_CONFIG="${QPI_QUANTIFY_HARDWARE_CONFIG:-"${QPI_DATA_DIR}/quantify.hardware.json"}"

# Ensure data directory exists and is owned by the real user
echo "Creating data directory at $QPI_DATA_DIR..."
mkdir -p "$QPI_DATA_DIR"
chown -R "$REAL_USER" "$QPI_DATA_DIR"

# 2/3. Install the qpi-driver CLI (unless it is already installed).
# QPI_SKIP_INSTALL=1 skips the install step and uses the qpi-driver already on
# PATH (or QPI_DRIVER_BIN), for operators who manage installs themselves.
if [ "${QPI_SKIP_INSTALL:-0}" = "1" ]; then
    echo "QPI_SKIP_INSTALL=1: skipping install; using an already-installed qpi-driver."
    QPI_DRIVER_BIN="${QPI_DRIVER_BIN:-qpi-driver}"
else
    # Locate or install uv
    if sudo -u "$REAL_USER" command -v uv >/dev/null 2>&1; then
        UV_PATH=$(sudo -u "$REAL_USER" command -v uv)
    elif [ -f "$REAL_HOME/.local/bin/uv" ]; then
        UV_PATH="$REAL_HOME/.local/bin/uv"
    else
        echo "Installing 'uv' for fast python package management..."
        sudo -u "$REAL_USER" bash -c "curl -LsSf https://astral.sh/uv/install.sh | sh"
        UV_PATH="$REAL_HOME/.local/bin/uv"
        [ -f "$REAL_HOME/.local/bin/env" ] && source "$REAL_HOME/.local/bin/env" || true
    fi

    echo "Installing qpi-driver via uv tool..."
    if [ -z "$QPI_DRIVER_VERSION" ]; then
        VERSION_SUFFIX=""
    else
        VERSION_SUFFIX="==$QPI_DRIVER_VERSION"
    fi
    sudo -u "$REAL_USER" "$UV_PATH" tool install --python 3.12 --prerelease allow "qpi-driver[cli,${DEVICE}]${VERSION_SUFFIX}"

    QPI_DRIVER_BIN="${QPI_DRIVER_BIN:-$REAL_HOME/.local/bin/qpi-driver}"
fi


# 4. Create systemd unit file
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.qpi-driver.service"
echo "Creating systemd service at $SERVICE_FILE..."

# Every operation is launched the same way: `qpi-driver <operation> --device
# <device> … -o key=value`. A process auto-fills the runtime options this
# installer manages (data dir, and quantify configs for qblox/quantify); any
# DRIVER_OPTIONS (how a monitor gets its base_url/channels) are appended after.
OPT_ARGS=""
add_opt() {
    [ -n "$1" ] && OPT_ARGS="$OPT_ARGS \\
        -o $1"
}

if [ "$OPERATION" = "process" ]; then
    add_opt "data_dir=$QPI_DATA_DIR"
    if [ "$DEVICE" = "qblox" ] || [ "$DEVICE" = "quantify" ]; then
        add_opt "quantify_device_config=$QPI_QUANTIFY_DEVICE_CONFIG"
        add_opt "quantify_hardware_config=$QPI_QUANTIFY_HARDWARE_CONFIG"
    fi
fi

IFS=';' read -ra _DRIVER_OPTS <<< "$DRIVER_OPTIONS"
for _opt in "${_DRIVER_OPTS[@]}"; do
    add_opt "$_opt"
done

EXEC_START_CMD="$QPI_DRIVER_BIN start \\
        --operation \"$OPERATION\" \\
        --device \"$DEVICE\" \\
        --ca-fingerprint $CA_FINGERPRINT \\
        --qpi-addr $QPI_ADDR$OPT_ARGS"

cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=QPI Driver Service ($SERVICE_NAME)
After=network.target

[Service]
Type=simple

Environment="QPI_ACCESS_TOKEN=$QPI_TOKEN"
Environment="QPI_CA_FILE=$QPI_CA_FILE"
# Standard Python output buffering disabled to ensure logs appear immediately in journalctl
Environment=PYTHONUNBUFFERED=1

ExecStart=$EXEC_START_CMD

Restart=on-failure
User=$REAL_USER

# Journalctl logging configuration
StandardOutput=journal
StandardError=journal
SyslogIdentifier=${SERVICE_NAME}.qpi-driver

[Install]
WantedBy=multi-user.target
EOF

# 5. Enable and start the service
echo "Reloading systemd daemon..."
systemctl daemon-reload

echo "Enabling and starting ${SERVICE_NAME}.qpi-driver.service..."
systemctl enable "${SERVICE_NAME}.qpi-driver.service"
systemctl start "${SERVICE_NAME}.qpi-driver.service"

echo "=========================================="
echo "Installation complete!"
echo "Service status:"
systemctl status "${SERVICE_NAME}.qpi-driver.service" --no-pager || true
echo "=========================================="
echo "To view logs, run: journalctl -u ${SERVICE_NAME}.qpi-driver.service -f"

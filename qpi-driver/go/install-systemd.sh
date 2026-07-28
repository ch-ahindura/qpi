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
while [ -z "$QPU_NAME" ]; do read -p "Enter QPU Name (e.g. rigetti-aspen-1): " QPU_NAME; done

# A driver is run by its OPERATION (the --operation flag: process | monitor) on a
# specific DEVICE. A process runs jobs (mock, qiskit_aer, quantify, qblox,
# presto); a monitor reports upward (bluefors_gen1). Both are launched the same
# way: `qpi-driver start --operation <operation> --device <device> … -o key=value`.
[ -z "$OPERATION" ] && read -p "Enter Operation (process, monitor) [process]: " OPERATION
OPERATION=${OPERATION:-process}
[ -z "$DEVICE" ] && read -p "Enter Device (mock, qiskit_aer, quantify, qblox, presto, bluefors_gen1) [mock]: " DEVICE
DEVICE=${DEVICE:-mock}

# A driver's device settings (e.g. bluefors_gen1's base_url/channels) are passed
# as generic DRIVER_OPTIONS ("key=value;key=value"), rendered as -o flags below.
# This applies to any operation; leave blank for a device that needs none.
[ -z "$DRIVER_OPTIONS" ] && read -p "Enter $DEVICE options as key=value;key=value (e.g. base_url=http://localhost:49099;channels=mapper.bf.tmc:K), or leave blank: " DRIVER_OPTIONS

# The version of qpi-driver to install.
# This should match the qpi-ui version if provided via environment variable.
QPI_DRIVER_VERSION="${QPI_DRIVER_VERSION:-}"
QPI_DATA_DIR="${QPI_DATA_DIR:-"/var/qpi-driver/${QPU_NAME}"}"
QPI_CA_FILE="${QPI_CA_FILE:-"${QPI_DATA_DIR}/qpi.ca.pem"}"

# Ensure data directory exists and is owned by the real user
echo "Creating data directory at $QPI_DATA_DIR..."
mkdir -p "$QPI_DATA_DIR"
chown -R "$REAL_USER" "$QPI_DATA_DIR"

# 2/3. Install the qpi-driver CLI with `go install` (unless it is already
# installed). QPI_SKIP_INSTALL=1 skips the install step and uses the qpi-driver
# already on PATH (or QPI_DRIVER_BIN), for operators who manage installs themselves.
if [ "${QPI_SKIP_INSTALL:-0}" = "1" ]; then
    echo "QPI_SKIP_INSTALL=1: skipping install; using an already-installed qpi-driver."
    QPI_DRIVER_BIN="${QPI_DRIVER_BIN:-qpi-driver}"
else
    # Locate the Go toolchain (required to `go install` the driver CLI)
    if sudo -u "$REAL_USER" command -v go >/dev/null 2>&1; then
        GO_BIN=$(sudo -u "$REAL_USER" command -v go)
    elif [ -x "/usr/local/go/bin/go" ]; then
        GO_BIN="/usr/local/go/bin/go"
    else
        echo "Error: the Go toolchain ('go') is required but was not found for $REAL_USER."
        echo "Install Go (https://go.dev/dl/) and re-run this script."
        exit 1
    fi

    # Where 'go install' drops binaries (GOBIN, else GOPATH/bin).
    GO_INSTALL_DIR=$(sudo -u "$REAL_USER" "$GO_BIN" env GOBIN)
    [ -z "$GO_INSTALL_DIR" ] && GO_INSTALL_DIR="$(sudo -u "$REAL_USER" "$GO_BIN" env GOPATH)/bin"

    if [ -z "$QPI_DRIVER_VERSION" ]; then
        VERSION_SUFFIX="@latest"
    else
        VERSION_SUFFIX="@v${QPI_DRIVER_VERSION#v}"
    fi
    echo "Installing qpi-driver via go install (${VERSION_SUFFIX})..."
    sudo -u "$REAL_USER" "$GO_BIN" install "github.com/sopherapps/qpi/qpi-driver/go/qpi-driver${VERSION_SUFFIX}"

    QPI_DRIVER_BIN="${QPI_DRIVER_BIN:-${GO_INSTALL_DIR}/qpi-driver}"
fi


# 4. Create systemd unit file
SERVICE_FILE="/etc/systemd/system/${QPU_NAME}.qpi-driver.service"
echo "Creating systemd service at $SERVICE_FILE..."

# Every operation is launched the same way: `qpi-driver <operation> --device
# <device> … -o key=value`. The device's DRIVER_OPTIONS (e.g. a monitor's
# base_url/channels) are rendered as trailing -o flags.
OPT_ARGS=""
add_opt() {
    [ -n "$1" ] && OPT_ARGS="$OPT_ARGS \\
        -o $1"
}

IFS=';' read -ra _DRIVER_OPTS <<< "$DRIVER_OPTIONS"
for _opt in "${_DRIVER_OPTS[@]}"; do
    add_opt "$_opt"
done

EXEC_START_CMD="$QPI_DRIVER_BIN start \\
        --operation \"$OPERATION\" \\
        --device \"$DEVICE\" \\
        --ca-fingerprint $CA_FINGERPRINT \\
        --qpi-addr $QPI_ADDR \\
        --name \"$QPU_NAME\"$OPT_ARGS"

cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=QPI Driver Service ($QPU_NAME)
After=network.target

[Service]
Type=simple

Environment="QPI_ACCESS_TOKEN=$QPI_TOKEN"
Environment="QPI_CA_FILE=$QPI_CA_FILE"

ExecStart=$EXEC_START_CMD

Restart=on-failure
User=$REAL_USER

# Journalctl logging configuration
StandardOutput=journal
StandardError=journal
SyslogIdentifier=${QPU_NAME}.qpi-driver

[Install]
WantedBy=multi-user.target
EOF

# 5. Enable and start the service
echo "Reloading systemd daemon..."
systemctl daemon-reload

echo "Enabling and starting ${QPU_NAME}.qpi-driver.service..."
systemctl enable "${QPU_NAME}.qpi-driver.service"
systemctl start "${QPU_NAME}.qpi-driver.service"

echo "=========================================="
echo "Installation complete!"
echo "Service status:"
systemctl status "${QPU_NAME}.qpi-driver.service" --no-pager || true
echo "=========================================="
echo "To view logs, run: journalctl -u ${QPU_NAME}.qpi-driver.service -f"

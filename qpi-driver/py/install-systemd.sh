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

# 1. Configuration comes from the environment; a prompt fills in the rest.
#
# Prompting needs a terminal on stdin, and one of the two documented invocations
# has none: `curl … | sudo bash` puts the script itself on stdin, so a `read` there
# reaches EOF at once and returns non-zero — which under `set -e` ends the install
# with nothing printed and no service written. So nothing is prompted for unless
# there is a terminal to answer it. Piped, a value with a default takes its
# default, and one without names the variable to set instead of exiting silently.
#
# The interactive form, `sudo bash -c "$(curl …)"`, keeps the terminal on stdin,
# because the script arrives as an argument rather than on the pipe.
require() { # require VAR PROMPT — for a value the installer cannot invent
    local name="$1" prompt="$2"
    while [ -z "${!name}" ]; do
        if [ ! -t 0 ]; then
            echo "Error: $name is not set, and there is no terminal to ask on." >&2
            echo "Set it in the environment ($name=…), or install interactively:" >&2
            echo "  sudo bash -c \"\$(curl -LsSf <installer-url>)\"" >&2
            exit 1
        fi
        read -rp "$prompt" "$name"
    done
}

ask() { # ask VAR PROMPT — for a value whose default the caller applies below
    local name="$1" prompt="$2"
    if [ -z "${!name}" ] && [ -t 0 ]; then
        read -rp "$prompt" "$name"
    fi
}

require QPI_TOKEN "Enter QPI Access Token: "
require QPI_ADDR "Enter QPI Server Address (e.g. https://qpi.sopherapps.se): "
require CA_FINGERPRINT "Enter CA Fingerprint: "
# Names the unit file, its journal identifier and its data directory — not the
# driver. A driver's display label is the one an admin typed into the dashboard,
# and the drivers/connect response hands it over.
require SERVICE_NAME "Enter a name for this service (e.g. cryostat-1): "

# A driver is run by its OPERATION (the --operation flag: process | monitor |
# calibrate) on a specific DEVICE. A process runs jobs (mock, qiskit_aer, quantify,
# qblox, presto); a monitor reports upward (bluefors_gen1); a calibrate tunes the
# chip (quantify_tuner, qblox_tuner). All three are launched the same way:
# `qpi-driver start --operation <operation> --device <device> … -o key=value`.
ask OPERATION "Enter Operation (process, monitor, calibrate) [process]: "
OPERATION=${OPERATION:-process}
ask DEVICE "Enter Device (mock, qiskit_aer, quantify, qblox, presto, bluefors_gen1, quantify_tuner, qblox_tuner) [mock]: "
DEVICE=${DEVICE:-mock}

# A driver's device settings (e.g. bluefors_gen1's base_url/channels, or a process
# device's job_timeout) are passed as generic DRIVER_OPTIONS ("key=value;key=value"),
# rendered as -o flags below. This applies to any operation — a process device reads
# -o keys too, and only the ones this installer manages itself (the config files
# under the data directory) are filled in for it. Leave blank for a device that
# needs none: a QPU and a tuner are both installable without setting this at all.
ask DRIVER_OPTIONS "Enter $DEVICE options as key=value;key=value (e.g. base_url=http://localhost:49099;channels=mapper.bf.tmc:K), or leave blank: "

# The version of qpi-driver to install.
# This should match the qpi-ui version if provided via environment variable.
QPI_DRIVER_VERSION="${QPI_DRIVER_VERSION:-}"
QPI_DATA_DIR="${QPI_DATA_DIR:-"/var/qpi-driver/${SERVICE_NAME}"}"
QPI_CA_FILE="${QPI_CA_FILE:-"${QPI_DATA_DIR}/qpi.ca.pem"}"
QPI_QUANTIFY_DEVICE_CONFIG="${QPI_QUANTIFY_DEVICE_CONFIG:-"${QPI_DATA_DIR}/quantify.device.yml"}"
QPI_QUANTIFY_HARDWARE_CONFIG="${QPI_QUANTIFY_HARDWARE_CONFIG:-"${QPI_DATA_DIR}/quantify.hardware.json"}"
QPI_CALIBRATION_CONFIG="${QPI_CALIBRATION_CONFIG:-"${QPI_DATA_DIR}/calibration.yml"}"

# Ensure data directory exists and is owned by the real user. Every path above is
# under it, so an operator has one directory to fill rather than -o flags to get right.
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
# <device> … -o key=value`. The config paths this installer manages are filled in
# for the devices that read them; any DRIVER_OPTIONS (how a monitor gets its
# base_url/channels) are appended after. The data directory is not among them —
# it reaches the driver as QPI_DATA_DIR in the unit's environment below.
OPT_ARGS=""
MANAGED_CONFIGS=()
add_opt() {
    # `return 0` rather than `[ -n "$1" ] && …`, whose false test would make the
    # function itself return non-zero and take `set -e` with it: a DRIVER_OPTIONS
    # of ";base_url=…" splits into an empty first field, and a stray semicolon
    # should not end the install without a word.
    [ -n "$1" ] || return 0
    OPT_ARGS="$OPT_ARGS \\
        -o $1"
}

add_config_opt() { # add_config_opt KEY PATH — an -o the operator has to supply a file for
    add_opt "$1=$2"
    MANAGED_CONFIGS+=("$2")
}

# A tuner and the QPU beside it read the same two quantify files — that is how the
# calibration a tuner writes reaches the jobs a QPU runs — so one case covers both
# operations. Only a tuner takes a calibration config.
case "$DEVICE" in
    quantify | qblox | quantify_tuner | qblox_tuner)
        add_config_opt quantify_device_config "$QPI_QUANTIFY_DEVICE_CONFIG"
        add_config_opt quantify_hardware_config "$QPI_QUANTIFY_HARDWARE_CONFIG"
        ;;
esac

if [ "$OPERATION" = "calibrate" ]; then
    add_config_opt calibration_config "$QPI_CALIBRATION_CONFIG"
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
Environment="QPI_DATA_DIR=$QPI_DATA_DIR"
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

# A config file nobody has put there yet is the one failure this script can see
# coming, and it is silent otherwise: the service starts and fails in its worker.
for _config in "${MANAGED_CONFIGS[@]}"; do
    if [ ! -f "$_config" ]; then
        echo "Warning: $_config does not exist yet. Put it there, then run:"
        echo "  sudo systemctl restart ${SERVICE_NAME}.qpi-driver.service"
    fi
done

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

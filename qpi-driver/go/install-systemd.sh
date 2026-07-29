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

# A driver is run by its OPERATION (the --operation flag) on a specific DEVICE,
# and both are launched the same way: `qpi-driver start --operation <operation>
# --device <device> … -o key=value`.
#
# These are the operations and devices *this* SDK ships, and they must stay in
# step with what it registers (`qpi-driver devices`). The Go SDK ships one monitor
# device and no process device, so offering `process` here would write a unit whose
# ExecStart can never succeed — and with Restart=on-failure below, crash-loop.
# Running a QPU means the Python SDK, or a device of your own.
ask OPERATION "Enter Operation (monitor) [monitor]: "
OPERATION=${OPERATION:-monitor}
ask DEVICE "Enter Device (bluefors_gen1) [bluefors_gen1]: "
DEVICE=${DEVICE:-bluefors_gen1}

# A driver's device settings (e.g. bluefors_gen1's base_url/channels) are passed
# as generic DRIVER_OPTIONS ("key=value;key=value"), rendered as -o flags below.
# This applies to any operation; leave blank for a device that needs none.
ask DRIVER_OPTIONS "Enter $DEVICE options as key=value;key=value (e.g. base_url=http://localhost:49099;channels=mapper.bf.tmc:K), or leave blank: "

# The version of qpi-driver to install.
# This should match the qpi-ui version if provided via environment variable.
QPI_DRIVER_VERSION="${QPI_DRIVER_VERSION:-}"
QPI_DATA_DIR="${QPI_DATA_DIR:-"/var/qpi-driver/${SERVICE_NAME}"}"
QPI_CA_FILE="${QPI_CA_FILE:-"${QPI_DATA_DIR}/qpi.ca.pem"}"

# The driver writes the root CA it downloaded to QPI_CA_FILE, so its directory has
# to exist and be writable by the user the unit runs as. Nothing else in this SDK
# writes to disk — the `data_dir` option is a process-device thing, and this SDK
# ships none.
echo "Creating $QPI_DATA_DIR for the downloaded root CA..."
mkdir -p "$QPI_DATA_DIR"
chown -R "$REAL_USER" "$QPI_DATA_DIR"

# 2/3. Install the qpi-driver CLI with `go install` (unless it is already
# installed). QPI_SKIP_INSTALL=1 skips the install step and uses the qpi-driver
# already on PATH (or QPI_DRIVER_BIN), for operators who manage installs themselves.
if [ "${QPI_SKIP_INSTALL:-0}" = "1" ]; then
    echo "QPI_SKIP_INSTALL=1: skipping install; using an already-installed qpi-driver."
    QPI_DRIVER_BIN="${QPI_DRIVER_BIN:-qpi-driver}"
else
    # Locate or install the Go toolchain, which `go install` below needs. An
    # installer that stops halfway to tell the operator to go and install a runtime
    # is not an installer, so bring our own — the official tarball into
    # /usr/local/go, which is where go.dev/doc/install puts it and where the `elif`
    # above already looks.
    if sudo -u "$REAL_USER" command -v go >/dev/null 2>&1; then
        GO_BIN=$(sudo -u "$REAL_USER" command -v go)
    elif [ -x "/usr/local/go/bin/go" ]; then
        GO_BIN="/usr/local/go/bin/go"
    else
        GO_VERSION="${GO_VERSION:-1.25.5}"
        case "$(uname -m)" in
            x86_64 | amd64) GO_ARCH="amd64" ;;
            aarch64 | arm64) GO_ARCH="arm64" ;;
            *)
                echo "Error: no Go toolchain, and no prebuilt one for $(uname -m)."
                echo "Install Go yourself (https://go.dev/dl/) and re-run this script."
                exit 1
                ;;
        esac
        GO_TARBALL="go${GO_VERSION}.$(uname -s | tr '[:upper:]' '[:lower:]')-${GO_ARCH}.tar.gz"

        echo "The Go toolchain was not found; installing go${GO_VERSION} into /usr/local/go..."
        # Unpacked as root because /usr/local/go is root-owned and shared: unlike the
        # Node install, which nvm deliberately keeps per-user, a Go toolchain here is
        # the machine's. `rm -rf` first is what go.dev/doc/install prescribes — an
        # unpack over an existing tree leaves a mix of two versions.
        curl -fsSLo "/tmp/${GO_TARBALL}" "https://go.dev/dl/${GO_TARBALL}"
        rm -rf /usr/local/go
        tar -C /usr/local -xzf "/tmp/${GO_TARBALL}"
        rm -f "/tmp/${GO_TARBALL}"

        GO_BIN="/usr/local/go/bin/go"
        if [ ! -x "$GO_BIN" ]; then
            echo "Error: unpacking ${GO_TARBALL} did not produce /usr/local/go/bin/go."
            echo "Install Go yourself (https://go.dev/dl/) and re-run with QPI_SKIP_INSTALL=1."
            exit 1
        fi
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
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.qpi-driver.service"
echo "Creating systemd service at $SERVICE_FILE..."

# Every operation is launched the same way: `qpi-driver <operation> --device
# <device> … -o key=value`. The device's DRIVER_OPTIONS (e.g. a monitor's
# base_url/channels) are rendered as trailing -o flags.
OPT_ARGS=""
add_opt() {
    # `return 0` rather than `[ -n "$1" ] && …`, whose false test would make the
    # function itself return non-zero and take `set -e` with it: a DRIVER_OPTIONS
    # of ";base_url=…" splits into an empty first field, and a stray semicolon
    # should not end the install without a word.
    [ -n "$1" ] || return 0
    OPT_ARGS="$OPT_ARGS \\
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
        --qpi-addr $QPI_ADDR$OPT_ARGS"

cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=QPI Driver Service ($SERVICE_NAME)
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

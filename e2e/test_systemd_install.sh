#!/usr/bin/env bash
set -e

echo "Starting systemd container..."
CONTAINER_ID=$(docker run -d --platform linux/amd64 --privileged --cgroupns=host --tmpfs /tmp --tmpfs /run -v /sys/fs/cgroup:/sys/fs/cgroup:rw jrei/systemd-ubuntu:22.04)

# Ensure container is destroyed on exit
trap "echo 'Cleaning up...'; docker stop \$CONTAINER_ID >/dev/null; docker rm \$CONTAINER_ID >/dev/null" EXIT

# Give systemd a moment to initialize
sleep 3

# Resolve absolute path to project root
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

echo "Container started: $CONTAINER_ID"
echo "Copying installer to container..."
# Copy from project root to ensure robustness regardless of execution directory
docker cp "$PROJECT_ROOT/qpi-driver/py/install-systemd.sh" $CONTAINER_ID:/install-systemd.sh
docker exec $CONTAINER_ID chmod +x /install-systemd.sh

# Need to install curl and sudo as they are missing in basic ubuntu image
docker exec $CONTAINER_ID apt-get update
docker exec $CONTAINER_ID apt-get install -y curl sudo

echo "Running installer..."
docker exec -e QPI_TOKEN="mock_token" \
            -e QPI_ADDR="http://mock" \
            -e CA_FINGERPRINT="mock_fingerprint" \
            -e SERVICE_NAME="mock_qpu" \
            -e OPERATION="process" \
            -e DEVICE="mock" \
            $CONTAINER_ID bash -c "/install-systemd.sh || true"

echo "Checking if service file was generated..."
if docker exec $CONTAINER_ID cat /etc/systemd/system/mock_qpu.qpi-driver.service | grep -q "QPI Driver Service"; then
    echo "SUCCESS: systemd service file generated correctly!"
else
    echo "FAILED: systemd service file not found or incorrect."
    exit 1
fi

echo "Checking if service can start..."
docker exec $CONTAINER_ID systemctl daemon-reload
docker exec $CONTAINER_ID systemctl start mock_qpu.qpi-driver

# We give it a moment to fail if it's going to
sleep 2

if docker exec $CONTAINER_ID systemctl status mock_qpu.qpi-driver | grep -q "active (running)"; then
    echo "SUCCESS: systemd service started successfully!"
else
    echo "FAILED: systemd service failed to start."
    docker exec $CONTAINER_ID systemctl status mock_qpu.qpi-driver || true
    docker exec $CONTAINER_ID journalctl -u mock_qpu.qpi-driver --no-pager || true
    exit 1
fi

# ---------------------------------------------------------------------------
# The rest of the installers, against a stub CLI.
#
# The Go and TypeScript CLIs are fetched from artifacts that only exist once
# published (`go install …@latest`, `npm install -g qpi-driver`), so a real
# install can't run here yet. Instead we exercise each script's config handling
# and unit-file/service generation with QPI_SKIP_INSTALL=1 and a stub
# `qpi-driver` on PATH that just stays running. The Python tuner goes through the
# same path for a different reason: `qpi-driver[cli,quantify_tuner]` pulls a
# scheduler and a stack of scientific packages in, and what is under test is the
# unit file, not pip.
# ---------------------------------------------------------------------------
echo "Installing a stub qpi-driver CLI in the container..."
docker exec $CONTAINER_ID bash -c 'printf "#!/bin/sh\nexec sleep infinity\n" > /usr/local/bin/qpi-driver && chmod +x /usr/local/bin/qpi-driver'

check_installer() {
    # check_installer LABEL SCRIPT SERVICE OPERATION DEVICE DRIVER_OPTIONS EXPECTED…
    local label="$1" qpu="$3" operation="$4" device="$5"
    local script="$2" driver_options="$6"
    shift 6
    echo "Running $label install-systemd.sh (QPI_SKIP_INSTALL=1)..."
    docker cp "$script" $CONTAINER_ID:/install-systemd.sh
    docker exec $CONTAINER_ID chmod +x /install-systemd.sh
    docker exec -e QPI_SKIP_INSTALL=1 \
                -e QPI_DRIVER_BIN=/usr/local/bin/qpi-driver \
                -e QPI_TOKEN="mock_token" \
                -e QPI_ADDR="http://mock" \
                -e CA_FINGERPRINT="mock_fingerprint" \
                -e SERVICE_NAME="$qpu" \
                -e OPERATION="$operation" \
                -e DEVICE="$device" \
                -e DRIVER_OPTIONS="$driver_options" \
                $CONTAINER_ID bash -c "/install-systemd.sh"

    local unit="/etc/systemd/system/$qpu.qpi-driver.service"
    if ! docker exec $CONTAINER_ID cat "$unit" | grep -q "QPI Driver Service"; then
        echo "FAILED: $label systemd service file not found or incorrect."
        exit 1
    fi
    # Each remaining argument is a line the unit file has to contain: what the
    # installer is expected to have worked out on the operator's behalf.
    for expected in "$@"; do
        if ! docker exec $CONTAINER_ID grep -qF "$expected" "$unit"; then
            echo "FAILED: $label unit file does not mention '$expected'."
            docker exec $CONTAINER_ID cat "$unit" || true
            exit 1
        fi
    done
    echo "SUCCESS: $label systemd service file generated!"

    docker exec $CONTAINER_ID systemctl daemon-reload
    docker exec $CONTAINER_ID systemctl start "$qpu.qpi-driver"
    sleep 2
    if docker exec $CONTAINER_ID systemctl status "$qpu.qpi-driver" | grep -q "active (running)"; then
        echo "SUCCESS: $label systemd service started successfully!"
    else
        echo "FAILED: $label systemd service failed to start."
        docker exec $CONTAINER_ID systemctl status "$qpu.qpi-driver" || true
        docker exec $CONTAINER_ID journalctl -u "$qpu.qpi-driver" --no-pager || true
        exit 1
    fi
}

check_installer "Go" "$PROJECT_ROOT/qpi-driver/go/install-systemd.sh" "go_cryostat" "monitor" "bluefors_gen1" \
    "base_url=http://mock;channels=mapper.bf.tmc:K"
check_installer "TypeScript" "$PROJECT_ROOT/qpi-driver/js/install-systemd.sh" "js_cryostat" "monitor" "bluefors_gen1" \
    "base_url=http://mock;channels=mapper.bf.tmc:K"

# A tuner installs with no DRIVER_OPTIONS at all: the three files it cannot start
# without, and the directory it writes under, are the installer's to fill in. Their
# defaults in the driver are relative paths, which a service resolves against `/`.
check_installer "Python tuner" "$PROJECT_ROOT/qpi-driver/py/install-systemd.sh" "py_tuner" "calibrate" "quantify_tuner" \
    "" \
    'Environment="QPI_DATA_DIR=/var/qpi-driver/py_tuner"' \
    "-o quantify_device_config=/var/qpi-driver/py_tuner/quantify.device.yml" \
    "-o quantify_hardware_config=/var/qpi-driver/py_tuner/quantify.hardware.json" \
    "-o calibration_config=/var/qpi-driver/py_tuner/calibration.yml"

echo "All install-systemd.sh variants (py, go, js) verified."

#!/bin/bash
# test_dashboard_cypress.sh — E2E tests for the React dashboard using Cypress.
#
# Usage:
#   ./test_dashboard_cypress.sh

set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
source "${DIR}/lib.sh"

echo ""
echo "========================================================================"
echo "[e2e] Running Cypress E2E Dashboard tests"
echo "========================================================================"
echo ""

# Ensure dashboard dependencies are installed, including Cypress, and compiled
echo "[e2e] Preparing React dashboard..."
(cd "${PROJECT_ROOT}/qpi-ui/internal/dashboard" && npm ci)

echo "[e2e] Compiling static assets..."
(cd "${PROJECT_ROOT}/qpi-ui/internal/dashboard" && npm run build)

build_server
install_driver

start_pocketbase
seed_database
start_driver "mock"

# Allow driver time to register and start polling
sleep 2

status=0
echo "[e2e] Executing Cypress spec tests..."
operator="run"
if [ -n "$IS_VISUAL" ]; then
    operator="open"
fi
(cd "${PROJECT_ROOT}/qpi-ui/internal/dashboard" && npx cypress $operator "$@") || status=$?

stop_driver
stop_pocketbase

echo "=============================="
echo "DUMPING THEMES DATABASE:"
sqlite3 "${PROJECT_ROOT}/bin/pb_data/data.db" "SELECT id, name, is_active, length(custom_css) FROM themes;"
echo "=============================="
if [ "$status" -ne 0 ]; then
    echo "[e2e] Cypress E2E Dashboard FAILED"
    exit 1
fi

echo "DUMPING THEMES DATABASE:"
sqlite3 "${PROJECT_ROOT}/bin/pb_data/data.db" "SELECT id, name, is_active, length(custom_css) FROM themes;"
echo "=============================="

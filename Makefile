.PHONY: sync-driver-catalog all build build-dashboard test test-js-driver test-go-driver lint lint-go lint-py lint-js lint-dashboard lint-go-client lint-py-client lint-js-driver lint-go-driver format format-go format-py format-js format-dashboard format-go-client format-py-client format-js-driver format-go-driver package package-driver package-driver-js package-driver-go package-js package-py package-go publish-js publish-driver-js publish-py clean venv-check test-e2e-dashboard test-e2e-driver-framework

VERSION ?= 0.1.2
UV := $(shell command -v uv 2> /dev/null || echo "$$HOME/.local/bin/uv")

all: build

# Automatically create the virtual environment if not already in one and uv is missing.
venv-check:
	@if ! command -v uv >/dev/null 2>&1 && [ ! -f "$$HOME/.local/bin/uv" ]; then \
		echo "uv not found, installing..."; \
		curl -LsSf https://astral.sh/uv/install.sh | sh; \
	fi

build: venv-check build-dashboard
	@echo "Building Go server..."
	mkdir -p bin
	(cd qpi-ui && go build -ldflags="-s -w" -o ../bin/qpi .)
	@echo "Installing python driver package..."
	$(UV) sync --project qpi-driver/py --extra cli --extra aer --extra quantify --dev
	@if [ "$$(uname)" = "Darwin" ]; then \
		echo "Fixing macOS codesign for q1asm_macos..."; \
		codesign --force --deep --sign - qpi-driver/py/.venv/lib/python3.12/site-packages/qblox_instruments/assemblers/q1asm_macos 2>/dev/null || true; \
	fi
	@echo "Building JS client..."
	(cd qpi-client/js && npm ci && npm run build)

build-dashboard:
	@echo "Building React dashboard..."
	(cd qpi-ui/internal/dashboard && CYPRESS_INSTALL_BINARY=0 npm ci && npm run build)

serve-docs:
	@echo "Building documentation..."
	@if [ ! -d ".venv" ]; then \
		echo "Creating virtual environment for docs..."; \
		$(UV) venv; \
	fi
	$(UV) pip install mike mkdocs-material mkdocstrings[python]
	$(UV) run mike serve

# ---------------------------------------------------------------------------
# Test targets
# ---------------------------------------------------------------------------

test: test-go test-py test-js-client test-go-client test-py-client test-js-driver test-go-driver test-e2e

test-go: build-dashboard
	@echo "Running Go unit tests (server)..."
	(cd qpi-ui && go test -race -v ./...)

test-go-minimal:
	@echo "Running Go server unit tests..."
	(cd qpi-ui && go test -race -cover ./...)

test-py: test-py-base test-py-cli test-py-aer test-py-quantify test-py-qblox

test-py-base:
	@echo "Running Python driver tests with base deps only (mock executor)..."
	$(UV) sync --project qpi-driver/py --dev
	$(UV) run --project qpi-driver/py pytest qpi-driver/py/tests/ -v

test-py-cli:
	@echo "Running Python driver tests with [cli] extra..."
	$(UV) sync --project qpi-driver/py --extra cli --dev
	$(UV) run --project qpi-driver/py pytest qpi-driver/py/tests/ -v

test-py-aer:
	@echo "Running Python driver tests with [aer] extra..."
	$(UV) sync --project qpi-driver/py --extra aer --dev
	$(UV) run --project qpi-driver/py pytest qpi-driver/py/tests/ -v

test-py-quantify:
	@echo "Running Python driver tests with [quantify] extra..."
	$(UV) sync --project qpi-driver/py --extra quantify --dev
	@if [ "$$(uname)" = "Darwin" ]; then \
		echo "Fixing macOS codesign for q1asm_macos..."; \
		codesign --force --deep --sign - qpi-driver/py/.venv/lib/python3.12/site-packages/qblox_instruments/assemblers/q1asm_macos 2>/dev/null || true; \
	fi
	$(UV) run --project qpi-driver/py pytest qpi-driver/py/tests/ -v

test-py-qblox:
	@echo "Running Python driver tests with [qblox] extra..."
	$(UV) sync --project qpi-driver/py --extra qblox --dev
	@if [ "$$(uname)" = "Darwin" ]; then \
		echo "Fixing macOS codesign for q1asm_macos..."; \
		codesign --force --deep --sign - qpi-driver/py/.venv/lib/python3.12/site-packages/qblox_instruments/assemblers/q1asm_macos 2>/dev/null || true; \
	fi
	$(UV) run --project qpi-driver/py pytest qpi-driver/py/tests/ -v

test-js-client:
	@echo "Running JS client tests..."
	(cd qpi-client/js && npm ci && npm test)

test-go-client:
	@echo "Running Go client tests..."
	(cd qpi-client/go && go test -race -v ./...)

test-go-client-minimal:
	@echo "Running Go client tests..."
	(cd qpi-client/go && go test -race -cover ./...)

test-py-client:
	@echo "Running Python client tests..."
	$(UV) sync --project qpi-client/py --dev
	$(UV) run --project qpi-client/py pytest qpi-client/py/tests/ -v

test-js-driver:
	@echo "Running JS/TS driver SDK tests..."
	(cd qpi-driver/js && npm ci && npm test)

test-go-driver:
	@echo "Running Go driver SDK tests..."
	(cd qpi-driver/go && go test -race -v ./...)

test-go-driver-minimal:
	@echo "Running Go driver SDK tests..."
	(cd qpi-driver/go && go test -race -cover ./...)

test-e2e: test-e2e-driver test-e2e-client-py test-e2e-client-js test-e2e-client-go test-e2e-dashboard test-e2e-systemd

test-e2e-driver:
	@echo "Running E2E driver tests..."
	./e2e/test_driver.sh $(EXECUTOR)

test-e2e-client-py:
	@echo "Running E2E Python client tests..."
	./e2e/test_client_py.sh

test-e2e-client-js:
	@echo "Running E2E JavaScript client tests..."
	./e2e/test_client_js.sh

test-e2e-client-go:
	@echo "Running E2E Go client tests..."
	./e2e/test_client_go.sh

test-e2e-dashboard:
	@echo "Running E2E Cypress dashboard tests..."
	./e2e/test_dashboard_cypress.sh

test-e2e-dashboard-visual:
	@echo "Running E2E Cypress dashboard tests..."
	IS_VISUAL=1 ./e2e/test_dashboard_cypress.sh

test-e2e-systemd:
	@echo "Running E2E systemd installer tests..."
	./e2e/test_systemd_install.sh

# ---------------------------------------------------------------------------
# Lint targets
# ---------------------------------------------------------------------------

lint: lint-go lint-py lint-js lint-dashboard lint-go-client lint-py-client lint-js-driver lint-go-driver

lint-go: build-dashboard
	@echo "Linting Go server files..."
	(cd qpi-ui && go vet ./...)
	(cd qpi-ui && gofmt -l -d .)

lint-py:
	@echo "Linting Python driver files..."
	$(UV) run --project qpi-driver/py ruff check qpi-driver/py/

lint-js:
	@echo "Linting JS client files..."
	(cd qpi-client/js && npm ci && npm run lint)

lint-dashboard:
	@echo "Linting dashboard files..."
	(cd qpi-ui/internal/dashboard && CYPRESS_INSTALL_BINARY=0 npm ci && npm run lint)

lint-go-client:
	@echo "Linting Go client files..."
	(cd qpi-client/go && go vet ./...)
	(cd qpi-client/go && gofmt -l -d .)

lint-py-client:
	@echo "Linting Python client files..."
	$(UV) run --project qpi-client/py ruff check qpi-client/py/qpi_client/ qpi-client/py/tests/

lint-js-driver:
	@echo "Type-checking JS/TS driver SDK..."
	(cd qpi-driver/js && npm ci && npm run lint)

lint-go-driver:
	@echo "Linting Go driver SDK files..."
	(cd qpi-driver/go && go vet ./...)
	(cd qpi-driver/go && gofmt -l -d .)

# ---------------------------------------------------------------------------
# Format targets
# ---------------------------------------------------------------------------

format: format-go format-py format-js format-dashboard format-go-client format-py-client format-js-driver format-go-driver

format-go:
	@echo "Formatting Go server files..."
	(cd qpi-ui && go fmt ./...)

# Regenerate the driver catalog fixture the qpi-ui drift check reads. Devices and
# their options belong to the driver SDK (RFC 0003 §9), so this is one-way: the SDK
# writes, qpi-ui checks. Running it is the deliberate act of recording a catalog
# change — the drift test names this target in every failure.
sync-driver-catalog:
	@echo "Regenerating qpi-ui/internal/drivers/testdata/catalog.json from the Python SDK..."
	$(UV) sync --project qpi-driver/py --extra cli
	$(UV) run --project qpi-driver/py python -m qpi_driver.cli catalog --json \
		> qpi-ui/internal/drivers/testdata/catalog.json
	@echo "Regenerating the -o option tables in the driver READMEs..."
	python3 qpi-driver/render_catalog_table.py qpi-driver/py/README.md \
		< qpi-ui/internal/drivers/testdata/catalog.json
	@echo "Done. Review the diff, then make catalog.go agree with it."

format-py:
	@echo "Formatting and sorting imports for Python driver files..."
	$(UV) run --project qpi-driver/py ruff format qpi-driver/py/
	$(UV) run --project qpi-driver/py ruff check --select I --fix qpi-driver/py/

format-js:
	@echo "Formatting JS client files..."
	(cd qpi-client/js && npm ci && npm run format)

format-dashboard:
	@echo "Formatting dashboard files..."
	(cd qpi-ui/internal/dashboard && CYPRESS_INSTALL_BINARY=0 npm ci && npm run format)

format-go-client:
	@echo "Formatting Go client files..."
	(cd qpi-client/go && go fmt ./...)

format-py-client:
	@echo "Formatting and sorting imports for Python client files..."
	$(UV) run --project qpi-client/py ruff format qpi-client/py/qpi_client/ qpi-client/py/tests/
	$(UV) run --project qpi-client/py ruff check --select I --fix qpi-client/py/qpi_client/ qpi-client/py/tests/

format-js-driver:
	@echo "Formatting JS/TS driver SDK files..."
	(cd qpi-driver/js && npm ci && npm run format)

format-go-driver:
	@echo "Formatting Go driver SDK files..."
	(cd qpi-driver/go && go fmt ./...)

# ---------------------------------------------------------------------------
# Package / Publish targets
# ---------------------------------------------------------------------------
package-js:
	@echo "Packaging JS client..."
	(cd qpi-client/js && npm ci && npm run build)

package-py:
	@echo "Packaging Python client..."
	$(UV) build --project qpi-client/py/

package-driver:
	@echo "Packaging Python driver..."
	$(UV) build --project qpi-driver/py/

package-driver-js:
	@echo "Packaging JS/TS driver SDK..."
	(cd qpi-driver/js && npm ci && npm run build)

package-driver-go:
	@echo "Go driver SDK is a module — no packaging step required."
	@echo "Consumers import it directly: go get github.com/sopherapps/qpi/qpi-driver/go"

package-go:
	@echo "Go client is a module — no packaging step required."
	@echo "Consumers import it directly: go get github.com/sopherapps/qpi/qpi-client/go"

publish-js:
	@echo "Publishing JS client to npm..."
	(cd qpi-client/js && npm publish --access public)

publish-driver-js:
	@echo "Publishing TypeScript driver SDK to npm..."
	(cd qpi-driver/js && npm publish --access public)

publish-py:
	@echo "Publishing Python client to PyPI..."
	$(UV) build --project qpi-client/py/
	$(UV) publish --project qpi-client/py/

# ---------------------------------------------------------------------------
# Clean
# ---------------------------------------------------------------------------

clean:
	@echo "Cleaning up..."
	rm -rf bin/pb_data bin/data bin/qpi bin/dist bin/builds
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type d -name "*.egg-info" -exec rm -rf {} +
	find . -type d -name ".pytest_cache" -exec rm -rf {} +
	find . -type d -name ".ruff_cache" -exec rm -rf {} +
	rm -rf qpi-driver/py/build qpi-driver/py/dist qpi-driver/py/*.egg-info qpi-driver/py/.venv .venv
	rm -rf qpi-driver/js/dist qpi-driver/js/node_modules
	rm -rf qpi-client/js/dist qpi-client/js/node_modules
	rm -rf qpi-client/py/build qpi-client/py/dist qpi-client/py/*.egg-info qpi-client/py/.venv

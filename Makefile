.PHONY: test-dashboard test-docs test-docs-static test-docs-snippets test-docs-example test-docs-site all build build-dashboard test test-js-driver test-go-driver lint lint-go lint-py lint-js lint-dashboard lint-go-client lint-py-client lint-js-driver lint-go-driver format format-go format-py format-js format-dashboard format-go-client format-py-client format-js-driver format-go-driver package package-driver package-driver-js package-driver-go package-js package-py package-go publish-js publish-driver-js publish-py clean venv-check test-e2e-dashboard

VERSION ?= 0.4.1
UV := $(shell command -v uv 2> /dev/null || echo "$$HOME/.local/bin/uv")
EXECUTORS := mock aer quantify qblox

# Scratch locations for the documentation checks. Under bin/, which is already
# ignored, so a failed run leaves nothing in the working tree for git to notice.
DOCS_EXAMPLE_VENV := bin/.docs-example-venv
DOCS_SITE_VENV := bin/.docs-site-venv
DOCS_SITE_OUT := bin/.docs-site
# The framework modules the coverage floor applies to: the SDK, the CLI, the device
# registry and its options, and the executors that need no hardware. Everything else
# is reported but not gated — see cov-py.
PY_COV_INCLUDE := qpi_driver/cli.py,qpi_driver/sdk.py,qpi_driver/events.py,qpi_driver/paths.py,qpi_driver/options.py,qpi_driver/builtins/*.py,qpi_driver/executors/__init__.py,qpi_driver/executors/base/*.py,qpi_driver/executors/mock/*.py
PY_COV_MIN := 96


# `uv sync` reinstalls qblox_instruments, and macOS strips the code signature
# from the q1asm assembler it bundles — after which every schedule that
# assembles dies with "Assembly failed". So each target that syncs has to put
# the signature back. Silent and `|| true`: on Linux, and in an environment
# without qblox_instruments at all, there is nothing to sign and that is fine.
RESIGN_Q1ASM = @if [ "$$(uname)" = "Darwin" ]; then codesign --force --deep --sign - qpi-driver/py/.venv/lib/python3.12/site-packages/qblox_instruments/assemblers/q1asm_macos 2>/dev/null || true; fi

# The Go packages the coverage floor applies to: the device registry and the CLI over
# it, both of which need no server. The base SDK (driver.go: Run, recvLoop, the TLS
# dialling) and `qpi-driver/main.go` are reported but not gated — the first needs a
# live server and is covered by `make test-e2e-driver`, and the second is `main`,
# which no in-process test can call. `cli.Execute` is inside a gated package but
# calls os.Exit, hence 94 rather than 96.
GO_COV_GATED := ./devices/... ./cli/...
GO_COV_MIN := 94

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
	$(RESIGN_Q1ASM)
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

test: test-go test-py test-js-client test-go-client test-py-client test-js-driver test-go-driver test-dashboard test-docs test-e2e

# ---------------------------------------------------------------------------
# Documentation, tested rather than proof-read.
#
# Every check here exists because the thing it checks was wrong at some point and
# nothing said so: a `make` target that only existed in .PHONY, a `pip install` path
# with no pyproject.toml in it, a config file named with the wrong extension, a
# TypeScript block that did not type-check, an executor example that could not be
# instantiated, and an entry-point device that was silently skipped because loading it
# raced the SDK's own import. Documentation drifts in one direction only — nobody
# re-runs the command they copied a block from — so the only fix that holds is a
# failing build.
#
# Split into four steps so a failure names which kind of claim broke.
# ---------------------------------------------------------------------------
test-docs: test-docs-static test-docs-snippets test-docs-example test-docs-site

# The static claims: make targets, repository paths, links, and the CLI flags every
# document names, checked against each SDK's own --help.
test-docs-static:
	@echo "Checking the documentation's claims about this repository..."
	(cd qpi-driver/js && npm ci --silent && npm run --silent build)
	$(UV) sync --project qpi-driver/py --extra cli
	# --extra cli on the *run* as well as the sync. check_docs asks the CLI for
	# its own flags, and forces PYTHONPATH at the source tree so `qpi_driver.cli`
	# imports whether or not it is installed — which means a run in an
	# environment missing typer gets the stub CLI, exits 0, and reports every
	# documented flag as removed. Naming the extra here makes the environment a
	# property of this target rather than of whatever target ran before it.
	$(UV) run --project qpi-driver/py --extra cli python scripts/check_docs.py

# The code blocks: Python executed against the real SDK, Go and TypeScript compiled.
test-docs-snippets:
	@echo "Running the documentation's Python snippets and the CLI transcripts..."
	$(UV) run --project qpi-driver/py python -m pytest \
		qpi-driver/py/tests/test_docs.py qpi-driver/py/tests/test_docs_snippets.py -v
	@echo "Compiling the documentation's Go and TypeScript snippets..."
	bash scripts/check_doc_snippets.sh

# The entry-point route end to end: install the example against *this* SDK and ask the
# CLI whether the device arrived. Only ever exercised with `entry_points` mocked
# before, which is how a circular import that skipped every installed device survived.
test-docs-example:
	@echo "Installing examples/custom_device and checking the CLI can run it..."
	rm -rf $(DOCS_EXAMPLE_VENV)
	$(UV) venv --python 3.12 $(DOCS_EXAMPLE_VENV)
	VIRTUAL_ENV=$(DOCS_EXAMPLE_VENV) $(UV) pip install --quiet "./qpi-driver/py[cli]"
	VIRTUAL_ENV=$(DOCS_EXAMPLE_VENV) $(UV) pip install --quiet --no-deps \
		./qpi-driver/py/examples/custom_device
	$(DOCS_EXAMPLE_VENV)/bin/qpi-driver devices 2>&1 | grep -q "quantum_x" \
		|| { echo "FAILED: the installed example's device is not registered"; exit 1; }
	@# A skipped entry point is a warning, not an error, so the exit code alone would
	@# not have caught it: the warning itself has to be absent.
	$(DOCS_EXAMPLE_VENV)/bin/qpi-driver devices 2>&1 \
		| grep -q "skipping device entry point" \
		&& { echo "FAILED: the installed example's entry point was skipped"; exit 1; } \
		|| echo "OK: quantum_x is registered, with no skipped entry point"
	rm -rf $(DOCS_EXAMPLE_VENV)

# The site itself: a broken nav entry or a dead internal link. `docs.yml` only runs on
# a v* tag, so without this nothing validates the documentation on a pull request.
test-docs-site:
	@echo "Building the documentation site (--strict)..."
	rm -rf $(DOCS_SITE_VENV)
	$(UV) venv --python 3.12 $(DOCS_SITE_VENV)
	VIRTUAL_ENV=$(DOCS_SITE_VENV) $(UV) pip install --quiet mkdocs-material "mkdocstrings[python]"
	$(DOCS_SITE_VENV)/bin/mkdocs build --strict --site-dir $(DOCS_SITE_OUT)
	rm -rf $(DOCS_SITE_VENV) $(DOCS_SITE_OUT)

test-go: build-dashboard
	@echo "Running Go unit tests (server)..."
	(cd qpi-ui && go test -race -v ./...)

test-go-minimal:
	@echo "Running Go server unit tests..."
	(cd qpi-ui && go test -race -cover ./...)

test-py: test-py-sim
	@for exec in $(EXECUTORS); do \
		$(MAKE) test-py-driver EXECUTOR=$$exec || exit 1; \
		$(MAKE) test-py-loop EXECUTOR=$$exec || exit 1; \
	done

test-py-driver:
	@echo "Running Python driver tests with [$(EXECUTOR)] extra..."
	$(UV) sync --project qpi-driver/py --extra $(EXECUTOR) --dev
	$(RESIGN_Q1ASM)
	$(UV) run --project qpi-driver/py pytest qpi-driver/py/tests/ -v

# The gated run. It is the [cli] extra's because that one imports the most: the base
# run skips every CLI test, so a floor there would be measuring a smaller program.
test-py-cli:
	@echo "Running Python driver tests with [cli] extra..."
	$(UV) sync --project qpi-driver/py --extra cli --dev
	$(RESIGN_Q1ASM)
	(cd qpi-driver/py && $(UV) run pytest tests/ -v --cov --cov-report=)
	@echo "Enforcing $(PY_COV_MIN)% coverage on the framework modules..."
	(cd qpi-driver/py && $(UV) run coverage report \
		--include='$(PY_COV_INCLUDE)' --fail-under=$(PY_COV_MIN))
	@echo "Coverage of the hardware executors, for information only:"
	-(cd qpi-driver/py && $(UV) run coverage report \
		--include='qpi_driver/executors/qblox/*,qpi_driver/executors/quantify/*,qpi_driver/executors/presto/*,qpi_driver/executors/qiskit_aer/*,qpi_driver/executors/utils/*,qpi_driver/compat/*')

test-py-sim:
	@echo "Running Python calibration tests against the physics simulator..."
	$(UV) sync --project qpi-driver/py --extra sim --dev
	$(RESIGN_Q1ASM)
	$(UV) run --no-sync --project qpi-driver/py pytest -v \
		qpi-driver/py/tests/test_physics_simulation.py \
		qpi-driver/py/tests/test_calibration_e2e.py

test-py-loop:
	@echo "Running the calibrate/process loop against the $(EXECUTOR) simulated chip..."
	$(UV) sync --project qpi-driver/py --extra $(EXECUTOR) --extra sim --dev
	$(RESIGN_Q1ASM)
	# --no-sync, because `uv run` otherwise re-syncs to the project's *default*
	$(UV) run --no-sync --project qpi-driver/py pytest \
		qpi-driver/py/tests/test_calibration_loop.py -v


# The dashboard's pure helpers — graph layering and the state derivation beside it.
# Anything needing a DOM is a Cypress spec against the real server instead
# (RFC 0006 §10), which is what test-e2e-dashboard runs.
test-dashboard:
	@echo "Running dashboard unit tests..."
	(cd qpi-ui/internal/dashboard && CYPRESS_INSTALL_BINARY=0 npm ci && npm test)

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
	$(RESIGN_Q1ASM)
	$(UV) run --project qpi-client/py pytest qpi-client/py/tests/ -v

test-js-driver:
	@echo "Running JS/TS driver SDK tests..."
	(cd qpi-driver/js && npm ci && npm test)

test-go-driver:
	@echo "Running Go driver SDK tests..."
	(cd qpi-driver/go && go test -race -v ./...)
	@echo "Enforcing $(GO_COV_MIN)% coverage on $(GO_COV_GATED)..."
	(cd qpi-driver/go && go test -coverprofile=/tmp/qpi-go-cov.out $(GO_COV_GATED) >/dev/null \
		&& go tool cover -func=/tmp/qpi-go-cov.out | tail -1 \
		&& go tool cover -func=/tmp/qpi-go-cov.out | awk -v min=$(GO_COV_MIN) '\
			/^total:/ { got = $$3 + 0; \
				if (got < min) { printf "Coverage failure: total of %s is less than %d%%\n", $$3, min; exit 1 } \
				printf "Coverage OK: %s\n", $$3 }')
	@echo "Coverage of the base SDK transport, for information only:"
	-(cd qpi-driver/go && go test -cover ./... | grep coverage)

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

# Pass SPEC=<glob> to run one spec instead of all 42 — the difference between
# eight seconds and four minutes when iterating on a single tab. e.g.
#   make test-e2e-dashboard SPEC=cypress/e2e/calibration/calibration-tab.cy.ts
test-e2e-dashboard:
	@echo "Running E2E Cypress dashboard tests..."
	./e2e/test_dashboard_cypress.sh $(if $(SPEC),--spec "$(SPEC)")

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

# scripts/ too: it holds the two Python tools this repository runs on itself, and an
# unformatted one is the same kind of drift as an unformatted SDK file.
lint-py:
	@echo "Linting Python driver files..."
	$(UV) run --project qpi-driver/py ruff check qpi-driver/py/ scripts/
	$(UV) run --project qpi-driver/py ruff format --check qpi-driver/py/ scripts/

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

format-py:
	@echo "Formatting and sorting imports for Python driver files..."
	$(UV) run --project qpi-driver/py ruff format qpi-driver/py/ scripts/
	$(UV) run --project qpi-driver/py ruff check --select I --fix qpi-driver/py/ scripts/

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

# Change log

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](http://keepachangelog.com/)
and this project follows versions of format `{year}.{month}.{patch_number}`.

## [Unreleased]

### Added

- `qpi-driver/py`: Added the device catalog (RFC 0003 §5) — `Operation`, `OperationSpec`, `DeviceSpec`, `OptionSpec` and `DeviceBuilder` in `qpi_driver.builtins.registry`, with `register()`, `operations()`, `devices()` and `resolve()`. A device now describes itself as data next to its own code, so `--device` accepts it without any change to the CLI.
- `qpi-driver/py`: Added `qpi_driver.builtins.qpu.device_spec()` for describing a `process` device that runs a given executor, including one the SDK does not ship.
- `qpi-driver/py`: `process --help` and `monitor --help` now list every device the operation can run and every `-o` key each one reads, with its type, default, example and whether it is required — generated from the device specs, so a new device appears with no CLI change. A device whose install target is not fully installed is listed as `qblox — unavailable: pip install "qpi-driver[cli,qblox]"`, read from package metadata without importing anything, so `--help` works when a device does not.
- `qpi-driver/py`: Added `qpi-driver devices [--operation OP]`, the same catalog as readable text for every operation at once.
- `qpi-driver/py`: Added `qpi-driver catalog --json`, the whole catalog as JSON for another program to read. The shape is a contract — QPI-UI checks its own catalog against it, and the Go and TypeScript SDKs mirror it — carries a `schema_version`, and is documented in full on `qpi_driver.builtins.catalog.catalog_dict` (RFC 0003 §9).
- `qpi-driver/py`: Added `DeviceSpec.parse_options()`, `OptionSpec.parse`/`OptionSpec.type_name` and `qpi_driver.paths.as_safe_dir()` — an option's value is checked and converted by its own schema, in one place, instead of each builder hand-rolling `int(...)`, boolean-ish string parsing and path validation.
- `qpi-driver/py`: A distribution can now advertise devices through the `qpi_driver.devices` entry-point group (RFC 0003 §6). `pip install mylab-devices` and its devices appear in `--help`, in `catalog --json` and to `--device`, indistinguishable from a built-in, with nothing to change in the SDK. An entry point that will not import or does not resolve to a `DeviceSpec` is logged and skipped, never fatal.
- `qpi-driver/py`: A device can now be named by import path — `--device mylab.devices:PrestoV2`, or `mylab.devices.PrestoV2`, following Pydantic's `ImportString` convention. For `process` it must import to an `Executor` subclass or instance, for `monitor` to a device builder; either accepts a `DeviceSpec`, which is how a custom device gets a declared option schema and a `--help` entry. Having no declared schema, an import-path device's undeclared `-o` options are passed through as strings — its declared ones, `data_dir` included, are still validated, so the safe-path check applies on this route too. A path that will not import exits 1 with one line, with the filesystem path Python volunteers stripped out of it (RFC 0003 §10).
- `qpi-driver/py`: Added `qpu.device_spec(options=...)` for a custom process device that wants to declare its executor's own `-o` options, and `DeviceSpec.accepts_any_option` for one that cannot.
- `qpi-driver/py`: Added `qpi-driver/py/examples/custom_device/` — an `Executor`, a `DeviceSpec`, and the `pyproject.toml` entry-point stanza, with a README covering all three ways to run it.
- `qpi-driver/py`: `JobPayload` and `CircuitPayload` are now exported from `qpi_driver` itself, which is what writing an executor needs.
- `qpi-driver/go`: Added the importable `devices` package — `Operation`, `OperationSpec`, `OptionSpec`, `DeviceSpec`, `DeviceBuilder`, `Options`, `Registry`, `Register`, `Devices`, `Resolve`, `Catalog` — the Go counterpart of the Python SDK's device catalog (RFC 0003 §5, §8). The device table was an unexported `map[string]deviceRunner` in `package main`, so nothing downstream could extend it.
- `qpi-driver/go`: Added the importable `cli` package with `NewRootCmd` and `Execute`. Go has no runtime import by name, so extension is compile-time: a build of your own calls `devices.Register` and then `cli.Execute`, and gets the same `start`/`devices`/`catalog` commands, the same generated help and the same option validation as the shipped binary. `qpi-driver/go/qpi-driver/main.go` is now exactly that — register the built-ins, hand off. The README documents it with an example that is compiled as part of verifying it.
- `qpi-driver/go`: Added `qpi-driver devices [--operation OP]` and `qpi-driver catalog --json`, emitting the same document as the Python SDK — byte-identical for the shared `bluefors_gen1` device, apart from `extra`, which names a Python install target and is empty where a device is compiled in.
- `qpi-driver/go`: The `bluefors_gen1` monitor now describes itself as a `DeviceSpec` beside its own code, with its five `-o` options typed. `qpi-driver/go/cli` and `qpi-driver/go/devices` have tests where `qpi-driver/go/qpi-driver` had none at all.

### Changed

- `qpi-driver`: [BREAKING] The `process` and `monitor` subcommands are replaced by one `start` verb taking `--operation`, in all three SDKs at once (RFC 0003 §4). One verb because everything about launching a driver is the same whichever operation it is — and because a third party can add a device, but only QPI-UI can add an operation, so the operation is an argument rather than part of the grammar.

  | Before | After |
  |--------|-------|
  | `qpi-driver process --device qblox …` | `qpi-driver start --operation process --device qblox …` |
  | `qpi-driver monitor --device bluefors_gen1 …` | `qpi-driver start --operation monitor --device bluefors_gen1 …` |

  `--operation` is required, has no short form (`-o` is `--option`, and `-O` beside it would be a hazard), and reads `QPI_OPERATION`. `--device` and `--name`, when omitted, now come from the operation's own defaults. The dashboard's setup snippets, all three `install-systemd.sh` installers and the e2e harness render the new grammar; the `OPERATION` environment variable the installers take is unchanged.
- `qpi-driver/go`, `qpi-driver/js`: [BREAKING] `start --operation process` now says that the SDK ships no process devices and where to find one, instead of `unknown process device "mock"; known devices: ` with an empty list and a default device that never existed there (RFC 0003 §8).
- `qpi-driver/py`: [BREAKING] An `-o` key the chosen device does not read is now an error naming the keys it does, where before it was silently ignored. A typo such as `-o data_dirr=/data` used to mean a driver running with a default nobody chose; it now exits 1. Anything that passed an unrecognised `-o` key deliberately, expecting it to reach the executor, must declare it in a `DeviceSpec` (see `qpu.device_spec()`).
- `qpi-driver/py`: [BREAKING] A device builder is now handed options that have already been checked and coerced against its own `DeviceSpec` — `build_from_options(options=spec.parse_options(raw))` — rather than raw strings. Calling a builder directly with `{"job_timeout": "30"}` no longer coerces it, and an omitted key is no longer defaulted by the builder.
- `qpi-driver/py`: [BREAKING] A device builder now returns an *unstarted* driver and the caller starts it, matching the TypeScript SDK (RFC 0003 §7). A driver can therefore be built and asserted on with no server running.

  | Removed | Replacement |
  |---------|-------------|
  | `run_driver(...)` | `QpuDriver(...).run()` |
  | `qpu.run_process(device=..., ...)` | `qpu.build_from_options(executor=..., ...).run()` |
  | `bluefors_gen1.run_monitor(...)` | `bluefors_gen1.build_from_options(...).run()` |
  | `builtins.PROCESS_DRIVERS`, `builtins.MONITOR_DRIVERS` | `builtins.devices(operation)` / `builtins.resolve(operation, device)` |
  | `builtins.DriverRunner` | `builtins.DeviceBuilder` (returns a driver rather than blocking) |
  | `resolve_executor(executor, custom_executors, ...)` | `resolve_executor(executor, ...)` — pass the class or instance itself |
  | `QpuDriver(custom_executors={"name": Cls})` | `QpuDriver(executor=Cls)` |
  | `QpuDriver.OPERATION`, `BlueforsGen1Driver.OPERATION` | `DeviceSpec.operation` |
  | `qpu.execute_job()` | `qpu.job_worker()` |

- `qpi-driver/py`: The driver-authoring surface is unchanged — `QpiDriver`, `handle_event()`, `emit()`, `every()`, `Event`, `EventType` and `Executor` keep their names and signatures. Only how a driver is registered and launched moved.

### Fixed

- `qpi-driver/go`: `--help` and `devices` no longer advertise an operation's default device in a build that does not have it, and omitting `--device` in such a build now asks for one, naming what is registered, instead of failing over a device the operator never typed. Which devices a Go binary has is decided when it is compiled.
- `qpi-driver/py`: Fixed the custom-executor example in `qpi-driver/py/README.md`, which passed a `custom_executor=` keyword that no function accepted and would have failed with `Unknown executor name 'custom'`.

## [0.1.2] - 2026-07-24

### Added

- `qpi-ui`: Added Admin Theme Management feature (RFC 0002) allowing administrators to create, preview, activate, and delete custom themes directly from the dashboard.
- `qpi-ui`: Added `themes` collection to PocketBase database for persisting theme records and custom branding configurations (logo, favicon).
- `qpi-ui`: Added `/api/theme/defaults`, `/api/theme/active`, `/api/theme/css`, and `/api/theme/js` endpoints to serve active theme configuration and injected assets.
- `qpi-ui`: Added `activeTheme` to `AppConfig` to serve as a high-performance, globally consistent in-memory cache for the active theme, avoiding expensive database queries.
- `qpi-ui`: Added React `ThemeContext` on the frontend for dynamic application of CSS variables (`rgb()` variants) and custom assets based on the active theme, gracefully falling back to a compiled-in default theme.
- `qpi-ui`: Added a Theme management UI to the Admin Dashboard (Settings -> Appearance) for customizing Design Tokens (JSON) and raw Custom CSS/JS with real-time preview functionality.
- `qpi-ui`: Optimized `OnThemeUpsert` hook to use a raw database query to efficiently deactivate sibling themes, avoiding nested hook executions.
- `docs`: Added `docs/theming.md` documentation guide for the Dashboard Theming engine.

### Fixed

- `qpi-driver/js` and `qpi-client/js`: Fix failing npm publish in GitHub actions

## [0.1.1] - 2026-07-23

### Added

- `qpi-ui`: Added the event-based driver framework (RFC 0001) with the `drivers` collection (`name`, `qpu`, `kind`, `language`, `events`, `token`, `status`, NNG ports) for registering and managing external driver processes.
- `qpi-ui`: Added `POST /api/op/drivers/create`, `POST /api/op/drivers/connect`, and `POST /api/op/drivers/toggle` endpoints for driver lifecycle, token issuance, and TLS/NNG port negotiation.
- `qpi-ui`: Added the `events` trace log collection for driver-to-UI events (`source`, `driver`, `qpu`, `type`, `payload`, `ts`) with composite index `idx_events_type_ts` on `events(type, ts)`.
- `qpi-ui`: Added background retention pruning for the `events` log (`events-retention` / `QPI_EVENTS_RETENTION`, default `720h`; `events-prune-interval` / `QPI_EVENTS_PRUNE_INTERVAL`, default `1h`).
- `qpi-ui`: Added per-driver inbound event rate limiting (`event-rate-limit` / `QPI_EVENT_RATE_LIMIT`, default `100`/sec).
- `qpi-ui`: Added the **Drivers** and **Monitoring** dashboard pages for superusers — managing driver records, copying setup snippets, viewing live status, and displaying real-time `CryostatReading` telemetry charts over PocketBase realtime.
- `qpi-ui`: Added `CryostatReading` event type and handler that persists telemetry readings to the `events` collection.
- `qpi-driver`: Added the Python driver SDK (`qpi-driver/py`, `QpiDriver` base class with `handle_event()`, `emit()`, `every()`), typed event envelope (`Event`, `EventType`), and re-expressed QPU execution as `QpuDriver` (`run_driver`).
- `qpi-driver`: Added the TypeScript driver SDK (`qpi-driver/js`, npm `qpi-driver`) with zero runtime dependencies, implementing NNG PULL/PUSH over Node's built-in `tls`.
- `qpi-driver`: Added the Go driver SDK (`qpi-driver/go`, `go get github.com/sopherapps/qpi/qpi-driver/go`) implementing `Base` over `go.nanomsg.org/mangos`.
- `qpi-driver`: Added the official `bluefors_gen1` cryostat monitoring driver across Python (`qpi-driver[cli,bluefors_gen1]`), TypeScript (`qpi-driver/builtins/bluefors-gen1`), and Go (`qpi-driver/go/qpi-driver/bluefors`), polling the Bluefors Remote Access Control API Gen. 1.
- `qpi-driver`: Added unified CLI runners (`process`, `monitor`, `version`) and systemd installer scripts (`install-systemd.sh`) across Python, TypeScript, and Go.
- `docs`: Added RFC 0001 (`docs/rfcs/0001-driver-framework.md`) and the driver framework operations runbook (`docs/driver/operations.md`).

### Changed

- `qpi-ui` & `qpi-driver`: Unified all QPU driver operations on the event-based driver framework (RFC 0001), replacing legacy direct QPU connections with event-driven `QpuDriver` instances.
- `qpi-driver`: [BREAKING] Reorganised the Python SDK repository directory from `qpi-driver/` to `qpi-driver/py/`, matching `qpi-driver/js` and `qpi-driver/go`.
- `qpi-driver`: [BREAKING] Reorganised the driver CLI around operations (`process` for QPUs, `monitor` for telemetry sensors) dispatched by `--device` with repeatable `-o key=value` options instead of the legacy `start --executor` interface.
- `qpi-ui`: Moved driver catalog definitions into a data-driven `internal/drivers` registry keyed by operation.
- `e2e`: Updated verification suite and test runners to connect all drivers via `POST /api/op/drivers/connect` and validate `bluefors_gen1` monitoring events across Python, TypeScript, and Go.
- `qpi-ui`: [BREAKING] The `QPU` struct was stripped of connection-related state that is now managed by the Driver framework. Removed `AccessToken`, `NNGCommandPort`, `NNGResultPort`, `DeviceConfig`, and `DriverVersion` fields, converting it into a pure registry entity.
- `qpi-ui`: [BREAKING] Simplified `handleQPUCreate` as it no longer generates tokens or sets up legacy executor configurations.
- `qpi-ui`: [BREAKING] Stripped removed QPU fields from `QPUCreateResponse`, `QPUUpdateResponse`, etc.
- `e2e`: Updated the backend/driver integration test (`verify.py`) to correctly retrieve authentication tokens using the new `drivers/create` endpoint rather than the removed fields on the QPU response.
- `e2e`: Fixed a local environment flakiness in the cypress script by utilizing `npm install --no-package-lock`.
- `docs`: Change driver docs folder structure to resemble that for clients due to qpi-driver folder restructure.

### Fixed

- `qpi-driver`: [BREAKING] Fixed inconsistent result dictionary shape returned by `build_qiskit_result`: `circuit_results` is now always present as a list of per-circuit experiment dicts regardless of circuit count, and redundant top-level `hex_counts` has been removed in favor of `circuit_results` and top-level `counts`.

### Removed

- `qpi-ui`: [BREAKING] Removed the legacy non-event QPU connection endpoint (`POST /api/op/qpus/connect`) and old dispatcher/listener routines. All drivers now connect through `POST /api/op/drivers/connect`.
- `qpi-driver`: [BREAKING] Removed legacy non-event driver module (`qpi_driver/driver.py`). Drivers now run via `QpuDriver` (`qpi_driver.builtins.qpu`).

## [0.1.0] - 2026-07-23

- Yanked

## [0.0.42] - 2026-07-21

### Added

- `qpi-driver`: Added `CRZGate` and `CPhaseGate` support to both the qblox and quantify executors' gate conversion.
- `qpi-driver`: Replaced the one-off Toffoli-only unitary test with at a test covering every unitary gate branch in `to_qblox_gates`/`to_quantify_gates`.

### Fixed

- `qpi-driver`: Fixed the misnamed `hex_counts` output of `build_qiskit_result`, which returned binary-string-keyed counts (duplicating `counts`) instead of hex-keyed counts: it now genuinely converts to hex via the existing `counts_to_hex` helper, and the redundant Qiskit `Result` construction (and its `build_experiment_result` helper) used only to derive that value was removed.
- `qpi-driver`: Fixed 'can only handle OpenQASM 2.0, but given 3.0' error caused by genuine error in OpenQASM 3
- `qpi-driver`: Fixed meas_level=2 counts collapsing all shots into one bin
- `qpi-driver`: Fixed meas_level=2 counts being keyed by qubit index/width instead of the classical register: a qubit measured into more than one clbit now reports each measurement as an independent bit, `measure q[i] -> c[j]` positions bits by clbit index `j` (little-endian, `c[0]` rightmost) rather than qubit index, and the bitstring width matches `num_clbits` instead of `2 ** n_qubits`.
- `qpi-driver`: Corrected the Toffoli (CCX) decomposition in both qblox and quantify executors.
- `qpi-driver`: Fixed the qblox and quantify executors only running the first circuit of a batch: `execute` now runs every circuit in `payload.circuits`, honouring per-circuit `shots` and `parameter_values`, and concatenates the results along a `circuit_index` dimension like the simulator executors.
- `qpi-driver`: Fixed multi-circuit batches with heterogeneous classical-bit/qubit widths raising or misaligning in the mock, qiskit-aer, qblox and quantify executors: per-circuit datasets are now bundled independently instead of being force-concatenated onto a shared axis, and the recorded `shots`/`n_qubits` metadata reflects what was actually used per circuit rather than the batch default or only the last circuit.
- `qpi-driver`: Fixed fragile `ThresholdedAcquisition` discrimination that relied on the backend returning exactly `1.0`: the discriminator now uses a midpoint threshold (`r >= 0.5`), correctly classifying floating-point values just below `1.0` and averaged fractional bins as `|1>`, consistent with the simulator path.
- `qpi-driver`: Fixed the qblox `FluxTunableCoupler` CZ compilation anchoring the virtual-Z phase corrections ambiguously: both `ShiftClockPhase` corrections now reference the square pulse explicitly (`ref_op=pulse`, `ref_pt="start"`) instead of the child correction implicitly chaining off the parent correction, so both are unambiguously applied at the pulse start and match the quantify executor's behaviour.
- `qpi-driver`: Removed a dead condition in the qblox `_apply_parameters`: `callable(attribute) and not hasattr(attribute, "__class__")` was always `False` since every object has `__class__`, so it never contributed to the branch decision; the condition now expresses only the check that actually applies.
- `qpi-driver`: Documented that `PhaseGate` and `RZGate` are intentionally mapped to the same `Rz` operation in both the qblox and quantify executors' gate conversion: they differ only by an unobservable global phase for a standalone gate. No functional change.

## [0.0.41] - 2026-07-20

### Changed

- `qpi-driver`: Made logging more verbose in qpi driver

### Fixed

- `qpi-driver`: Fixed invalid YAML error when loading quantify hardware json file
- `qpi-driver`: Fixed connection reset by peer errors caused by qblox-instruments >= 1.3.0

## [0.0.40] - 2026-07-17

### Fixed

- `qpi-driver`: Fixed 'Frequency settings underconstrained for freqs.clock=0. Neither LO nor IF supplied (freqs.LO=None, freqs.IF=None).'
- `qpi-driver`: Fixed 'ValueError: Operation 'CZ(qC='q1',qT='q2')' contains an unknown clock 'q1_q2.cz''

## [0.0.39] - 2026-07-17

### Changed

- `qpi-ui`: Reduced built binary size by compiling with `-ldflags="-s -w"` (stripping symbol table and DWARF debug information).

## [0.0.38] - 2026-07-17

### Added

- `qpi-driver`: Added safe-path validation for `--data-dir` and `--ca-file` to prevent writing to unsafe/unauthorized locations.
- `qpi-driver`: Added environment variable defaults for `QPI_DATA_DIR`, `QPI_CA_FILE`, `QPI_QUANTIFY_DEVICE_CONFIG`, and `QPI_QUANTIFY_HARDWARE_CONFIG` to the systemd service installer.
- `qpi-driver`: Added the FluxTunableCoupler as a CompositeSquareEdge for qblox and quantify executors

### Changed

- `docs`: Updated README files to provide detailed instructions for installing the server via pre-compiled binaries, native Linux packages (`.deb`), and using the non-interactive/interactive systemd installation script for the driver.
- `qpi-driver`: Sanitized driver/device names to replace hyphens (`-`) with underscores (`_`) for executor compatibility.
- `qpi-driver`: Added `--prerelease allow` flag to the `uv tool install` command for the `qblox` executor in `install-systemd.sh`.

### Fixed

- `qpi-driver`: Fixed permissions/directory creation bugs and improved `uv` location detection in `install-systemd.sh`.
- `qpi-driver`: Fixed standard Python test output teardown issues by gracefully unregistering the default QCoDeS instrument closing handler from `atexit` and closing them in a test session fixture while logging is still active.
- `qpi-driver`: Ensured the target parent directory for the CA certificate exists before saving the file.
- `qpi-driver`: Fixed minor code linting errors.

## [0.0.37] - 2026-06-29

### Fixed

- `docs`: Enabled mermaid diagram rendering in mkdocs material theme by adding the `pymdownx.superfences` markdown extension in `mkdocs.yml`.

## [0.0.36] - 2026-06-29

### Changed

- `qpi-ui/dashboard`: Moved the React dashboard path from `/dashboard/` to the root path `/` to improve user experience.

## [0.0.35] - 2026-06-27

### Fixed

- `qpi-ui`: Fixed a race condition where a driver failing to dial the NNG socket would incorrectly leave the QPU marked as `online`. QPU online status is now strictly determined by the NNG socket attachment lifecycle.
- `qpi-ui`: Fixed an issue where regenerating root CA certificates returned an empty fingerprint, causing authentication failures for new driver connections.

## [0.0.34] - 2026-06-27

### Fixed

- `qpi-ui`: Fixed an issue in the admin dashboard where dismissed system notifications reappeared on page refresh. Dismissals are now correctly persisted via proxy user API requests.
- `qpi-driver`: Fixed a `panic: nng is not fork-reentrant safe` error in multiprocessing environments by deferring the NNG TLSConfig initialization until after the worker processes have forked.

## [0.0.33] - 2026-06-27

### Added

- `qpi-ui`: Added `--ip-addr` (or `QPI_IP_ADDR`, or `ipAddr` in config) to explicitly specify the public IP for binding TLS sockets. The provided IP is now properly encoded in the X509 certificate's SAN IP block.

### Changed

- `qpi-driver`: Updated NNG setup logic. The driver now establishes connections using the explicit NNG IP address returned by the server via `ConnectResponse`, decoupling it from the HTTP QPI address.

### Fixed

- `qpi-ui`: Removed the `fetchHostIPs()` autodiscovery logic which caused unintended behavior when deployed behind proxies.
- `qpi-driver`: Fixed a race condition where the result sender process could attempt to read the CA certificate from disk before the main process had downloaded it.

## [0.0.32] - 2026-06-26

### Fixed

- `qpi`: Fixed various linting errors across the Go and React UI codebases.

## [0.0.31] - 2026-06-26

### Changed

- `qpi-ui`: Made the metric cards on the Overview dashboard (Active QPUs, Queue Status, Next Booking) clickable so they quickly route to their respective tabs.
- `qpi-ui`: Clarified the "Load Example" button text and icon in the Jobs Console to read "Load Bell State Example".

### Fixed

- `qpi-ui`: Fixed "authentication required" error that occurred when superusers attempted to submit a quantum job. Superusers are now transparently issued a proxy `users` record with unlimited QPU seconds to satisfy relational constraints.

## [0.0.30] - 2026-06-26

### Added

- `qpi-ui`: Light mode support for the dashboard UI with a theme toggle. Dark mode remains the default.
- `qpi-ui`: Added an admin option to delete QPUs from the QPU Registry, complete with a confirmation modal.
- `qpi-ui`: Added a user profile dropdown menu in the dashboard top bar for quick access to settings and signing out.
- `qpi-ui`: Synchronized auth sessions across tabs and between the `/_/` admin UI and `/dashboard`, automatically signing users in/out when state changes globally.

### Changed

- `qpi-ui`: Restricted the "Create QPU" and "Toggle Status" buttons in the QPU Registry tab to administrators only, while still allowing standard users to view available QPUs.
- `qpi-ui`: Conditionally hide the username and password login fields if `passwordAuth` is disabled in the PocketBase users collection.

### Fixed

- `qpi-ui`: Fixed the QPU Registry cards to properly display the Executor Driver (`executor_type`).

## [0.0.29] - 2026-06-25

### Fixed

- `ci`: Fixed failing python test step in CI.

## [0.0.28] - 2026-06-25

### Fixed

- `ci`: Fixed failing lint step in CI by updating `uv sync` flags.

## [0.0.27] - 2026-06-25

### Fixed

- `qpi-ui`: Fixed `CHANGELOG.md` versioning mismatch and correctly restored `0.0.25` entries. Bumping version to `0.0.27` due to tag immutability on `0.0.26`.

## [0.0.26] - 2026-06-25

### Fixed

- `qpi-ui`: Reverted the hiding of the "QPU Registry" dashboard tab for standard non-admin users so that they can see existing QPUs (but cannot register or toggle them).

## [0.0.25] - 2026-06-25

### Added

- `qpi-driver`: Added `install-systemd.sh` script to automate installation of the driver as a systemd background service.
- `qpi-ui`: Added an admin-only endpoint `GET /api/op/version` to retrieve the server's version.
- `qpi-ui`: Added a dynamic version label to the dashboard sidebar (visible only to admins).
- `qpi-ui`: Updated the QPU Registration success modal to generate and display a copyable `install-systemd.sh` execution snippet.
- `ci`: Added a dedicated E2E testing job (`test-systemd-installer`) in GitHub Actions to validate the systemd installation script via a Docker container.

### Changed

- Global: Renamed all instances of "Orchestrator" to "Server" (and "orchestrator" to "server") across documentation, code, and CI scripts.
- Global: Renamed all instances of "Hardware Driver" to "QPU Driver" (and "hardware driver" to "QPU driver") across the project.
- `qpi-ui`: Simplified the `README.md` introduction with a shorter description, a simpler mermaid diagram, and pulled the Quick Start section to the top.

### Fixed

- `qpi-ui`: Fixed a bug in the dashboard (`App.tsx` and `Sidebar.tsx`) where the "QPU Registry" tab was still visible to standard non-admin users.
- `qpi-ui`: Fixed a double-hashing bug in `handleQPUCreate` that caused driver connection snippet tests to fail with `401 Unauthorized`.

## [0.0.24] - 2026-06-23

### Fixed

- Updated the CHANGELOG appropriately.

## [0.0.23] - 2026-06-23

### Fixed

- `qpi-ui`: Used GoReleaser NFPM overrides to separate Debian/RPM and Alpine `init` script configurations, preventing `dpkg` installation crashes (`Default-Start contains no runlevels`) and eliminating improper `systemd` dependencies in `.apk` packages.


## [0.0.22] - 2026-06-23

### Fixed

- `qpi-ui`: Added `draft: true` to all intermediate `softprops/action-gh-release` asset upload steps to prevent them from prematurely publishing the GitHub release and triggering immutable release errors on subsequent jobs.

## [0.0.21] - 2026-06-23

### Added
- `qpi-ui`: Added macOS ARM64 (Apple Silicon) native installer packaging to the CI pipeline.

### Fixed

- `qpi-ui`: Fixed directory pathing error during the macOS binary build step in GitHub Actions.
- `qpi-ui`: Fixed macOS pkg output path evaluation and eliminated a GitHub release asset race condition between parallel macOS runners.

## [0.0.20] - 2026-06-23

### Fixed

- `qpi-ui`: Configured GoReleaser to create a `draft` release and automated publishing at the end of the pipeline to avoid GitHub's immutable release asset errors during Windows MSI and macOS PKG uploads.

## [0.0.19] - 2026-06-23

### Fixed

- `github-actions`: Updated Node.js version from 20 to 22 in CI jobs to resolve deprecation warnings.

## [0.0.18] - 2026-06-23

### Fixed

- `qpi-ui`: Added `wixl` to the apt-get install step to fix missing command during Windows MSI packaging.

## [0.0.17] - 2026-06-23

### Fixed

- `qpi-ui`: Fixed GoReleaser LICENSE path and removed invalid NFPM contents entry.

## [0.0.16] - 2026-06-23

### Fixed

- `qpi-ui`: Fixed GoReleaser v2 syntax errors and NFPM script names.

## [0.0.15] - 2026-06-23

### Changed

- `qpi-ui`: Upgraded the packaging to support 'rpm', 'apk', macOS and windows installers.

## [0.0.14] - 2026-06-23

### Fixed

- `qpi-ui`: Fixed the loading of flags which were not taking effect even when supplied.

## [0.0.13] - 2026-06-21

### Fixed

- Fixed broken links and typos in docs website.

## [0.0.12] - 2026-06-21

### Fixed

- Fixed failing deployment of documentation site in GitHub actions on push to new tag.

## [0.0.11] - 2026-06-21

### Fixed

- Fixed failing deployment of documentation site in GitHub actions

## [0.0.10] - 2026-06-21

### Added

- Documentation site configuration (`mkdocs.yml`) with automated deployments (`docs.yml`) to GitHub Pages via MkDocs Material.

## [0.0.9] - 2026-06-21

### Added

- TLS connection between the server (qpi-ui) and the driver (qpi-driver)
- `qpi-driver`: added the `--ca-file` and `--ca-fingerprint` params to the qpi-driver all
- `qpi-ui (dashboard)`: updated the code snippet shown to the user on QPU creation to include `--ca-fingerprint`.
- Comprehensive Cypress E2E test suite covering all dashboard sections:
  - **Auth & Navigation** — login error flow, role-based navigation, hash routing, back/forward sync, logout
  - **QPU Registry** — admin QPU registration (with token and command verification), toggle online/offline, regular user restrictions
  - **Jobs Console** — default form state, job submission and results, QPU dropdown filtering, empty state
  - **Bookings** — booking a time slot, validation (end before start), cancel with confirmation, visibility (user vs admin)
  - **Admin Panel** — user quota allocations, time request approval/rejection, broadcast announcements, notification badge, approval quota updates
  - **Overview & Header** — metrics row accuracy, quick-action navigation, recent jobs table, notifications panel (dismiss individual/clear all), notification targeting (broadcast vs targeted), notification dismiss isolation (per-user), header page title sync
  - **Settings & Request Time** — profile settings (email, quota, role badge), request time modal submission, validation (empty reason/seconds)
  - **Error & Edge Cases** — empty states (no jobs, no selected job, no QPUs), network failure handling (`alert()` messages), unauthorized access to `/#admin`
- Backend unit tests for `OnQPUTimeRequestUpdateRequest` hook:
  - Approval adds requested seconds to user quota; rejection leaves it unchanged
  - Non-superusers are forbidden from updating time requests
  - Already-processed (approved/rejected) requests cannot be modified

## [0.0.8] - 2026-06-17

### Added

- Added READMEs for all client packages (Go, JS, Python) and the QPU driver

## [0.0.7] - 2026-06-17

### Added

- Added logo to CLI and README

### Fixed

- Fixed error with 'make package' failing due to missing dashboard built files

## [0.0.6] - 2026-06-17

### Fixed

- Fixed failing tests on GitHub CI and reduced pocketbase's verbosity.

## [0.0.5] - 2026-06-17

### Changed

- Fixed GitHub Actions matrix for tests sleeping.

## [0.0.4] - 2026-06-17

### Changed

- `qpi-driver`: [BREAKING] Changed the format of the `element_type` in quantify.device.yml to include
  `path (str)`, `args (tuple)` and `kwargs (dict)`
- `qpi-driver`: Unskipped the e2e errors for 'quantify' executor
- `qpi-driver`: Added a log file for the driver at `data/{executor}-driver.log` during e2e tests 

### Fixed

- `qpi-driver`: Failing e2e errors for 'qblox' executor. Specifically:
  - Fixed 4ns grid rounding misalignment on custom durations for `Delay` operations.
  - Added support for OpenQASM `Delay` instructions by mapping them to `IdlePulse`.
  - Added concurrent anchoring (`ref_pt="start"`) for parallel multi-qubit Qiskit instructions (e.g., `Measure`, `Delay`, `Barrier`).
  - Handled invalid `-1` hardware acquisition dummy data thresholds during Qblox and Quantify dummy measurements.
- `qpi`: Resolved Apple Silicon macOS codesign binary integrity crashes during the E2E suite due to dynamically installed `q1asm_macos`.


## [0.0.3] - 2026-06-16

### Changed

- `qpi-ui`: Refactored the hooks.go files to make them easier to read
  

## [0.0.2] - 2026-06-16

### Added

- `qpi-ui`: Centralized API payload and database collection schemas as Go structs in the new `qpi/internal/schema` package (including `User`, `APIToken`, `QPU`, `TimeSlot`, `QuantumJob`, `QPUTimeRequest`, `Notification`, and corresponding request/response payloads).
- `qpi-ui`: Added `*FromRecord` helper mapping functions in the `schema` package to safely construct database model structs from PocketBase `*core.Record` objects.
- `qpi-ui`: Added `qpi_addr` dynamically computed field to the `/api/op/qpus/create` JSON response.
- `qpi-ui/internal/dashboard`: Updated the QPU registry tab to show a success modal upon QPU registration, including copy-to-clipboard icons for both the raw access token and a copyable `qpi-driver` start command.
- `qpi-driver`: Added support for the Qblox Scheduler (`qblox-scheduler`) package via a new `QbloxExecutor` (`qblox`).
- `qpi-driver`: Added `qblox` optional-dependencies group to `pyproject.toml` and a compatibility layer at `qpi_driver/compat/qblox.py` to gracefully handle cases where `qblox-scheduler` is not installed.
- `qpi-driver`: Created automated test suite at `qpi_driver/tests/test_qblox.py` and integrated `test-py-qblox` test target into `GitHub CI` matrix.
- `qpi-client/go`: Added `QpiAddr` field to the `QpuRecord` struct.
- `qpi-ui`: Added `FindAndDeleteOne` and `FindOneByFilter` helpers to the database query layer (`internal/db/queries.go`) to support cleaner repository queries.
- `qpi-ui`: Added validation-tagged API DTO models (`QPUCreateRequest`, `QPUCreateResponse`, `QPUToggleResponse`, `DispatchPayload`, `JobResultUpdate`) under `internal/api/schema.go`.

### Changed

- `qpi-ui`: Integrated the centralized `schema` structs into all custom REST controllers and handlers inside the `api` package, replacing duplicate local private struct definitions.
- `qpi-driver`: [Breaking] Removed deprecated `-H`/`--host` and `-P`/`--port` options from CLI and `run_driver` in favor of `--qpi-addr` / `-a` (env: `QPI_ADDR`, default: `http://127.0.0.1:8090`).
- `qpi-client`: Updated Go/Python/JS client E2E test suites to use the `QPI_ACCESS_TOKEN` environment variable.
- `qpi-ui`: Refactored all HTTP REST handlers (`handleNotificationDismiss`, `handleTokenDelete`, `handleQPUConnect`, `handleQPUToggle`) to use database models and generic queries instead of raw `core.Record` objects.
- `qpi-ui`: Removed reflection from `internal/db/queries.go` by refactoring methods to accept a pre-allocated model destination interface, improving performance.
- `qpi-ui`: Separated access token lookup and status validation in `handleQPUConnect` to correctly return `401 Unauthorized` for invalid tokens and `403 Forbidden` for disabled QPUs.
- `qpi-ui/internal/dashboard`: Updated `App.tsx` quantum job submission callback to extract `id` instead of `job_id` from the backend response.

## [0.0.1] - 2026-06-14

### Added

- `qpi-ui`: Added `notifications` collection with admin-only CRUD, user visibility rules, broadcast/targeted targeting, time-window filtering, and per-user dismiss support via `POST /api/notifications/{id}/dismiss`.
- `e2e/verify.py`: Added the `test_notifications_crud` E2E test to verify broadcast/targeted visibility, time-window filtering, per-user dismiss, and admin-only CUD enforcement.
- `qpi-ui`: Added `enabled` boolean field to `qpus` collection to allow administrators to toggle QPU drivers on and off.
- `qpi-ui`: Added an update event hook on the `qpus` collection that cancels/stops dispatcher and listener goroutines (and sets status to `"offline"`) when `enabled` is set to `false`, and starts goroutines (and sets status to `"online"`) when `enabled` is set to `true`.
- `qpi-ui`: Enforced `enabled` check in the `/api/op/qpu/register` route to reject registration of disabled QPUs with a `403 Forbidden` response.
- `e2e/verify.py`: Added the `test_qpu_toggle_switch` E2E test to verify the QPU disabled/enabled lifecycle, goroutine lifecycle, and registration blocking.

- `qpi-ui`: Added authenticated CRUD rules and validation hooks for the `qpu_time_requests` collection, supporting user requests, admin approvals/rejections, automatic QPU seconds crediting, and handled request immutability.
- `qpi-ui`: Added authenticated CRUD rules and validation hooks for `time_slots` collection, implementing interval order, overlap checks, auto-population of owner, past booking/update/delete restrictions, and admin bypass capability.
- `qpi-ui`: Added admin-only `PATCH /api/admin/users/{id}` endpoint for superusers to update `qpu_seconds` and `api_tokens` on any user record.
- `qpi-client/py`: `QPIBackend.run()` now supports `parameter_values` kwarg for parameterized circuit execution, automatically binding parameters and forwarding ordered values to the API payload.
- `qpi-driver/tests`: Added `@pytest.mark.skipif` decorators to CLI and quantify tests so they gracefully skip when optional dependencies (`typer`, `quantify_scheduler`, `qblox_instruments`) are not installed.
- `Makefile`: Added granular `test-py-base`, `test-py-cli`, `test-py-aer`, and `test-py-quantify` targets for testing each `pyproject.toml` extra in isolation.
- `qpi-driver`: Added an abstract `process_result()` method to the `Executor` interface, letting executors handle their own data processing (e.g. state discrimination, IQ memory formatting) directly in the worker process.
- `qpi-driver`: Implemented state discrimination, average/single IQ memory formatting, and raw trace handling in `MockExecutor`, `QiskitAerExecutor`, and `QuantifyExecutor`.
- `qpi-driver`: Support for `ThresholdedAcquisition` protocol in `QuantifyExecutor` when threshold/rotation parameters are defined on device elements, automatically falling back to software discrimination via `SSBIntegrationComplex`.

### Changed

- `qpi-ui`: Default `qpu_seconds` for new users changed from `1000` to `0`. Users must now be granted QPU time explicitly by an admin via the `PATCH /api/admin/users/{id}` endpoint. The `OnRecordCreate` hook that previously set the default has been removed.
- `qpi-driver`: Renamed the `translator` process to `result sender` and simplified it to forward processed dicts via NNG PUSH directly from a queue, eliminating intermediate `.pkl` filesystem serialization overhead.



import importlib.metadata
import importlib.util
from unittest.mock import patch

import pytest

has_typer = importlib.util.find_spec("typer") is not None

pytestmark = pytest.mark.skipif(
    not has_typer, reason="typer must be installed to run CLI tests"
)

if has_typer:
    from qpi_driver.cli import app
    from typer.testing import CliRunner

    # TERM=dumb is what makes these assertions hold on CI as well as locally.
    # Typer forces Rich's colour on when GITHUB_ACTIONS is set, and Rich renders a
    # flag as several coloured runs — `--device` arrives as `-` then `-device` with
    # escape codes between — so a plain substring match finds neither the flag nor
    # even the `Usage:` at the start of a line. A dumb terminal turns the whole
    # colour system off, leaving the text Rich would have coloured.
    runner = CliRunner(env={"TERM": "dumb"})


def _output(result) -> str:
    output = result.stdout or ""
    try:
        if result.stderr:
            output += "\n" + result.stderr
    except ValueError:
        pass
    return output


def test_cli_version():
    """Verify that the version command outputs the correct package version."""
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    try:
        expected_version = importlib.metadata.version("qpi-driver")
    except importlib.metadata.PackageNotFoundError:
        expected_version = "0.4.0"
    assert expected_version in result.stdout


def test_every_command_builds():
    """Every command is constructible and takes no positional arguments.

    Typer builds the whole command tree up front, so a single option annotated
    with a type it cannot render takes down every command — `version` and bare
    `--help` included — and a `**kwargs` parameter silently becomes a required
    positional argument. Neither failure is visible to a test that exercises one
    command's happy path, so assert the tree itself here.
    """
    import typer.main

    typer.main.get_command(app)

    for command in ("start", "devices", "version"):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0, _output(result)
        # Rich pads its help output, so strip before matching.
        usage = next(
            line.strip()
            for line in result.stdout.splitlines()
            if line.strip().startswith("Usage:")
        )
        assert usage.endswith("[OPTIONS]"), usage


class Recorder:
    """A fake device that logs how the CLI used it, connecting to nothing."""

    def __init__(self):
        self.build_calls: list[dict] = []
        self.options: dict[str, str] = {}
        self.log: list[str] = []

    def build(self, **kwargs):
        options = kwargs.get("options")
        if options is not None:
            # A real device reads the keys it understands; this one takes whatever
            # it is given, so the CLI's unread-option check has nothing to report.
            self.options = options.remaining()
        self.build_calls.append(kwargs)
        self.log.append("build")
        return self

    def run(self) -> None:
        self.log.append("run")


def _fake_device(operation, name="fake", build=None):
    """Register *name* as the only device of *operation*, for the test's duration.

    Lets the CLI's own behaviour be asserted without a real device's dependencies
    or connection attempts in the way. Returns the patcher to enter and the
    recorder to assert on.
    """
    from qpi_driver.builtins import registry

    recorder = Recorder()
    spec = registry.DeviceSpec(
        name=name,
        operation=operation,
        build=build or recorder.build,
    )
    table = {op: {} for op in registry.Operation}
    table[operation][name] = spec
    return patch.dict(registry._DEVICES, table, clear=True), recorder


def test_operation_is_required():
    """`start` alone is not a command; the operation is what it needs to know."""
    result = runner.invoke(app, ["start", "--token", "t", "--ca-fingerprint", "fp"])

    assert result.exit_code == 2
    assert "--operation" in _output(result)


def test_an_unknown_operation_lists_the_valid_ones():
    """Operations are a closed set, so the error can name all of them (RFC 0003 §13.1)."""
    result = runner.invoke(
        app,
        ["start", "--operation", "procces", "--token", "t", "--ca-fingerprint", "fp"],
        env={"COLUMNS": "200"},
    )
    output = _output(result)

    assert result.exit_code == 2
    assert "'process'" in output and "'monitor'" in output


def test_the_operation_comes_from_the_environment_too():
    """Every universal flag has an env var, so a unit file can be all Environment=."""
    result = runner.invoke(
        app,
        ["start", "--device", "nope"],
        env={
            "QPI_OPERATION": "monitor",
            "QPI_ACCESS_TOKEN": "t",
            "QPI_CA_FINGERPRINT": "fp",
            "COLUMNS": "200",
        },
    )

    # It got as far as resolving a monitor device, which is the operation landing.
    assert "unknown monitor device" in _output(result)


def test_the_data_dir_comes_from_the_environment():
    """QPI_DATA_DIR is how the systemd unit points a driver at its own directory."""
    from pathlib import Path

    from qpi_driver.builtins import Operation

    patcher, recorder = _fake_device(Operation.MONITOR, name="fake")
    with patcher:
        result = runner.invoke(
            app,
            ["start", "--operation", "monitor", "--device", "fake"],
            env={
                "QPI_DATA_DIR": "/var/qpi-driver/cryostat-1",
                "QPI_ACCESS_TOKEN": "t",
                "QPI_CA_FINGERPRINT": "fp",
                "TERM": "dumb",
            },
        )

    assert result.exit_code == 0, _output(result)
    assert recorder.build_calls[0]["data_dir"] == Path("/var/qpi-driver/cryostat-1")


@pytest.mark.parametrize("operation", ["process", "monitor"])
def test_the_old_subcommands_are_gone(operation):
    """`qpi-driver process` must fail, so nobody quietly reinstates it.

    The grammar broke once, deliberately (RFC 0003 §11); a subcommand that still
    worked would make the migration optional and the docs wrong.
    """
    result = runner.invoke(app, [operation, "--token", "t", "--ca-fingerprint", "fp"])

    assert result.exit_code != 0


@pytest.mark.parametrize("operation", ["process", "monitor"])
def test_device_is_required(operation):
    """There is no default device, because the SDK has no fact to default it from.

    QPI-UI is where a driver is registered and where the command to run it is
    generated, and that command always names a device. A driver guessing one would
    be the driver deciding what it is (RFC 0003 §9).
    """
    result = runner.invoke(
        app,
        ["start", "--operation", operation, "--token", "t", "--ca-fingerprint", "fp"],
        env={"COLUMNS": "200"},
    )

    assert result.exit_code == 2
    assert "--device" in _output(result)


def test_start_builds_the_device_it_is_given():
    """The named device is the one built, and the builder gets no name at all."""
    from qpi_driver.builtins import Operation

    patcher, recorder = _fake_device(Operation.MONITOR, name="fake")
    with patcher:
        result = runner.invoke(
            app,
            [
                "start",
                "--operation",
                "monitor",
                "--device",
                "fake",
                "--token",
                "t",
                "--ca-fingerprint",
                "fp",
            ],
        )

    assert result.exit_code == 0, _output(result)
    assert recorder.log == ["build", "run"]
    assert "name" not in recorder.build_calls[0]


def test_start_has_no_name_flag():
    """A driver does not name itself, so --name/-n is gone rather than ignored.

    The label belongs to the admin who registered the driver in the dashboard, and
    the drivers/connect response hands it over. Accepting a flag that no longer
    reaches anything would be worse than rejecting it.
    """
    result = runner.invoke(
        app,
        ["start", "--operation", "monitor", "--token", "t", "--name", "cryostat-1"],
    )

    assert result.exit_code != 0
    assert "--name" in _output(result)


def test_cli_start_builds_then_runs():
    """The CLI builds a driver from the spec, then starts it — in that order.

    The builder returns an unstarted driver and the CLI calls ``run()`` on it, so
    a device never decides when to connect (RFC 0003 §7). The ``-o`` values arrive
    as the strings they were typed as: what they mean is the device's business.
    """
    from pathlib import Path

    from qpi_driver.builtins import Operation
    from qpi_driver.cli import _start

    patcher, recorder = _fake_device(Operation.MONITOR)
    with patcher:
        _start(
            Operation.MONITOR,
            device="fake",
            qpi_addr="http://qpi:8090",
            token="tok",
            ca_file=Path("./bin/qpi.ca.pem"),
            ca_fingerprint="fp",
            data_dir=Path("./bin/data"),
            options=["ticks=3"],
            recv_timeout_ms=250,
        )

    assert recorder.log == ["build", "run"]
    assert recorder.options == {"ticks": "3"}
    call = recorder.build_calls[0]
    assert {key: value for key, value in call.items() if key != "options"} == {
        "qpi_addr": "http://qpi:8090",
        "token": "tok",
        "ca_fingerprint": "fp",
        "ca_file_path": "bin/qpi.ca.pem",
        "data_dir": Path("./bin/data"),
        "recv_timeout_ms": 250,
    }


def test_cli_start_takes_no_registry():
    """The operation alone selects the device table; callers pass no registry.

    Removing that parameter is the point of the change — a caller that could
    supply its own table could route ``process`` at a monitor.
    """
    import inspect

    from qpi_driver.cli import _start

    parameters = list(inspect.signature(_start).parameters)
    assert parameters[0] == "operation"
    assert "registry" not in parameters


def test_cli_start_reports_a_builder_error():
    """A ValueError from a device's builder becomes a one-line CLI error."""
    from qpi_driver.builtins import Operation

    def refuse(**_):
        raise ValueError("fake needs a 'widget' option")

    patcher, _recorder = _fake_device(Operation.MONITOR, build=refuse)
    with patcher:
        result = runner.invoke(
            app,
            [
                "start",
                "--operation",
                "monitor",
                "--token",
                "t",
                "--ca-fingerprint",
                "fp",
                "--device",
                "fake",
            ],
        )

    assert result.exit_code == 1
    assert "Error: fake needs a 'widget' option" in _output(result)


def test_cli_reports_a_bad_import_path_in_one_line():
    """A device that will not import exits 1 with a sentence, not a traceback.

    An operator gets a traceback for a bug, not for a typo — and a traceback here
    would carry filesystem paths into the journal (RFC 0003 §10).
    """
    result = runner.invoke(
        app,
        [
            "start",
            "--operation",
            "process",
            "--token",
            "t",
            "--ca-fingerprint",
            "fp",
            "--device",
            "no_such_module:Thing",
        ],
    )
    output = _output(result)

    assert result.exit_code == 1
    assert "Error: could not import device 'no_such_module:Thing'" in output
    assert "Traceback" not in output


def test_cli_runs_a_device_named_by_import_path():
    """`--device mod:Cls` builds the built-in QPU driver over that executor.

    No registration, no packaging — the route for "I have a class in a file".
    """
    from pathlib import Path

    from qpi_driver.builtins import Operation
    from qpi_driver.builtins.qpu import QpuDriver
    from qpi_driver.cli import _start

    built: list[QpuDriver] = []

    with patch.object(QpuDriver, "run", lambda self: built.append(self)):
        _start(
            Operation.PROCESS,
            device="tests.test_discovery:FakeExecutor",
            qpi_addr="http://qpi:8090",
            token="tok",
            ca_file=Path("./bin/qpi.ca.pem"),
            ca_fingerprint="fp",
            data_dir=Path("./bin/data"),
            options=["probe_count=4"],
            recv_timeout_ms=200,
        )

    from tests.test_discovery import FakeExecutor

    assert len(built) == 1
    assert built[0].executor is FakeExecutor
    assert built[0].executor_options["probe_count"] == "4"


def test_help_points_at_the_dashboard_rather_than_describing_devices():
    """--help says where a device is chosen and configured; it is not a catalog.

    It used to render every device and every -o key from a declared schema, which
    was a second catalog beside QPI-UI's (RFC 0003 §9).
    """
    result = runner.invoke(app, ["start", "--help"], env={"COLUMNS": "200"})
    output = _output(result)

    assert result.exit_code == 0, output
    assert "QPI-UI" in output
    assert "qpi-driver devices" in output


def test_there_is_no_catalog_command():
    """The machine-readable catalog is gone; nothing consumed it but a sync script."""
    result = runner.invoke(app, ["catalog", "--json"])

    assert result.exit_code != 0


def test_version_falls_back_when_the_package_is_not_installed():
    """Run from a source tree with no installed distribution, `version` still works."""
    import importlib.metadata

    from qpi_driver.cli import _get_version

    with patch.object(
        importlib.metadata,
        "version",
        side_effect=importlib.metadata.PackageNotFoundError("qpi-driver"),
    ):
        assert _get_version() == "0.4.0"


def test_cli_process_requires_token():
    """process fails if the access token is not supplied."""
    result = runner.invoke(
        app,
        [
            "start",
            "--operation",
            "process",
            "--device",
            "mock",
            "--ca-fingerprint",
            "fp",
        ],
    )
    assert result.exit_code == 1
    assert "Error: access token is required" in _output(result)


def test_cli_process_rejects_unknown_device():
    """process rejects an executor device with no registered runner."""
    result = runner.invoke(
        app,
        [
            "start",
            "--operation",
            "process",
            "--token",
            "t",
            "--ca-fingerprint",
            "fp",
            "--device",
            "nope",
        ],
    )
    assert result.exit_code == 1
    assert "unknown process device" in _output(result)


def test_cli_process_unsafe_ca_file():
    """process fails if the ca-file is in an unsafe location."""
    result = runner.invoke(
        app,
        [
            "start",
            "--operation",
            "process",
            "--token",
            "t",
            "--ca-fingerprint",
            "fp",
            "--device",
            "mock",
            "--ca-file",
            "/",
        ],
    )
    assert result.exit_code in (1, 2)


def test_cli_process_unsafe_data_dir_option():
    """A process data_dir option in an unsafe location is rejected."""
    result = runner.invoke(
        app,
        [
            "start",
            "--operation",
            "process",
            "--device",
            "mock",
            "--token",
            "t",
            "--ca-fingerprint",
            "fp",
            "-o",
            "data_dir=/var",
        ],
    )
    assert result.exit_code == 1
    assert "safe location" in _output(result)


def test_cli_monitor_requires_token():
    """monitor fails without an access token, like process."""
    result = runner.invoke(
        app,
        [
            "start",
            "--operation",
            "monitor",
            "--device",
            "bluefors_gen1",
            "--ca-fingerprint",
            "fp",
        ],
    )
    assert result.exit_code == 1


def test_cli_monitor_rejects_unknown_device():
    """monitor rejects a device that no runner is registered for."""
    result = runner.invoke(
        app,
        [
            "start",
            "--operation",
            "monitor",
            "--token",
            "t",
            "--ca-fingerprint",
            "fp",
            "--device",
            "nope",
        ],
    )
    assert result.exit_code == 1
    assert "unknown monitor device" in _output(result)


def test_cli_monitor_reports_missing_required_option():
    """A device's own option validation surfaces as a clean CLI error."""
    result = runner.invoke(
        app,
        [
            "start",
            "--operation",
            "monitor",
            "--token",
            "t",
            "--ca-fingerprint",
            "fp",
            "--device",
            "bluefors_gen1",
        ],
    )
    assert result.exit_code == 1
    assert "channels" in _output(result)


def test_split_options_reads_the_syntax_only():
    """-o key=value pairs split into raw strings; a pair without '=' is rejected.

    Splitting is all the CLI does — which keys mean anything and what type each
    value has is the chosen device's business, so an unknown key passes through
    here untouched.
    """
    from qpi_driver.cli import _split_options

    assert _split_options(["base_url=http://x", "channels=a:K,b", "nonsense=1"]) == {
        "base_url": "http://x",
        "channels": "a:K,b",
        "nonsense": "1",
    }
    with pytest.raises(ValueError):
        _split_options(["not-a-pair"])


def test_cli_rejects_an_unknown_option():
    """A misspelt -o key is an error, not a silent no-op.

    With no declared schema, the check is what the device read: `mock` builds
    fine and never looks at `data_dirr`, so the leftover key is the whole
    evidence. Silently ignoring it — which the executors' `**kwargs` would —
    means a typo in a systemd unit runs a driver with a default nobody chose.
    """
    result = runner.invoke(
        app,
        [
            "start",
            "--operation",
            "process",
            "--device",
            "mock",
            "--token",
            "t",
            "--ca-fingerprint",
            "fp",
            "-o",
            "data_dirr=/tmp/x",
        ],
    )

    output = _output(result)
    assert result.exit_code == 1
    assert "unknown option 'data_dirr' for process device 'mock'" in output


def test_cli_rejects_an_uncoercible_option_value():
    """A value the device cannot read names the option, not the parser's internals."""
    result = runner.invoke(
        app,
        [
            "start",
            "--operation",
            "process",
            "--device",
            "mock",
            "--token",
            "t",
            "--ca-fingerprint",
            "fp",
            "-o",
            "job_timeout=soon",
        ],
    )

    assert result.exit_code == 1
    assert "bad value for -o job_timeout" in _output(result)


def test_devices_command_lists_what_this_install_can_run():
    """Names only: what a device does is documented in QPI-UI, not here."""
    result = runner.invoke(app, ["devices"])
    output = _output(result)

    assert result.exit_code == 0, output
    assert "process" in output and "monitor" in output
    assert "mock" in output and "bluefors_gen1" in output


def test_devices_command_narrows_to_one_operation():
    result = runner.invoke(app, ["devices", "--operation", "monitor"])
    output = _output(result)

    assert result.exit_code == 0, output
    assert "bluefors_gen1" in output
    assert "mock" not in output


def test_devices_command_says_none_rather_than_nothing():
    """An SDK with no device for an operation must say so (RFC 0003 §8)."""
    from qpi_driver.builtins import Operation, registry

    table = {op: {} for op in registry.Operation}
    table[Operation.MONITOR]["bluefors_gen1"] = registry.resolve(
        Operation.MONITOR, "bluefors_gen1"
    )

    with patch.dict(registry._DEVICES, table, clear=True):
        result = runner.invoke(app, ["devices"])

    assert "process: none" in _output(result)


def test_validate_safe_path():
    """Directly test the validate_safe_path logic for safe and unsafe paths."""
    from pathlib import Path

    import typer
    from qpi_driver.cli import _validate_safe_path

    # Safe paths should not raise typer.Exit
    _validate_safe_path(Path("./bin/data"), "test-dir")
    _validate_safe_path(Path("/tmp/safe-dir"), "test-dir")
    _validate_safe_path(Path("/var/tmp/safe-dir"), "test-dir")
    _validate_safe_path(Path("/var/qpi-driver"), "test-dir")
    _validate_safe_path(Path("/var/qpi-driver/sub"), "test-dir")
    _validate_safe_path(Path("/etc/qpi-driver/sub"), "test-dir")
    _validate_safe_path(Path.home() / "qpi-driver", "test-dir")

    # Root path should raise typer.Exit(code=1)
    with pytest.raises(typer.Exit) as exc:
        _validate_safe_path(Path("/"), "test-dir")
    assert exc.value.exit_code == 1

    # Forbidden system path should raise typer.Exit(code=1)
    with pytest.raises(typer.Exit) as exc:
        _validate_safe_path(Path("/usr/local/bin"), "test-dir")
    assert exc.value.exit_code == 1

    # System roots that are blocked
    with pytest.raises(typer.Exit) as exc:
        _validate_safe_path(Path("/var"), "test-dir")
    assert exc.value.exit_code == 1

    with pytest.raises(typer.Exit) as exc:
        _validate_safe_path(Path("/home"), "test-dir")
    assert exc.value.exit_code == 1

    with pytest.raises(typer.Exit) as exc:
        _validate_safe_path(Path("/Users"), "test-dir")
    assert exc.value.exit_code == 1

    # Home root itself is blocked
    with pytest.raises(typer.Exit) as exc:
        _validate_safe_path(Path.home(), "test-dir")
    assert exc.value.exit_code == 1

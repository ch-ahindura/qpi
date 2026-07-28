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

    runner = CliRunner()


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
        expected_version = "0.1.2"
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

    for command in ("start", "devices", "catalog", "version"):
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
        self.log: list[str] = []

    def build(self, **kwargs):
        self.build_calls.append(kwargs)
        self.log.append("build")
        return self

    def run(self) -> None:
        self.log.append("run")


def _fake_device(operation, name="fake", build=None, options=None):
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
        options=options if options is not None else (),
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


@pytest.mark.parametrize("operation", ["process", "monitor"])
def test_the_old_subcommands_are_gone(operation):
    """`qpi-driver process` must fail, so nobody quietly reinstates it.

    The grammar broke once, deliberately (RFC 0003 §11); a subcommand that still
    worked would make the migration optional and the docs wrong.
    """
    result = runner.invoke(app, [operation, "--token", "t", "--ca-fingerprint", "fp"])

    assert result.exit_code != 0


@pytest.mark.parametrize(
    ("operation", "device"),
    [("process", "mock"), ("monitor", "bluefors_gen1")],
)
def test_device_defaults_per_operation(operation, device):
    """One verb, but the default device still differs by operation, from OperationSpec.

    Asserted through the CLI rather than on the table, since the point is that the
    resolution happens when `--device` is omitted.
    """
    from qpi_driver.builtins import Operation

    # Registered under the name the operation defaults to, so the driver is only
    # built at all if that default was resolved.
    patcher, recorder = _fake_device(Operation(operation), name=device)
    with patcher:
        result = runner.invoke(
            app,
            [
                "start",
                "--operation",
                operation,
                "--token",
                "t",
                "--ca-fingerprint",
                "fp",
            ],
        )

    assert result.exit_code == 0, _output(result)
    assert recorder.log == ["build", "run"]
    # The builder is handed no name at all: there is nothing to default.
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
    a device never decides when to connect (RFC 0003 §7). The options it is handed
    have been through the device's own schema, so ``ticks`` arrives as an int.
    """
    from pathlib import Path

    from qpi_driver.builtins import Operation, OptionSpec
    from qpi_driver.cli import _start

    patcher, recorder = _fake_device(
        Operation.MONITOR,
        options=(
            OptionSpec(key="ticks", help="How many.", parse=int),
            OptionSpec(key="label", help="What to call it.", default="unnamed"),
        ),
    )
    with patcher:
        _start(
            Operation.MONITOR,
            device="fake",
            qpi_addr="http://qpi:8090",
            token="tok",
            ca_file=Path("./bin/qpi.ca.pem"),
            ca_fingerprint="fp",
            options=["ticks=3"],
            recv_timeout_ms=250,
        )

    assert recorder.log == ["build", "run"]
    assert recorder.build_calls == [
        {
            "options": {"ticks": 3, "label": "unnamed"},
            "qpi_addr": "http://qpi:8090",
            "token": "tok",
            "ca_fingerprint": "fp",
            "ca_file_path": "bin/qpi.ca.pem",
            "recv_timeout_ms": 250,
        }
    ]


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
            options=["probe_count=4"],
            recv_timeout_ms=200,
        )

    from tests.test_discovery import FakeExecutor

    assert len(built) == 1
    assert built[0].executor is FakeExecutor
    assert built[0].executor_options["probe_count"] == "4"


def test_help_documents_the_import_path_route():
    """--help says the catalog is not the limit, and that -o is unchecked there."""
    result = runner.invoke(app, ["start", "--help"], env={"COLUMNS": "200"})
    output = _output(result)

    assert "named by import path" in output
    assert "unchecked" in output


def test_catalog_command_can_print_the_text_view():
    """`catalog --text` is the same renderer `devices` uses, for a quick look."""
    result = runner.invoke(app, ["catalog", "--text"])

    assert result.exit_code == 0, _output(result)
    assert _output(result) == _output(runner.invoke(app, ["devices"]))


def test_version_falls_back_when_the_package_is_not_installed():
    """Run from a source tree with no installed distribution, `version` still works."""
    import importlib.metadata

    from qpi_driver.cli import _get_version

    with patch.object(
        importlib.metadata,
        "version",
        side_effect=importlib.metadata.PackageNotFoundError("qpi-driver"),
    ):
        assert _get_version() == "0.1.2"


def test_cli_process_requires_token():
    """process fails if the access token is not supplied."""
    result = runner.invoke(
        app, ["start", "--operation", "process", "--ca-fingerprint", "fp"]
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
        app, ["start", "--operation", "monitor", "--ca-fingerprint", "fp"]
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

    Splitting is all the CLI does — which keys exist and what type each value has
    is the chosen device's schema's business, not this function's, so an unknown
    key passes through here untouched.
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
    """A misspelt -o key is an error naming the valid ones, not a silent no-op.

    Silently ignoring it is the behaviour this replaced: a typo in a systemd unit
    used to mean a driver running with a default nobody chose.
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
            "-o",
            "data_dirr=/tmp/x",
        ],
    )

    output = _output(result)
    assert result.exit_code == 1
    assert "unknown option 'data_dirr'" in output
    assert "data_dir" in output  # the valid keys are listed


def test_cli_rejects_an_uncoercible_option_value():
    """A value its parser refuses names the option, not the parser's internals."""
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
            "-o",
            "job_timeout=soon",
        ],
    )

    assert result.exit_code == 1
    assert "bad value for -o job_timeout" in _output(result)


def test_help_lists_every_operation_device_and_option():
    """--help answers "what can I pass?" from the registry, not by hand.

    One verb serves every operation, so one --help covers all of them. Rich
    rewraps the epilog, so match on substrings rather than whole lines.
    """
    from qpi_driver.builtins import Operation, devices

    result = runner.invoke(app, ["start", "--help"], env={"COLUMNS": "200"})
    output = _output(result)

    assert result.exit_code == 0, output
    for operation in Operation:
        assert f"--operation {operation.value}" in output
        for spec in devices(operation):
            assert spec.name in output
            for option in spec.options:
                assert f"{option.key}=<{option.type_name}>" in output


def test_help_marks_a_required_option():
    """A device with a required option says so, so it is not learnt by crashing."""
    result = runner.invoke(app, ["start", "--help"], env={"COLUMNS": "200"})

    assert "channels=<channels> (required" in _output(result)


def test_help_survives_a_device_it_cannot_describe():
    """One unusable device must not take --help down for the others.

    An installed device whose module half-imports is the case this guards: help
    is the command someone runs *because* something is wrong, so it has to work
    when a device does not.
    """
    from qpi_driver.builtins import Operation, registry
    from qpi_driver.cli import _epilog

    class Unimportable:
        """A registered device whose description cannot be read."""

        name = "broken"

        def __getattr__(self, attribute):
            raise ImportError("no module named 'vendor_sdk'")

    table = {op: {} for op in registry.Operation}
    table[Operation.MONITOR]["broken"] = Unimportable()
    table[Operation.MONITOR]["bluefors_gen1"] = registry.resolve(
        Operation.MONITOR, "bluefors_gen1"
    )

    with patch.dict(registry._DEVICES, table, clear=True):
        epilog = _epilog(Operation.MONITOR)

    assert "broken — cannot be described: ImportError" in epilog
    assert "bluefors_gen1" in epilog


def test_devices_command_lists_every_operation():
    """`devices` is the readable view of the same catalog --help generates."""
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


def test_catalog_command_round_trips_as_json():
    """`catalog --json` is what QPI-UI and the other SDKs read (RFC 0003 §9)."""
    import json

    from qpi_driver.builtins import Operation, devices

    result = runner.invoke(app, ["catalog", "--json"])
    assert result.exit_code == 0, _output(result)

    document = json.loads(result.stdout)
    assert document["schema_version"] == 1

    operations = {entry["name"]: entry for entry in document["operations"]}
    assert set(operations) == {"process", "monitor"}

    for name, entry in operations.items():
        listed = {device["name"] for device in entry["devices"]}
        assert listed == {spec.name for spec in devices(Operation(name))}

    qblox = next(
        device
        for device in operations["process"]["devices"]
        if device["name"] == "qblox"
    )
    assert qblox["extra"] == "qpi-driver[cli,qblox]"
    assert {option["key"] for option in qblox["options"]} >= {"data_dir", "job_timeout"}


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

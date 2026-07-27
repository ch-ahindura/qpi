import importlib.metadata
import importlib.util

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

    for command in ("process", "monitor", "version"):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0, _output(result)
        # Rich pads its help output, so strip before matching.
        usage = next(
            line.strip()
            for line in result.stdout.splitlines()
            if line.strip().startswith("Usage:")
        )
        assert usage.endswith("[OPTIONS]"), usage


def test_cli_process_requires_token():
    """process fails if the access token is not supplied."""
    result = runner.invoke(app, ["process", "--ca-fingerprint", "fp"])
    assert result.exit_code == 1
    assert "Error: access token is required" in _output(result)


def test_cli_process_rejects_unknown_device():
    """process rejects an executor device with no registered runner."""
    result = runner.invoke(
        app,
        ["process", "--token", "t", "--ca-fingerprint", "fp", "--device", "nope"],
    )
    assert result.exit_code == 1
    assert "unknown process device" in _output(result)


def test_cli_process_unsafe_ca_file():
    """process fails if the ca-file is in an unsafe location."""
    result = runner.invoke(
        app,
        [
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
    result = runner.invoke(app, ["monitor", "--ca-fingerprint", "fp"])
    assert result.exit_code == 1


def test_cli_monitor_rejects_unknown_device():
    """monitor rejects a device that no runner is registered for."""
    result = runner.invoke(
        app,
        ["monitor", "--token", "t", "--ca-fingerprint", "fp", "--device", "nope"],
    )
    assert result.exit_code == 1
    assert "unknown monitor device" in _output(result)


def test_cli_monitor_reports_missing_required_option():
    """A device's own option validation surfaces as a clean CLI error."""
    result = runner.invoke(
        app,
        [
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


def test_parse_options_parses_and_validates():
    """-o key=value pairs parse into a dict; a pair without '=' is rejected."""
    from qpi_driver.cli import _parse_options

    assert _parse_options(["base_url=http://x", "channels=a:K,b"]) == {
        "base_url": "http://x",
        "channels": "a:K,b",
    }
    with pytest.raises(ValueError):
        _parse_options(["not-a-pair"])


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

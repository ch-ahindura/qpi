import importlib.metadata
from pathlib import Path
from typing import Annotated, Callable

from pydantic import ImportString

from qpi_driver.builtins import MONITOR_DRIVERS, PROCESS_DRIVERS, DriverRunner
from qpi_driver.compat import typer
from qpi_driver.paths import validate_safe_path
from qpi_driver.sdk import DEFAULT_RECV_TIMEOUT_MS

app = None
if typer.IS_TYPER_INSTALLED:
    app = typer.Typer(help="Quantum Processing Interface (QPI) Driver CLI")

    # Universal options shared by every operation subcommand, defined once so a
    # new operation reuses them rather than redeclaring their flags/env/help. An
    # operation's own settings go through --option / -o instead (RFC 0001 §4).
    QpiAddrOpt = Annotated[
        str,
        typer.Option(
            "--qpi-addr",
            "-a",
            envvar="QPI_ADDR",
            help="Full URL of the QPI server (e.g. http://localhost:8090 or https://qpi.example.com)",
        ),
    ]
    TokenOpt = Annotated[
        str,
        typer.Option(
            "--token",
            "-t",
            envvar="QPI_ACCESS_TOKEN",
            help="Access token identifying this driver to the QPI server",
        ),
    ]
    NameOpt = Annotated[
        str,
        typer.Option(
            "--name",
            "-n",
            envvar="QPI_DRIVER_NAME",
            help="Human-readable name for this driver",
        ),
    ]
    DeviceOpt = Annotated[
        str,
        typer.Option(
            "--device",
            "-d",
            envvar="QPI_DEVICE",
            help="Which backend to run within the operation (e.g. mock, qblox, bluefors_gen1)",
        ),
    ]
    CaFileOpt = Annotated[
        Path,
        typer.Option(
            envvar="QPI_CA_FILE",
            help="Where the downloaded server root CA certificate is written.",
            writable=True,
            readable=True,
            dir_okay=False,
            file_okay=True,
            resolve_path=True,
        ),
    ]
    OptionsOpt = Annotated[
        list[str] | None,
        typer.Option(
            "--option",
            "-o",
            help="Operation-specific config as key=value, repeatable — e.g. "
            "-o data_dir=./bin/data (process) or "
            "-o channels=mapper.bf.tmc:K,mapper.bf.pmc:mbar (monitor). "
            '-o executors={"quantify":"quantify_driver.CustomExecutor","qblox":"qblox_driver.CustomExecutor"}'
            "See the chosen device for the keys it reads.",
        ),
    ]
    RecvTimeoutOpt = Annotated[
        int,
        typer.Option(
            envvar="QPI_RECV_TIMEOUT_MS",
            help="How long the receive loop blocks per attempt before checking "
            "for a shutdown signal, in milliseconds.",
        ),
    ]
    RunnerOpt = Annotated[
        ImportString | None,
        typer.Option(
            "--runner",
            "-r",
            envvar="QPI_OPERATION_RUNNER",
            help="The custom import path to the callable to run this driver",
        ),
    ]

    def _ca_fingerprint_option() -> typer.Option:
        return typer.Option(
            default=...,
            envvar="QPI_CA_FINGERPRINT",
            help="SHA-256 fingerprint pinning the automatically downloaded root CA of the QPI server.",
        )

    @app.command()
    def process(
        device: DeviceOpt = "mock",
        qpi_addr: QpiAddrOpt = "http://127.0.0.1:8090",
        token: TokenOpt = "",
        name: NameOpt = "qpu_sim_01",
        ca_file: CaFileOpt = Path("./bin/qpi.ca.pem"),
        ca_fingerprint: str = _ca_fingerprint_option(),
        options: OptionsOpt = None,
        recv_timeout_ms: RecvTimeoutOpt = DEFAULT_RECV_TIMEOUT_MS,
        runner: RunnerOpt = None,
        **kwargs,
    ):
        """
        Run a process driver — a QPU that executes jobs pushed to it (RFC 0001 §4).

        Executor runtime settings are passed as -o options: data_dir, is_dummy,
        job_timeout, quantify_hardware_config, quantify_device_config.
        """
        _run_operation(
            PROCESS_DRIVERS,
            "process",
            device=device,
            qpi_addr=qpi_addr,
            token=token,
            name=name,
            ca_file=ca_file,
            ca_fingerprint=ca_fingerprint,
            options=options,
            recv_timeout_ms=recv_timeout_ms,
            runner=runner,
            **kwargs,
        )

    @app.command()
    def monitor(
        device: DeviceOpt = "bluefors_gen1",
        qpi_addr: QpiAddrOpt = "http://127.0.0.1:8090",
        token: TokenOpt = "",
        name: NameOpt = "qpi-monitor",
        ca_file: CaFileOpt = Path("./bin/qpi.ca.pem"),
        ca_fingerprint: str = _ca_fingerprint_option(),
        options: OptionsOpt = None,
        recv_timeout_ms: RecvTimeoutOpt = DEFAULT_RECV_TIMEOUT_MS,
        **kwargs,
    ):
        """
        Run a monitor driver — one that only reports upward on its own schedule
        and never handles JobDispatch (RFC 0001 §4, §7).

        The device's settings are passed as -o options, e.g. for bluefors_gen1:
        -o base_url=... -o channels=path:unit,... -o api_key=...
        """
        _run_operation(
            MONITOR_DRIVERS,
            "monitor",
            device=device,
            qpi_addr=qpi_addr,
            token=token,
            name=name,
            ca_file=ca_file,
            ca_fingerprint=ca_fingerprint,
            options=options,
            recv_timeout_ms=recv_timeout_ms,
            **kwargs,
        )

    def _run_operation(
        registry: dict[str, DriverRunner],
        operation: str,
        *,
        device: str,
        qpi_addr: str,
        token: str,
        name: str,
        ca_file: Path,
        ca_fingerprint: str,
        options: list[str] | None,
        recv_timeout_ms: int,
        runner: Callable[..., None] | None,
        **kwargs,
    ) -> None:
        """Look up a device's runner in *registry* and run it, or exit with an error.

        Shared by every operation subcommand: the operation is the command name,
        the device selects the backend, and the runner reads its own config from
        the parsed -o options.
        """
        if not token:
            typer.echo(
                "Error: access token is required. "
                "Set it via --token / -t or the QPI_ACCESS_TOKEN environment variable.",
                err=True,
            )
            raise typer.Exit(code=1)

        runner = runner or registry.get(device)
        if runner is None:
            typer.echo(
                f"Error: unknown {operation} device {device!r}. "
                f"Known devices: {', '.join(sorted(registry))}.",
                err=True,
            )
            raise typer.Exit(code=1)

        _validate_safe_path(ca_file, "--ca-file")
        typer.rich_print(_banner())

        try:
            runner(
                device=device,
                options=_parse_options(options or []),
                qpi_addr=qpi_addr,
                token=token,
                name=name,
                ca_fingerprint=ca_fingerprint,
                ca_file_path=ca_file.as_posix(),
                recv_timeout_ms=recv_timeout_ms,
                **kwargs,
            )
        except ValueError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(code=1)

    @app.command()
    def version():
        """
        Show the version of the QPI driver.
        """
        typer.echo(_get_version())

    def _get_version() -> str:
        try:
            return importlib.metadata.version("qpi-driver")
        except importlib.metadata.PackageNotFoundError:
            return "0.1.2"

    def _banner():
        """Renders the banner at the top of the CLI"""
        text = (
            "[bold bright_cyan]  ██████╗ ██████╗ ██╗  [/bold bright_cyan]\n"
            "[bold bright_cyan] ██╔═══██╗██╔══██╗██║  [/bold bright_cyan]\n"
            "[bold bright_cyan] ██║   ██║██████╔╝██║  [/bold bright_cyan]\n"
            "[bold bright_cyan] ██║▄▄ ██║██╔═══╝ ██║  [/bold bright_cyan]\n"
            "[bold bright_cyan] ╚██████╔╝██║     ██║  [/bold bright_cyan]\n"
            "[bold bright_cyan]  ╚══▀▀═╝ ╚═╝     ╚═╝  [/bold bright_cyan]\n"
            "\n"
            "  [dim]Quantum Processing Interface[/dim]\n"
            f"  [dim]Driver[/dim]  [bold]{_get_version()}[/bold]\n"
            "\n"
            "  [link=https://github.com/sopherapps/qpi]github.com/sopherapps/qpi[/link]"
        )
        return typer.Panel(
            text,
            border_style="bright_cyan",
            padding=(1, 2),
        )

    def _parse_options(pairs: list[str]) -> dict[str, str]:
        """Turn repeatable ``-o key=value`` options into a dict.

        Each device reads the keys it cares about from the result, so the CLI
        stays generic across operations and devices.
        """
        options: dict[str, str] = {}
        for pair in pairs:
            key, sep, value = pair.partition("=")
            if not sep or not key.strip():
                raise ValueError(f"invalid option {pair!r}; expected key=value")
            options[key.strip()] = value.strip()
        return options

    def _validate_safe_path(path: Path, name: str) -> None:
        """CLI wrapper over validate_safe_path that exits instead of raising."""
        try:
            validate_safe_path(path, name)
        except ValueError as exc:
            typer.echo(f"Error: {exc}.", err=True)
            raise typer.Exit(code=1)

    if __name__ == "__main__":
        app()

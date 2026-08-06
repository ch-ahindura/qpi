import importlib.metadata
from pathlib import Path
from typing import Annotated

from qpi_driver.builtins import Operation, resolve_device
from qpi_driver.builtins import devices as registered_devices
from qpi_driver.compat import typer
from qpi_driver.options import Options
from qpi_driver.paths import validate_safe_path
from qpi_driver.sdk import DEFAULT_RECV_TIMEOUT_MS

app = None
if typer.IS_TYPER_INSTALLED:
    app = typer.Typer(
        help="Quantum Processing Interface (QPI) Driver CLI",
        rich_markup_mode="rich",
    )

    # Universal options `start` shares across every operation, defined once so a
    # new operation reuses them rather than redeclaring their flags/env/help. A
    # device's own settings go through --option / -o instead (RFC 0001 §4).
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
    # No short form for --operation: -o is --option, and -O beside it would be a
    # hazard on a command line that is usually written once into a unit file
    # (RFC 0003 §13.7).
    OperationOpt = Annotated[
        Operation,
        typer.Option(
            "--operation",
            envvar="QPI_OPERATION",
            help="What this driver does: run jobs pushed to it, report upward, "
            "or calibrate a chip.",
        ),
    ]
    DeviceOpt = Annotated[
        str,
        typer.Option(
            "--device",
            "-d",
            envvar="QPI_DEVICE",
            help="Which backend to run within the operation (e.g. mock, qblox, "
            "bluefors_gen1), or an import path. `qpi-driver devices` lists the ones "
            "this install has.",
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
    # Universal rather than each device's `-o data_dir`: a node has one answer for it.
    # `-o data_dir=` still wins, for unit files written before this flag.
    DataDirOpt = Annotated[
        Path,
        typer.Option(
            "--data-dir",
            envvar="QPI_DATA_DIR",
            help="Directory this driver writes its data under, and where the "
            "installer puts its config files.",
        ),
    ]
    OptionsOpt = Annotated[
        list[str] | None,
        typer.Option(
            "--option",
            "-o",
            help="A setting of the chosen device, as key=value, repeatable — "
            "e.g. -o data_dir=./bin/data. Which keys a device reads is the "
            "device's own business; QPI-UI's registration form is where they "
            "are documented and filled in.",
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

    def _ca_fingerprint_option():
        return typer.Option(
            default=...,
            envvar="QPI_CA_FINGERPRINT",
            help="SHA-256 fingerprint pinning the automatically downloaded root CA of the QPI server.",
        )

    @app.command()
    def start(
        operation: OperationOpt,
        device: DeviceOpt,
        qpi_addr: QpiAddrOpt = "http://127.0.0.1:8090",
        token: TokenOpt = "",
        ca_file: CaFileOpt = Path("./bin/qpi.ca.pem"),
        ca_fingerprint: str = _ca_fingerprint_option(),
        data_dir: DataDirOpt = Path("./bin/data"),
        options: OptionsOpt = None,
        recv_timeout_ms: RecvTimeoutOpt = DEFAULT_RECV_TIMEOUT_MS,
    ):
        """
        Run a driver: one --operation, on one --device within it (RFC 0001 §4).

        One verb rather than a subcommand per operation, because everything about
        launching a driver is the same whichever operation it is — and because a
        third party can add a device, but only QPI-UI can add an operation.

        QPI-UI is where a driver is registered and where the command to run it —
        this operation, this device, these -o options — is generated. Run
        `qpi-driver devices` to see which devices this install has.
        """
        _start(
            operation,
            device=device,
            qpi_addr=qpi_addr,
            token=token,
            ca_file=ca_file,
            ca_fingerprint=ca_fingerprint,
            data_dir=data_dir,
            options=options,
            recv_timeout_ms=recv_timeout_ms,
        )

    def _start(
        operation: Operation,
        *,
        device: str,
        qpi_addr: str,
        token: str,
        ca_file: Path,
        ca_fingerprint: str,
        data_dir: Path,
        options: list[str] | None,
        recv_timeout_ms: int,
    ) -> None:
        """Build the driver for *device* within *operation* and run it.

        Shared by every operation: the device selects the backend and its builder
        reads the -o options it understands, returning the unstarted driver that
        gets started here. Anything rejected along the way — an unknown device, a
        missing or unreadable value — surfaces as a ``ValueError`` and becomes a
        one-line CLI error rather than a traceback.

        An option no device read is reported after the build rather than checked
        against a declared list beforehand: the device's own code is the only
        description of what it accepts.

        There is no name to resolve: a driver's display label belongs to the admin
        who registered it in the dashboard, and the ``drivers/connect`` response
        hands it over.
        """
        if not token:
            typer.echo(
                "Error: access token is required. "
                "Set it via --token / -t or the QPI_ACCESS_TOKEN environment variable.",
                err=True,
            )
            raise typer.Exit(code=1)

        # Usage errors, reported before the banner so they are the first thing on
        # screen: which device, and whether the -o syntax parses at all.
        try:
            spec = resolve_device(operation, device)
            parsed = Options(_split_options(options or []))
        except ValueError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(code=1)

        _validate_safe_path(ca_file, "--ca-file")
        typer.rich_print(_banner())

        try:
            driver = spec.build(
                options=parsed,
                qpi_addr=qpi_addr,
                token=token,
                ca_fingerprint=ca_fingerprint,
                ca_file_path=ca_file.as_posix(),
                data_dir=data_dir,
                recv_timeout_ms=recv_timeout_ms,
            )
        except ValueError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(code=1)

        unread = parsed.unread()
        if unread:
            label = "options" if len(unread) > 1 else "option"
            typer.echo(
                f"Error: unknown {label} {', '.join(repr(k) for k in unread)} for "
                f"{operation.value} device {device!r}.",
                err=True,
            )
            raise typer.Exit(code=1)

        driver.run()

    @app.command()
    def devices(
        operation: Annotated[
            Operation | None,
            typer.Option(
                "--operation",
                help="Show only this operation's devices, instead of all of them.",
            ),
        ] = None,
    ):
        """
        List the devices this install can run, by operation.

        Names only, and only what is actually registered here — which is the useful
        question after installing a distribution of devices, or writing one. What a
        device does and which -o options it takes is documented where a driver is
        registered, in QPI-UI.
        """
        wanted = list(Operation) if operation is None else [operation]
        for op in wanted:
            names = [spec.name for spec in registered_devices(op)]
            typer.echo(f"{op.value}: {', '.join(names) or 'none'}")

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
            return "0.3.1"

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

    def _split_options(pairs: list[str]) -> dict[str, str]:
        """Turn repeatable ``-o key=value`` options into a dict of raw strings.

        Only the syntax is the CLI's business; what the keys mean and what type
        each value is belong to the device that reads them.
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

import importlib.metadata
import json
from pathlib import Path
from typing import Annotated

from qpi_driver.builtins import Operation, resolve_device
from qpi_driver.builtins.catalog import catalog_dict, device_lines, render_catalog
from qpi_driver.compat import typer
from qpi_driver.paths import validate_safe_path
from qpi_driver.sdk import DEFAULT_RECV_TIMEOUT_MS

app = None
if typer.IS_TYPER_INSTALLED:
    # Rich mode explicitly, because _epilog() is written for how it renders:
    # blank line for a line break, and square brackets escaped.
    app = typer.Typer(
        help="Quantum Processing Interface (QPI) Driver CLI",
        rich_markup_mode="rich",
    )

    def _epilog(operation: Operation) -> str:
        """Render the device catalog as an epilog for *operation*'s command.

        Built once, at import, from the registry — so a new device shows up in
        ``--help`` with no CLI change at all. Written for Typer's rich renderer,
        which joins the lines of a paragraph with spaces and keeps a break only
        where it finds a blank line, and which reads ``[...]`` as markup
        (``typer.rich_utils.rich_format_help``): hence one paragraph per line,
        and escaped brackets so ``qpi-driver[cli,qblox]`` survives.
        """
        return "\n\n".join(line.replace("[", r"\[") for line in device_lines(operation))

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
            help="A setting of the chosen device, as key=value, repeatable — "
            "e.g. -o data_dir=./bin/data. The keys each device reads are "
            "listed below, and by `qpi-driver devices`.",
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

    @app.command(epilog=_epilog(Operation.PROCESS))
    def process(
        device: DeviceOpt = "mock",
        qpi_addr: QpiAddrOpt = "http://127.0.0.1:8090",
        token: TokenOpt = "",
        name: NameOpt = "qpu_sim_01",
        ca_file: CaFileOpt = Path("./bin/qpi.ca.pem"),
        ca_fingerprint: str = _ca_fingerprint_option(),
        options: OptionsOpt = None,
        recv_timeout_ms: RecvTimeoutOpt = DEFAULT_RECV_TIMEOUT_MS,
    ):
        """
        Run a process driver — a QPU that executes jobs pushed to it (RFC 0001 §4).

        The devices it can run and the -o options each one reads are listed below.
        """
        _start(
            Operation.PROCESS,
            device=device,
            qpi_addr=qpi_addr,
            token=token,
            name=name,
            ca_file=ca_file,
            ca_fingerprint=ca_fingerprint,
            options=options,
            recv_timeout_ms=recv_timeout_ms,
        )

    @app.command(epilog=_epilog(Operation.MONITOR))
    def monitor(
        device: DeviceOpt = "bluefors_gen1",
        qpi_addr: QpiAddrOpt = "http://127.0.0.1:8090",
        token: TokenOpt = "",
        name: NameOpt = "qpi-monitor",
        ca_file: CaFileOpt = Path("./bin/qpi.ca.pem"),
        ca_fingerprint: str = _ca_fingerprint_option(),
        options: OptionsOpt = None,
        recv_timeout_ms: RecvTimeoutOpt = DEFAULT_RECV_TIMEOUT_MS,
    ):
        """
        Run a monitor driver — one that only reports upward on its own schedule
        and never handles JobDispatch (RFC 0001 §4, §7).

        The devices it can run and the -o options each one reads are listed below.
        """
        _start(
            Operation.MONITOR,
            device=device,
            qpi_addr=qpi_addr,
            token=token,
            name=name,
            ca_file=ca_file,
            ca_fingerprint=ca_fingerprint,
            options=options,
            recv_timeout_ms=recv_timeout_ms,
        )

    def _start(
        operation: Operation,
        *,
        device: str,
        qpi_addr: str,
        token: str,
        name: str,
        ca_file: Path,
        ca_fingerprint: str,
        options: list[str] | None,
        recv_timeout_ms: int,
    ) -> None:
        """Build the driver for *device* within *operation* and run it.

        Shared by every operation: the device selects the backend, its own option
        schema checks and coerces the -o values, and its builder turns those into
        the unstarted driver that gets started here. Anything rejected along the
        way — an unknown device or option, a missing or bad value — surfaces as a
        ``ValueError`` and becomes a one-line CLI error rather than a traceback.
        """
        if not token:
            typer.echo(
                "Error: access token is required. "
                "Set it via --token / -t or the QPI_ACCESS_TOKEN environment variable.",
                err=True,
            )
            raise typer.Exit(code=1)

        # Usage errors, reported before the banner so they are the first thing on
        # screen: which device, and whether its options make sense at all.
        try:
            spec = resolve_device(operation, device)
            parsed = spec.parse_options(_split_options(options or []))
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
                name=name,
                ca_fingerprint=ca_fingerprint,
                ca_file_path=ca_file.as_posix(),
                recv_timeout_ms=recv_timeout_ms,
            )
        except ValueError as exc:
            typer.echo(f"Error: {exc}", err=True)
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
        List the operations, the devices each one can run, and their -o options.

        The same catalog `process --help` and `monitor --help` show, in one place;
        `catalog --json` is the machine-readable form.
        """
        typer.echo(render_catalog(operation))

    @app.command()
    def catalog(
        as_json: Annotated[
            bool,
            typer.Option(
                "--json/--text",
                help="Print the catalog as JSON, or as the same text `devices` shows.",
            ),
        ] = True,
    ):
        """
        Print the whole device catalog for another program to read.

        The JSON shape is a contract — QPI-UI checks its own catalog against it,
        and the Go and TypeScript SDKs mirror it — and is documented in full on
        `qpi_driver.builtins.catalog.catalog_dict` (RFC 0003 §9).
        """
        if not as_json:
            typer.echo(render_catalog())
            return
        typer.echo(json.dumps(catalog_dict(), indent=2))

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

    def _split_options(pairs: list[str]) -> dict[str, str]:
        """Turn repeatable ``-o key=value`` options into a dict of raw strings.

        Only the syntax is the CLI's business; what the keys mean, whether they
        exist and what type each value is belong to the chosen device's schema
        (:meth:`DeviceSpec.parse_options`), which runs on the result.
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

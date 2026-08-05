"""Check 3: every ``qpi-driver`` flag a document names is one the CLI accepts."""

import os
import re
import subprocess
import sys
from pathlib import Path

from _check_docs.document import ROOT, Line, Report

FLAG_RE = re.compile(r"(?<![\w-])(--[a-z][a-z0-9-]+)")
DRIVER_COMMAND_RE = re.compile(r"qpi-driver\b|qpi_driver\.cli\b")

# Every subcommand whose flags a document might legitimately name.
# `devices --operation` is as much part of the documented CLI as `start` is.
SUBCOMMANDS = ("", "start", "devices")

#: Below this many flags across every subcommand, the help is not a real one.
#: Each SDK's `start` alone documents more than this.
MINIMUM_PLAUSIBLE_FLAGS = 4

CLI_INVOCATIONS = {
    "py": ([sys.executable, "-m", "qpi_driver.cli"], ROOT / "qpi-driver" / "py"),
    "go": (["go", "run", "./qpi-driver"], ROOT / "qpi-driver" / "go"),
    "js": (["node", "dist/builtins/cli.js"], ROOT / "qpi-driver" / "js"),
}


def read_cli_flags(sdk: str) -> set[str] | None:
    """The long flags this SDK's CLI accepts, from its own --help.

    None when the CLI cannot be run here: an absent toolchain should skip the check
    with a warning, not fail the build. COLUMNS is set wide because typer wraps
    --help to the terminal, and a flag broken across two lines is a flag this would
    not find.
    """
    argv, cwd = CLI_INVOCATIONS[sdk]
    env = {
        **os.environ,
        "COLUMNS": "400",
        "TERM": "dumb",
        "NO_COLOR": "1",
        "PYTHONPATH": str(CLI_INVOCATIONS["py"][1]),
    }
    found: set[str] = set()
    for subcommand in SUBCOMMANDS:
        command = argv + ([subcommand] if subcommand else []) + ["--help"]
        try:
            out = subprocess.run(
                command, cwd=cwd, capture_output=True, text=True, env=env
            )
        except FileNotFoundError:
            return None
        if out.returncode != 0:
            return None
        found |= set(FLAG_RE.findall(out.stdout + out.stderr))

    # A CLI that ran but told us almost nothing is not a CLI whose flags we can
    # check against — it is a stub. `qpi_driver.cli` is importable from the
    # source tree whether or not the `cli` extra is installed (PYTHONPATH above
    # guarantees it), and without typer it prints a near-empty help and exits 0.
    # Believing that would report every documented flag as removed, which is a
    # wall of false failures rather than the one true statement "the CLI could
    # not be run here".
    if len(found) < MINIMUM_PLAUSIBLE_FLAGS:
        return None
    return found


def check_cli_flags(
    path: Path, lines: list[Line], report: Report, accepted: set[str]
) -> None:
    """Flags are attributed to the command they follow.

    A line is only checked from the last `qpi-driver` token onwards, so
    `uv tool install --with mylab-devices "qpi-driver[cli]"` does not have `--with`
    read as a driver flag. A command continued with a trailing backslash keeps the
    attribution across the continuation lines.
    """
    continuing = False
    for line in lines:
        code = line.code
        if DRIVER_COMMAND_RE.search(code):
            for match in DRIVER_COMMAND_RE.finditer(code):
                segment = code[match.end() :]  # the last occurrence wins
            continuing = True
        elif continuing:
            segment = code
        else:
            continue

        for flag in FLAG_RE.findall(segment):
            report.checked += 1
            if flag not in accepted:
                report.fail(
                    path,
                    line.number,
                    f"`{flag}` is not a flag this SDK's qpi-driver accepts — it was "
                    f"renamed or removed. The CLI's own --help is the list.",
                )
        continuing = code.rstrip().endswith("\\")

"""Check 1: every ``make`` target a document names exists."""

import re
import subprocess
from pathlib import Path

from _check_docs.document import ROOT, Line, Report

MAKE_TARGET_RE = re.compile(r"\bmake\s+([a-z][a-z0-9_-]*)")


def read_makefile_targets() -> set[str]:
    """Every target the Makefile defines, read from `make`'s own database.

    Not a regex over the file: a `.PHONY` entry looks like a declaration and defines
    nothing, which is exactly the bug this check exists for.
    """
    out = subprocess.run(
        ["make", "-qp", "-f", str(ROOT / "Makefile")],
        cwd=ROOT,
        capture_output=True,
        text=True,
    ).stdout
    return {
        match.group(1)
        for match in (
            re.match(r"^([a-zA-Z][a-zA-Z0-9_.-]*):(?!=)", line)
            for line in out.splitlines()
        )
        if match
    }


def check_make_targets(
    path: Path, lines: list[Line], report: Report, known: set[str]
) -> None:
    for line in lines:
        for target in MAKE_TARGET_RE.findall(line.code):
            report.checked += 1
            if target not in known:
                report.fail(
                    path,
                    line.number,
                    f"`make {target}` is not a Makefile target — name the one that "
                    f"exists, or add it. A .PHONY entry alone is not a target.",
                )

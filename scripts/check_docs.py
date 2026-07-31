#!/usr/bin/env python3
"""Check the documentation against the repository it documents.

Run through ``make test-docs``. This is the static half — the checks that need
nothing installed beyond the SDKs themselves, so they run first and fail fast:

1. **Every ``make`` target a document names exists.** A `make` line in a runbook is
   a promise; ``make test-e2e-driver-framework`` sat in ``docs/driver/operations.md``
   for a release, existing only in the Makefile's ``.PHONY`` list.
2. **Every repository path a document names exists.** Both halves of
   ``pip install ./qpi-driver[cli]`` and of
   ``-o quantify_device_config=quantify.device.example.json`` were wrong at once — a
   directory with no ``pyproject.toml`` and a file that has never existed.
3. **Every ``qpi-driver`` flag a document names is one the CLI accepts.** The flags
   come from each SDK's own ``--help``, so a removed flag cannot linger in a README,
   and each document is checked against the SDK it belongs to.

The runnable half — the snippets, the error transcripts, the installed example — is
the rest of ``make test-docs``.

Three deliberate exclusions:

* ``CHANGELOG.md`` is a record of what *was* true, so it quotes commands and paths
  that no longer work on purpose. Checking it would make writing history impossible.
* A fenced block preceded by ``<!-- docs-check: skip -->`` is left alone. Some
  commands are meant to fail — the ``-o qubit_counr=4`` typo that demonstrates the
  option error is the point of the block it is in.
* ``docs/**`` is mostly symlinks to the READMEs, so the real file is walked and the
  link ignored. A harness that walked both would report every failure twice.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SKIP_MARKER = "<!-- docs-check: skip -->"

# A record of the past, not instructions for the present. See the module docstring.
EXCLUDED = {"CHANGELOG.md"}

# The SDK whose CLI each document's `qpi-driver` commands belong to. A document not
# listed here has no CLI of its own and is checked for make targets and paths only.
CLI_BY_DOC = {
    "README.md": "py",
    "qpi-driver/py/README.md": "py",
    "qpi-driver/go/README.md": "go",
    "qpi-driver/js/README.md": "js",
    "qpi-driver/py/examples/custom_device/README.md": "py",
    "docs/driver/operations.md": "py",
}


# ---------------------------------------------------------------------------
# Reading a document
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Line:
    """One line of a document, with what a check needs to know about where it is.

    ``code`` is the part of the line inside a fenced block or inline backticks —
    empty for prose. Every check runs on ``code`` rather than the raw line, because
    "we make the operation a value" is prose about the word *make*, not a promise
    about a Makefile target.
    """

    number: int
    raw: str
    code: str


INLINE_CODE_RE = re.compile(r"`([^`]+)`")

# A shell comment is prose that happens to be inside a fence — "# the make targets it
# names" is not a command. Only a `#` that starts a token counts, so the `#` in
# `--device ./dist/my-device.js#MyExport` is left where it is.
SHELL_COMMENT_RE = re.compile(r"(?:(?<=\s)|^)#.*$")


def read(path: Path) -> list[Line]:
    """The document's lines, skip-marked fences dropped, code spans extracted."""
    lines: list[Line] = []
    skip_next_fence = False
    in_fence = False
    in_skipped_fence = False

    for number, raw in enumerate(path.read_text().splitlines(), start=1):
        stripped = raw.strip()
        if stripped == SKIP_MARKER:
            skip_next_fence = True
            continue
        if stripped.startswith("```"):
            if in_skipped_fence:
                in_skipped_fence, in_fence = False, False
            elif in_fence:
                in_fence = False
            elif skip_next_fence:
                in_skipped_fence, skip_next_fence = True, True
            else:
                in_fence = True
            continue
        if in_skipped_fence:
            continue
        code = raw if in_fence else " ".join(INLINE_CODE_RE.findall(raw))
        lines.append(Line(number, raw, SHELL_COMMENT_RE.sub("", code)))
    return lines


def documents() -> list[Path]:
    candidates = [
        *ROOT.glob("*.md"),
        *ROOT.glob("docs/**/*.md"),
        *ROOT.glob("qpi-driver/*/README.md"),
        *ROOT.glob("qpi-driver/py/examples/*/README.md"),
        *ROOT.glob("qpi-client/*/README.md"),
    ]
    seen: dict[Path, Path] = {}
    for path in candidates:
        if path.is_symlink() or str(path.relative_to(ROOT)) in EXCLUDED:
            continue
        seen[path.resolve()] = path
    return sorted(seen.values())


@dataclass
class Report:
    """Collected rather than raised, so one run names every problem."""

    failures: list[str] = field(default_factory=list)
    checked: int = 0

    def fail(self, path: Path, number: int, message: str) -> None:
        self.failures.append(f"{path.relative_to(ROOT)}:{number}: {message}")


# ---------------------------------------------------------------------------
# 1. make targets
# ---------------------------------------------------------------------------

MAKE_TARGET_RE = re.compile(r"\bmake\s+([a-z][a-z0-9_-]*)")


def makefile_targets() -> set[str]:
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


# ---------------------------------------------------------------------------
# 2. repository paths
# ---------------------------------------------------------------------------

PIP_TARGET_RE = re.compile(r"""(?:pip|uv pip) install\s+["']?(\.[^"'\s\[]*)""")
O_PATH_RE = re.compile(r"-o\s+\w+=([\w./-]*\.(?:json|ya?ml|pem|toml|py))")
LINK_RE = re.compile(r"\]\((?!https?:|#|mailto:)([^)\s]+)\)")

# A path is ours to check when it names a top-level directory of this repository.
# Anything else is a path on the reader's machine (`/var/qpi-driver/…`, `./data`).
REPO_DIRS = tuple(
    p.name for p in ROOT.iterdir() if p.is_dir() and not p.name.startswith(".")
)


def check_paths(path: Path, lines: list[Line], report: Report) -> None:
    """Paths resolve from the document's own directory — a reader who `cd`s to the
    example and runs `pip install .` means that example, not the repository root."""
    for line in lines:
        for raw in PIP_TARGET_RE.findall(line.code):
            report.checked += 1
            target = (path.parent / raw).resolve()
            if not target.is_dir():
                report.fail(path, line.number, f"`pip install {raw}`: not a directory")
            elif not (target / "pyproject.toml").exists():
                report.fail(
                    path,
                    line.number,
                    f"`pip install {raw}`: {raw} has no pyproject.toml, so there is "
                    f"nothing there to install",
                )

        for raw in O_PATH_RE.findall(line.code):
            if not raw.lstrip("./").startswith(REPO_DIRS):
                continue  # a path on the reader's machine, not ours
            report.checked += 1
            if not (ROOT / raw.lstrip("./")).exists():
                report.fail(
                    path, line.number, f"{raw} does not exist in this repository"
                )

    for line in lines:  # links are prose, so read from the raw line
        for raw in LINK_RE.findall(line.raw):
            target = raw.split("#", 1)[0]
            if not target:
                continue
            report.checked += 1
            if not (path.parent / target).resolve().exists():
                report.fail(path, line.number, f"link target {raw} does not exist")


# ---------------------------------------------------------------------------
# 3. qpi-driver CLI flags
# ---------------------------------------------------------------------------

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


def cli_flags(sdk: str) -> set[str] | None:
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
            segment = code[DRIVER_COMMAND_RE.search(code).end() :]
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


# ---------------------------------------------------------------------------


def main() -> int:
    known_targets = makefile_targets()
    flags_cache: dict[str, set[str] | None] = {}
    report = Report()
    docs = documents()

    for path in docs:
        relative = str(path.relative_to(ROOT))
        lines = read(path)

        check_make_targets(path, lines, report, known_targets)
        check_paths(path, lines, report)

        sdk = CLI_BY_DOC.get(relative)
        if sdk is None:
            continue
        if sdk not in flags_cache:
            flags_cache[sdk] = cli_flags(sdk)
            if flags_cache[sdk] is None:
                print(
                    f"[check-docs] ⚠ skipping CLI flag checks for the {sdk} SDK: its "
                    f"CLI could not be run here",
                    file=sys.stderr,
                )
        accepted = flags_cache[sdk]
        if accepted:
            check_cli_flags(path, lines, report, accepted)

    if report.failures:
        print(f"[check-docs] ✗ {len(report.failures)} problem(s):", file=sys.stderr)
        for failure in report.failures:
            print(f"  {failure}", file=sys.stderr)
        return 1

    print(
        f"[check-docs] ✓ {report.checked} claims checked across {len(docs)} documents"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

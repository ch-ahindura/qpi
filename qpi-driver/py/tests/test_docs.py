"""The documentation's own transcripts, checked against what the CLI prints.

A README that quotes an error message, or lists the flags a command takes, is making
a claim about this code — and the two drift in one direction only, because nobody
re-runs a command to check the block they copied it from. So these read the documents
and assert the claim.

Which documents: ``qpi-driver/py/README.md`` and ``docs/driver/operations.md``, the
two that quote this CLI. They are read as files rather than duplicated here, so a
sentence rewritten in the README is checked as rewritten, and a message that changes
fails at the line of the document that has to change with it.

The rest of the documentation checks — the paths, the make targets, the flags in every
document, the snippets in every language — are ``make test-docs``. These live here
because the strings they are about are produced by the code in this package.
"""

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

# The transcripts are the CLI's, so there is nothing to check without the CLI —
# skipped rather than failed on the base run, as the other CLI tests are.
has_typer = importlib.util.find_spec("typer") is not None

pytestmark = pytest.mark.skipif(
    not has_typer, reason="typer must be installed to run CLI tests"
)

if has_typer:
    from typer.testing import CliRunner

    from qpi_driver.cli import app

    runner = CliRunner()

REPO_ROOT = Path(__file__).resolve().parents[3]
PY_README = REPO_ROOT / "qpi-driver" / "py" / "README.md"
OPERATIONS = REPO_ROOT / "docs" / "driver" / "operations.md"


def _output(result) -> str:
    """Typer's result, with the exception text a failed exit carries."""
    text = result.output
    if result.exception is not None:
        text += str(result.exception)
    return text


# ---------------------------------------------------------------------------
# The four error transcripts in docs/driver/operations.md
# ---------------------------------------------------------------------------

# Each documented message, with the invocation that has to produce it. The message
# is not written here — it is read out of the document by its prefix, so the document
# is the source and this is only the command that proves it.
TRANSCRIPT_CASES = [
    pytest.param(
        "unknown option 'data_dirr'",
        [
            "start",
            "--operation",
            "process",
            "--token",
            "t",
            "--ca-fingerprint",
            "f",
            "-o",
            "data_dirr=/data",
        ],
        id="unknown-option",
    ),
    pytest.param(
        "monitor device 'bluefors_gen1' needs a 'channels' option",
        ["start", "--operation", "monitor", "--token", "t", "--ca-fingerprint", "f"],
        id="missing-required-option",
    ),
    pytest.param(
        "bad value for -o job_timeout",
        [
            "start",
            "--operation",
            "process",
            "--token",
            "t",
            "--ca-fingerprint",
            "f",
            "-o",
            "job_timeout=soon",
        ],
        id="bad-value",
    ),
    pytest.param(
        "bad value for -o data_dir",
        [
            "start",
            "--operation",
            "process",
            "--token",
            "t",
            "--ca-fingerprint",
            "f",
            "-o",
            "data_dir=/var",
        ],
        id="unsafe-path",
    ),
]


def documented_error_lines() -> list[str]:
    """Every `Error: …` line quoted in the operations runbook, unwrapped.

    The runbook hard-wraps a long message across two lines to stay readable, so a
    continuation is joined back on before comparing — otherwise the check would be
    about the document's line width rather than the message. Only inside a fenced
    block, so the prose that follows one is never read as part of it.
    """
    lines: list[str] = []
    in_fence = False
    open_message = False

    for raw in OPERATIONS.read_text().splitlines():
        stripped = raw.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            open_message = False
            continue
        if not in_fence:
            continue
        if stripped.startswith("Error: "):
            lines.append(stripped)
            open_message = True
        elif open_message and stripped and not stripped.startswith("$"):
            lines[-1] = f"{lines[-1]} {stripped}"
        else:
            open_message = False
    return lines


@pytest.mark.parametrize(("prefix", "argv"), TRANSCRIPT_CASES)
def test_the_documented_error_is_the_error_the_cli_prints(prefix, argv):
    """A message quoted in the runbook is the message an operator will see.

    All four exit 1 before connecting to anything, which is why they can be asserted
    here with no server (RFC 0003 §10).
    """
    documented = [line for line in documented_error_lines() if prefix in line]
    assert documented, (
        f"no `Error: …{prefix}…` line in {OPERATIONS.name}. If the message moved, "
        f"move this case with it; if it is gone, delete both."
    )

    result = runner.invoke(app, argv)
    assert result.exit_code == 1, _output(result)

    printed = " ".join(_output(result).split())
    for line in documented:
        expected = " ".join(line.removeprefix("Error: ").split())
        assert expected in printed, (
            f"{OPERATIONS.name} documents:\n  {expected}\nbut the CLI printed:\n  "
            f"{printed}"
        )


# ---------------------------------------------------------------------------
# The CLI reference block in qpi-driver/py/README.md
# ---------------------------------------------------------------------------

FLAG_RE = re.compile(r"(?<![\w-])(--[a-z][a-z0-9-]+)")


def documented_universal_flags() -> set[str]:
    """The long flags the README's "Universal options" block lists."""
    text = PY_README.read_text()
    start = text.index("Universal options:")
    end = text.index("```", start)
    return set(FLAG_RE.findall(text[start:end]))


def actual_universal_flags() -> set[str]:
    """The long flags `start --help` actually offers.

    Through a subprocess with a wide COLUMNS, because typer wraps its help to the
    terminal and a flag split across two lines is a flag no regex finds.
    """
    out = subprocess.run(
        [sys.executable, "-m", "qpi_driver.cli", "start", "--help"],
        capture_output=True,
        text=True,
        env={
            "COLUMNS": "400",
            "TERM": "dumb",
            "NO_COLOR": "1",
            "PATH": "/usr/bin:/bin",
        },
        cwd=REPO_ROOT / "qpi-driver" / "py",
    )
    assert out.returncode == 0, out.stderr
    return set(FLAG_RE.findall(out.stdout))


def test_the_readmes_cli_reference_lists_every_universal_flag():
    """The block calls itself a reference, so an omission is a wrong reference.

    This is the direction a hand-maintained transcript drifts: a flag is added and
    the block is not touched, so the one place a reader looks for the whole list
    quietly stops being it. The other direction — a flag in a document the CLI does
    not have — is `make test-docs`, across every document rather than this one.
    """
    documented = documented_universal_flags()
    actual = actual_universal_flags()

    missing = actual - documented
    assert not missing, (
        f"`qpi-driver start --help` offers {sorted(missing)}, which the README's "
        f"Universal options block does not list. Add them, or stop calling it a "
        f"reference."
    )

    stale = documented - actual
    assert not stale, (
        f"the README's Universal options block lists {sorted(stale)}, which "
        f"`qpi-driver start --help` does not offer."
    )

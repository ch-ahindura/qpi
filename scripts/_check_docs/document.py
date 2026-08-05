"""Reading a document: its lines, its code spans, and where a failure is reported."""

import re
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

SKIP_MARKER = "<!-- docs-check: skip -->"

# A record of the past, not instructions for the present: a changelog quotes
# commands and paths that no longer work on purpose.
EXCLUDED = {"CHANGELOG.md"}

INLINE_CODE_RE = re.compile(r"`([^`]+)`")

# A shell comment is prose that happens to be inside a fence — "# the make targets it
# names" is not a command. Only a `#` that starts a token counts, so the `#` in
# `--device ./dist/my-device.js#MyExport` is left where it is.
SHELL_COMMENT_RE = re.compile(r"(?:(?<=\s)|^)#.*$")


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


def read_lines(path: Path) -> list[Line]:
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


def find_documents() -> list[Path]:
    candidates = [
        *ROOT.glob("*.md"),
        *ROOT.glob("docs/**/*.md"),
        *ROOT.glob("qpi-driver/*/README.md"),
        *ROOT.glob("qpi-driver/py/examples/*/README.md"),
        *ROOT.glob("qpi-client/*/README.md"),
        # Named individually because the symlink under docs/ that publishes each of
        # these is skipped below: a glob that reaches only the symlink checks nothing.
        # The dashboard README sat unchecked long enough to still be the Vite scaffold.
        ROOT / "qpi-driver/py/qpi_driver/tuners/README.md",
        ROOT / "qpi-ui/internal/dashboard/README.md",
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

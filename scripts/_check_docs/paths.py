"""Check 2: every repository path a document names exists."""

import re
from pathlib import Path

from _check_docs.document import ROOT, Line, Report

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

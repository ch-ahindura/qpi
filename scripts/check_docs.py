#!/usr/bin/env python3
"""Check the documentation against the repository it documents.

Run through ``make test-docs``. This is the static half — the checks that need
nothing installed beyond the SDKs themselves, so they run first and fail fast:

1. **Every ``make`` target a document names exists.** A `make` line in a runbook is
   a promise; ``make test-e2e-driver-framework`` sat in ``docs/driver/operations.md``
   for a release, existing only in the Makefile's ``.PHONY`` list. See
   :mod:`_check_docs.make_targets`.
2. **Every repository path a document names exists.** Both halves of
   ``pip install ./qpi-driver[cli]`` and of
   ``-o quantify_device_config=quantify.device.example.json`` were wrong at once — a
   directory with no ``pyproject.toml`` and a file that has never existed. See
   :mod:`_check_docs.paths`.
3. **Every ``qpi-driver`` flag a document names is one the CLI accepts.** The flags
   come from each SDK's own ``--help``, so a removed flag cannot linger in a README,
   and each document is checked against the SDK it belongs to. See
   :mod:`_check_docs.cli_flags`.

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

import sys

from _check_docs.cli_flags import check_cli_flags, read_cli_flags
from _check_docs.document import ROOT, Report, find_documents, read_lines
from _check_docs.make_targets import check_make_targets, read_makefile_targets
from _check_docs.paths import check_paths

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


def main() -> int:
    known_targets = read_makefile_targets()
    flags_cache: dict[str, set[str] | None] = {}
    report = Report()
    docs = find_documents()

    for path in docs:
        relative = str(path.relative_to(ROOT))
        lines = read_lines(path)

        check_make_targets(path, lines, report, known_targets)
        check_paths(path, lines, report)

        sdk = CLI_BY_DOC.get(relative)
        if sdk is None:
            continue
        if sdk not in flags_cache:
            flags_cache[sdk] = read_cli_flags(sdk)
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

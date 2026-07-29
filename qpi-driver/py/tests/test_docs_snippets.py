"""Every Python snippet in the driver documentation, run against the real SDK.

The claim a code block makes is that it works, and it is the claim most easily
broken: the custom-executor example in ``qpi-driver/py/README.md`` was wrong twice —
once passing a keyword no function accepted, once defining only one of ``Executor``'s
two abstract methods, so it could not be instantiated. Both would have been a line of
output here.

How a snippet is run:

* It is executed in a fresh namespace, so one block cannot lean on another's imports.
* ``QpiDriver.run`` is stubbed. Every driver snippet ends in ``.run()``, which would
  otherwise try to reach a server; what is being checked is that the code up to that
  point is valid and the driver can be constructed at all.
* The device registry is swapped for a copy. A snippet showing how to add a device
  ends in ``register(SPEC)``, and the registry is process-global — running the
  documentation would otherwise leave ``thermometer`` and ``bluefors_gen2`` in it for
  every later test to trip over. A copy rather than an empty table, so a snippet that
  registers a name the SDK already ships still fails here.
* A block that is deliberately not meant to run is skipped, by preceding it with
  ``<!-- docs-check: skip -->`` in the document. There are none today; the marker
  exists so that a block which needs one does not become a reason to delete this
  test.

Only the documents that describe this SDK: the Python README and the custom-device
example. A ``typescript`` or ``go`` block is somebody else's compiler's business
(``make test-docs``).
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest

from qpi_driver.builtins import registry
from qpi_driver.sdk import QpiDriver

REPO_ROOT = Path(__file__).resolve().parents[3]

DOCUMENTS = [
    REPO_ROOT / "qpi-driver" / "py" / "README.md",
    REPO_ROOT / "qpi-driver" / "py" / "examples" / "custom_device" / "README.md",
]

SKIP_MARKER = "<!-- docs-check: skip -->"


@dataclass(frozen=True)
class Snippet:
    document: Path
    line: int
    source: str

    @property
    def label(self) -> str:
        return f"{self.document.name}:{self.line}"


def python_snippets(path: Path) -> list[Snippet]:
    """The ```python blocks in *path*, with the line each one starts on."""
    snippets: list[Snippet] = []
    lines = path.read_text().splitlines()
    skip_next = False
    index = 0

    while index < len(lines):
        stripped = lines[index].strip()
        if stripped == SKIP_MARKER:
            skip_next = True
            index += 1
            continue
        if stripped in ("```python", "```py"):
            start = index + 1
            end = start
            while end < len(lines) and not lines[end].strip().startswith("```"):
                end += 1
            if not skip_next:
                snippets.append(Snippet(path, start + 1, "\n".join(lines[start:end])))
            skip_next = False
            index = end + 1
            continue
        if stripped.startswith("```"):
            skip_next = False
        index += 1
    return snippets


ALL_SNIPPETS = [snippet for path in DOCUMENTS for snippet in python_snippets(path)]


def test_there_are_snippets_to_check():
    """A refactor that stops finding any block would otherwise pass silently."""
    assert len(ALL_SNIPPETS) >= 3, (
        f"only found {len(ALL_SNIPPETS)} python blocks across "
        f"{[p.name for p in DOCUMENTS]}; the extractor has probably stopped matching"
    )


@pytest.mark.parametrize(
    "snippet", ALL_SNIPPETS, ids=[snippet.label for snippet in ALL_SNIPPETS]
)
def test_the_snippet_runs(snippet: Snippet, monkeypatch, tmp_path):
    """Import it, define it, construct the driver — then stop short of connecting."""
    connected: list[QpiDriver] = []
    monkeypatch.setattr(QpiDriver, "run", lambda self: connected.append(self))
    # A snippet that registers a device must not leave it in the process-global table.
    monkeypatch.setattr(
        registry,
        "_DEVICES",
        {op: dict(table) for op, table in registry._DEVICES.items()},
    )
    # A snippet writing under `./data` or `./bin/data` writes it wherever pytest was
    # run from, which is not somewhere a test may leave things.
    monkeypatch.chdir(tmp_path)
    # The example's module sits beside its README, not on the path.
    monkeypatch.syspath_prepend(
        str(REPO_ROOT / "qpi-driver" / "py" / "examples" / "custom_device")
    )

    namespace: dict[str, object] = {"__name__": "__doc_snippet__"}
    try:
        exec(compile(snippet.source, snippet.label, "exec"), namespace)
    except Exception as exc:  # noqa: BLE001 — the failure is the result
        pytest.fail(
            f"{snippet.label} does not run: {type(exc).__name__}: {exc}\n\n"
            f"{textwrap.indent(snippet.source, '    ')}"
        )

"""Which routine last measured each parameter, and when (RFC 0008).

A device config records what every parameter *is*. This records where it came from, so
that "has this ever been measured?" is a question the driver can answer. The August 2026
chip carried ``clock_freqs.f01: 4735509751.238763`` — nine significant figures, so it read
as a measurement — while the qubit sat 302 MHz away. Precision is not provenance.

A sidecar beside the device config rather than inside it: that file's schema belongs to
quantify, whose models reject unknown keys, and ``calibration.yml`` is hand-authored
intent a machine must not rewrite (RFC 0008 §5). This file holds **no values** — only
metadata about them — so it is never a second source of truth for what the chip is, and
deleting it costs nothing but the memory of what was measured.

Absence is not an error anywhere here. A missing, empty, corrupt or half-written sidecar
means every parameter is a prior, which is both conservative and exactly the behaviour
before this existed — a fresh checkout has no sidecar and must still calibrate.
"""

import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

#: What the sidecar is called, given the device config beside it:
#: ``quantify.device.yml`` -> ``quantify.device.provenance.yml``.
PROVENANCE_SUFFIX = ".provenance.yml"

#: The fit numbers worth keeping, of the whole payload a routine fitted.
#:
#: RFC 0008 §11 asked what of the fit to store. These four, because they are the scalars a
#: guard *judged*: `require_resolved_curve` compares ``reach``, the discriminators compare
#: ``separation`` and ``contrast``, and ``snr`` is reported everywhere. The rest of a fit
#: payload is the sweep itself, which is large, already capped for the event by RFC 0005,
#: and answers no question this file exists for.
FIT_SUMMARY_KEYS: tuple[str, ...] = ("snr", "reach", "contrast", "separation")


@dataclass(frozen=True)
class Provenance:
    """Where one parameter on one target came from."""

    routine: str
    at: str
    run: str | None = None
    fit: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        record: dict[str, Any] = {"routine": self.routine, "at": self.at}
        if self.run is not None:
            record["run"] = self.run
        if self.fit:
            record["fit"] = dict(self.fit)
        return record

    def describe(self) -> str:
        """``measured by ramsey at 2026-08-12T09:22:17Z``, for a report an operator reads."""
        return f"measured by {self.routine} at {self.at}"


class ProvenanceStore:
    """The sidecar, in memory, keyed by ``(target, dotted path)``.

    Merged per key rather than rewritten, in both directions. `record` merges into what
    was loaded, and `save` re-reads the file first and merges into *that*, so a walk that
    measured three parameters cannot erase the provenance of the ninety it did not touch —
    including any written by another process while it ran.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._records: dict[str, dict[str, Provenance]] = {}

    @classmethod
    def load(cls, device_config_path: Path | None) -> "ProvenanceStore":
        """The store beside *device_config_path*, or an empty one if it cannot be read."""
        if device_config_path is None:
            return cls(None)
        store = cls(provenance_path(device_config_path))
        store._records = _read(store.path)
        return store

    def of(self, target: str, path: str) -> Provenance | None:
        """What measured *path* on *target*, or ``None`` if nothing is on record."""
        return self._records.get(target, {}).get(path)

    def is_measured(self, target: str, path: str) -> bool:
        """Whether *path* on *target* was ever measured. The negative is RFC 0008's *prior*."""
        return self.of(target, path) is not None

    def record(self, target: str, path: str, provenance: Provenance) -> None:
        self._records.setdefault(target, {})[path] = provenance

    def targets(self) -> list[str]:
        return sorted(self._records)

    def paths(self, target: str) -> list[str]:
        return sorted(self._records.get(target, {}))

    def save(self) -> None:
        """Merge this store into the file and write it, or do nothing if there is no path.

        Never raises. A calibration that measured a chip correctly must not be reported as
        failed because a metadata file could not be written — the values themselves are
        already in the device config, which has its own write-back and its own guarantees.
        """
        if self.path is None or not self._records:
            return
        merged = _read(self.path)
        for target, paths in self._records.items():
            merged.setdefault(target, {}).update(paths)
        try:
            _write(self.path, merged)
        except Exception:
            log.exception("could not write provenance to %s", self.path)


def provenance_path(device_config_path: Path) -> Path:
    """The sidecar's path, so it is found wherever the device config is and moves with it."""
    return Path(device_config_path).with_suffix(PROVENANCE_SUFFIX)


def fit_summary(fit: Any) -> dict[str, float]:
    """The `FIT_SUMMARY_KEYS` of *fit* that are finite numbers.

    Infinities are dropped rather than stored: ``snr`` is infinite when a fit had no
    residual scatter at all, and YAML round-trips ``.inf`` in a way not every reader of
    this file would survive.
    """
    if not isinstance(fit, dict):
        return {}
    summary = {}
    for key in FIT_SUMMARY_KEYS:
        value = fit.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        number = float(value)
        if number != number or number in (float("inf"), float("-inf")):
            continue
        summary[key] = round(number, 4)
    return summary


def _read(path: Path | None) -> dict[str, dict[str, Provenance]]:
    """Whatever *path* holds that parses as provenance, skipping whatever does not.

    Every failure mode resolves to fewer records rather than an exception: a parameter
    with no readable record is a prior, which is the safe answer and the answer the
    absent-file case already gives.
    """
    if path is None or not path.exists():
        return {}
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except Exception:
        log.warning("could not parse %s; treating every parameter as a prior", path)
        return {}
    if not isinstance(raw, dict):
        log.warning("%s is not a mapping; treating every parameter as a prior", path)
        return {}

    records: dict[str, dict[str, Provenance]] = {}
    for target, paths in raw.items():
        if not isinstance(paths, dict):
            continue
        for dotted, entry in paths.items():
            # A record with no routine names nothing, so it cannot attribute anything.
            if not isinstance(entry, dict) or not entry.get("routine"):
                continue
            records.setdefault(str(target), {})[str(dotted)] = Provenance(
                routine=str(entry["routine"]),
                at=str(entry.get("at", "")),
                run=None if entry.get("run") is None else str(entry["run"]),
                fit=fit_summary(entry.get("fit")),
            )
    return records


def _write(path: Path, records: dict[str, dict[str, Provenance]]) -> None:
    """Replace *path* atomically, so a crash mid-write leaves the old file intact."""
    serialisable = {
        target: {dotted: entry.to_dict() for dotted, entry in sorted(paths.items())}
        for target, paths in sorted(records.items())
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(handle, "w") as stream:
            stream.write(
                "# Written by qpi-driver: which routine last measured each parameter\n"
                "# in the device config beside this file (RFC 0008). Holds no values,\n"
                "# is not read by anything that runs circuits, and is safe to delete.\n"
            )
            yaml.safe_dump(serialisable, stream, sort_keys=False)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)

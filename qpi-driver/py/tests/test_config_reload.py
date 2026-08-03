"""Tier 1: the change detector behind config hot-reload."""

import os
from pathlib import Path

from qpi_driver.reload import ConfigFile


def _write(path: Path, text: str) -> None:
    path.write_text(text)


def test_an_untouched_file_has_not_changed(tmp_path: Path):
    path = tmp_path / "device.yml"
    _write(path, "q0: {}")

    assert ConfigFile(path).changed() is False


def test_a_rewritten_file_has_changed(tmp_path: Path):
    path = tmp_path / "device.yml"
    _write(path, "q0: {}")
    watched = ConfigFile(path)

    _write(path, "q0: {clock_freqs: {f01: 5e9}}")

    assert watched.changed() is True


# The write-back replaces the file by rename, so the new content arrives on a new
# inode and may carry an mtime and size indistinguishable from the old.
def test_an_atomic_replacement_has_changed(tmp_path: Path):
    path = tmp_path / "device.yml"
    _write(path, "q0: {a: 1}")
    watched = ConfigFile(path)
    original = path.stat()

    replacement = tmp_path / "device.yml.tmp"
    _write(replacement, "q0: {a: 2}")
    os.utime(replacement, ns=(original.st_atime_ns, original.st_mtime_ns))
    replacement.replace(path)

    assert path.stat().st_size == original.st_size
    assert path.stat().st_mtime_ns == original.st_mtime_ns
    assert watched.changed() is True


def test_a_vanished_file_has_not_changed(tmp_path: Path):
    path = tmp_path / "device.yml"
    _write(path, "q0: {}")
    watched = ConfigFile(path)

    path.unlink()

    assert watched.changed() is False


def test_a_missing_file_that_appears_has_changed(tmp_path: Path):
    path = tmp_path / "device.yml"
    watched = ConfigFile(path)

    _write(path, "q0: {}")

    assert watched.changed() is True


def test_accept_settles_the_signature(tmp_path: Path):
    path = tmp_path / "device.yml"
    _write(path, "q0: {a: 1}")
    watched = ConfigFile(path)

    _write(path, "q0: {a: 2}")
    assert watched.changed() is True

    watched.accept()
    assert watched.changed() is False

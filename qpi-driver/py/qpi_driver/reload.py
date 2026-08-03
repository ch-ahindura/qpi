"""Noticing that a config file on disk is no longer the one we read."""

import logging
from pathlib import Path

log = logging.getLogger(__name__)

__all__ = ["ConfigFile"]


class ConfigFile:
    """A config file's path and the signature of the version last read.

    The signature includes the inode because the atomic write these files are
    given (``tuners/utils/persistence.py``) replaces rather than truncates, so a
    rename within the same second and at the same size would otherwise look
    unchanged.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._signature = self._read_signature()

    def _read_signature(self) -> tuple[int, int, int] | None:
        try:
            stat = self.path.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size, stat.st_ino)

    def changed(self) -> bool:
        """Whether the file differs from the version last accepted.

        A file that has gone away is not a change: the driver keeps running on
        what it has rather than losing its parameters to a botched copy.
        """
        signature = self._read_signature()
        if signature is None or signature == self._signature:
            return False
        return True

    def accept(self) -> None:
        """Record the file as read, so `changed` reports False until it moves."""
        self._signature = self._read_signature()

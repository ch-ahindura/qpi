"""Noticing that a config file on disk is no longer the one we read."""

import logging
from pathlib import Path

log = logging.getLogger(__name__)

__all__ = ["ConfigFile"]


class ConfigFile:
    """A config file's path and the signature of the version last read.

    The signature includes the inode: the atomic write these files get replaces
    rather than truncates, so a same-second same-size rename would otherwise look
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
        """Whether the file differs from the version last read.

        A vanished file is not a change: better to keep running on what we have than
        to lose the parameters to a botched copy.
        """
        signature = self._read_signature()
        return signature is not None and signature != self._signature

    def mark_read(self) -> None:
        """Settle the signature, so `changed` is False until the file moves again."""
        self._signature = self._read_signature()

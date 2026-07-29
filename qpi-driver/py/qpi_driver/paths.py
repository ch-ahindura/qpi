"""Filesystem safety checks shared by the CLI and the built-in drivers."""

from pathlib import Path


def validate_safe_path(path: Path, name: str) -> None:
    """Raise ``ValueError`` if *path* is outside where a driver may read/write.

    A driver only ever needs its own data and config directories, so a path
    outside a small allow-list (a stray token or config location pointing
    somewhere sensitive) is rejected. *name* names the offending option for the
    error message.
    """
    if not is_safe_path(path):
        raise ValueError(f"path for {name} ({path}) is not in a safe location")


def as_safe_dir(raw: str) -> Path:
    """Parse *raw* as a directory a driver may write to (RFC 0003 §5, §10).

    The parser of an ``-o`` option does not know what the option is called, so
    the message names the path and leaves naming the option to the caller.
    """
    path = Path(raw)
    if not is_safe_path(path):
        raise ValueError(f"{path} is not in a safe location")
    return path


def is_safe_path(path: Path) -> bool:
    """Whether *path* is inside somewhere a driver may read and write."""
    resolved = path.resolve().as_posix()
    permitted_folders = (
        Path("/var/qpi-driver").resolve().as_posix(),
        Path("/etc/qpi-driver").resolve().as_posix(),
    )
    permitted_parent_dirs = (
        Path.home().resolve().as_posix(),
        Path("/tmp").resolve().as_posix(),
        Path("/var/tmp").resolve().as_posix(),
    )

    for folder in permitted_folders:
        if resolved.startswith(folder):
            return True
    for folder in permitted_parent_dirs:
        if resolved != folder and resolved.startswith(folder):
            return True

    return False

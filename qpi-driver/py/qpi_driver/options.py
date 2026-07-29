"""The ``-o key=value`` settings a device was launched with.

There is no option schema in this SDK. A device's options are whatever keys its
builder reads, and a value's type is whichever accessor reads it — so there is no
second description of a device to keep in step with the device, and nothing for
QPI-UI to be checked against. QPI-UI is where an operator picks a device and fills
in its options; by the time a value reaches here it is a string on a command line.

Reads are remembered so :meth:`Options.unread` can report a key nothing looked at.
That is what keeps ``-o data_dr=/tmp`` an error rather than a setting silently
ignored, without a declared list of keys to check it against.
"""

from collections.abc import Mapping
from pathlib import Path

from qpi_driver.paths import as_safe_dir

# What counts as true in `-o is_dummy=…`. Anything else, including the empty
# string, is false.
_TRUTHY = ("1", "true", "yes", "on")


class Options:
    """Raw ``-o`` values, read by the device that understands them.

    Every accessor takes the fallback used when the key is absent, written as the
    value itself rather than as a string to parse: the default belongs to the one
    piece of code that acts on it.

    A value the accessor cannot read raises :class:`ValueError` naming the option,
    which the CLI turns into a single line.
    """

    def __init__(self, values: Mapping[str, str] | None = None) -> None:
        self._values = {key: str(value) for key, value in (values or {}).items()}
        self._read: set[str] = set()

    def __contains__(self, key: str) -> bool:
        """Whether *key* was given. Counts as reading it."""
        self._read.add(key)
        return key in self._values

    def __repr__(self) -> str:
        return f"Options({self._values!r})"

    def get_str(self, key: str, default: str = "") -> str:
        """The value of *key* as typed, or *default*."""
        self._read.add(key)
        return self._values.get(key, default)

    def require(self, key: str, example: str = "") -> str:
        """The value of *key*, or raise because the device cannot run without it.

        Raises:
            ValueError: if *key* was not given. *example* is appended as a
                ready-to-paste ``-o`` pair when there is a sensible one.
        """
        self._read.add(key)
        if key not in self._values:
            hint = f", e.g. -o {key}={example}" if example else ""
            raise ValueError(f"missing required option {key!r}{hint}")
        return self._values[key]

    def get_int(self, key: str, default: int) -> int:
        """The value of *key* as an integer, or *default*."""
        return self._parsed(key, default, int)

    def get_float(self, key: str, default: float) -> float:
        """The value of *key* as a float, or *default*."""
        return self._parsed(key, default, float)

    def get_bool(self, key: str, default: bool = False) -> bool:
        """The value of *key* as a boolean, or *default*.

        ``1``, ``true``, ``yes`` and ``on`` are true in any case; every other
        value, the empty string included, is false. Nothing here can fail, so a
        misspelled ``-o is_dummy=ture`` is false rather than an error — the
        alternative is refusing to start over a flag.
        """
        self._read.add(key)
        if key not in self._values:
            return default
        return self._values[key].strip().lower() in _TRUTHY

    def get_path(self, key: str, default: Path | str) -> Path:
        """The value of *key* as a path, or *default*. Not checked for safety."""
        return self._parsed(key, Path(default), Path)

    def get_dir(self, key: str, default: Path | str) -> Path:
        """The value of *key* as a directory a driver may write to, or *default*.

        The safe-location check of :func:`~qpi_driver.paths.as_safe_dir` applies to
        the given value and to *default* alike, so a device cannot default itself
        somewhere it may not write.
        """
        return self._parsed(key, None, as_safe_dir, fallback_raw=str(default))

    def remaining(self) -> dict[str, str]:
        """Every option not read yet, as typed, and marks them read.

        For a device that passes options on to something this SDK has never seen —
        an executor named by import path, whose constructor is the only thing that
        knows its keys. A device that reads its own options should not need this.
        """
        rest = {
            key: value for key, value in self._values.items() if key not in self._read
        }
        self._read.update(rest)
        return rest

    def unread(self) -> tuple[str, ...]:
        """The keys given that nothing read, sorted.

        The caller reports them: the device has finished building by then, so a key
        left over is one it does not understand.
        """
        return tuple(sorted(set(self._values) - self._read))

    def _parsed(self, key, default, parse, *, fallback_raw: str | None = None):
        """Read *key* through *parse*, falling back to *default* or *fallback_raw*.

        A fallback given as a raw string goes through *parse* like any other value,
        which is how a default directory is checked for safety too.
        """
        self._read.add(key)
        raw = self._values.get(key)
        if raw is None:
            if fallback_raw is None:
                return default
            raw = fallback_raw
            key = f"{key} (default)"

        try:
            return parse(raw)
        except ValueError as exc:
            raise ValueError(f"bad value for -o {key}: {exc}") from exc

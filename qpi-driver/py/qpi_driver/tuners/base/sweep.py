"""What one target's schedule swept, carried to its analysis (RFC 0009 §6.6).

A routine used to keep its setpoints on itself — ``self._frequencies`` written in
`build_schedule` and read back in `analyse`. That was correct while a routine ran one
target at a time, and silently wrong the moment it ran several: a sweep centred on a
per-qubit value has a different grid per target, so every fit in a fused group would
have used whichever target built last. Not an error — a plausible curve fitted against
the wrong axis, which is the failure this graph is most scarred by.

So the setpoints belong to the target, and the target's `Sweep` is threaded from the
build to the fit. Keyed by axis name rather than held as attributes because that is how
`escalating` reaches them: a guard refuses an axis by name, and the retry has to widen
that axis and no other.
"""

from collections.abc import Iterator
from typing import Any


class Sweep:
    """One target's swept setpoints, by axis name.

    Dict-like on purpose. A routine writes ``sweep["frequencies"] = ...`` where it used
    to write ``self._frequencies``, and reads it back the same way; nothing else about
    the routine changes.
    """

    __slots__ = ("target", "axes")

    def __init__(self, target: str, **axes: Any) -> None:
        self.target = target
        self.axes: dict[str, Any] = dict(axes)

    def __getitem__(self, axis: str) -> Any:
        try:
            return self.axes[axis]
        except KeyError:
            raise KeyError(
                f"{self.target} swept no {axis!r}; "
                f"it swept {', '.join(sorted(self.axes)) or 'nothing'}"
            ) from None

    def __setitem__(self, axis: str, setpoints: Any) -> None:
        self.axes[axis] = setpoints

    def __contains__(self, axis: str) -> bool:
        return axis in self.axes

    def __iter__(self) -> Iterator[str]:
        return iter(self.axes)

    def __len__(self) -> int:
        return len(self.axes)

    def get(self, axis: str, default: Any = None) -> Any:
        return self.axes.get(axis, default)

    def setdefault(self, axis: str, setpoints: Any) -> Any:
        return self.axes.setdefault(axis, setpoints)

    def __repr__(self) -> str:
        return f"Sweep({self.target!r}, {', '.join(sorted(self.axes))})"

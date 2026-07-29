"""A device module that raises ``ImportError`` while being imported.

The failure mode a real half-installed device has: the module itself is found, and
then something it imports is not. Python's error for that names the file it was
reading, which is what `discovery._without_paths` has to strip — so this exists to
produce a genuine one rather than a hand-written string (RFC 0003 §10).
"""

from qpi_driver import ThisNameDoesNotExist  # noqa: F401

"""The single-qubit Clifford group, for randomized benchmarking (RFC 0004 §6.7).

RB only measures anything if the recovery gate really does invert the sequence:
the whole protocol is "apply random Cliffords, undo them, see how much survives".
An approximate inverse turns the decay into a measurement of the inverse's own
error, which is why the group is built here by exact matrix composition rather
than by a lookup table with gaps.

The 24 elements are generated from ``X/2`` and ``Y/2``, identified up to global
phase, and each is decomposed into the ``Rxy(θ, φ)`` rotations both schedulers
provide natively.
"""

import random
from functools import lru_cache

import numpy as np

CLIFFORD_GROUP_SIZE = 24

#: A native rotation as ``(theta, phi)`` in **degrees**, to be played as ``Rxy``.
#: Degrees rather than radians because that is the unit both schedulers' ``Rxy``
#: takes, and this type exists only to be handed to it.
NativeGate = tuple[float, float]

_I = np.eye(2, dtype=complex)
_X90 = np.array(
    [
        [np.cos(np.pi / 4), -1j * np.sin(np.pi / 4)],
        [-1j * np.sin(np.pi / 4), np.cos(np.pi / 4)],
    ],
    dtype=complex,
)
_Y90 = np.array(
    [[np.cos(np.pi / 4), -np.sin(np.pi / 4)], [np.sin(np.pi / 4), np.cos(np.pi / 4)]],
    dtype=complex,
)

#: Generators, with the native rotation that plays each, in degrees.
_GENERATORS: tuple[tuple[np.ndarray, NativeGate], ...] = (
    (_X90, (90.0, 0.0)),
    (_Y90, (90.0, 90.0)),
)


def _canonical(matrix: np.ndarray) -> tuple:
    """A global-phase-independent key for *matrix*.

    Two unitaries that differ only by a global phase are the same operation on a
    qubit, so the group has 24 elements rather than 192. Dividing out the phase
    of the first significant entry is what collapses them.
    """
    flat = matrix.reshape(-1)
    significant = flat[np.argmax(np.abs(flat) > 1e-9)]
    normalised = matrix * np.exp(-1j * np.angle(significant))
    return tuple(np.round(normalised.reshape(-1), 6) + 0.0)


@lru_cache(maxsize=1)
def _group() -> tuple[list[np.ndarray], list[list[NativeGate]]]:
    """Build the group by breadth-first closure over the generators.

    Returns the 24 matrices and, for each, the shortest generator sequence that
    produces it — shortest because a longer decomposition would add gate error
    to every Clifford in every RB sequence.
    """
    matrices: list[np.ndarray] = [_I]
    decompositions: list[list[NativeGate]] = [[]]
    seen = {_canonical(_I): 0}

    frontier = [0]
    while frontier and len(matrices) < CLIFFORD_GROUP_SIZE:
        next_frontier: list[int] = []
        for index in frontier:
            for generator, native in _GENERATORS:
                product = generator @ matrices[index]
                key = _canonical(product)
                if key in seen:
                    continue
                seen[key] = len(matrices)
                matrices.append(product)
                decompositions.append(decompositions[index] + [native])
                next_frontier.append(len(matrices) - 1)
        frontier = next_frontier

    if len(matrices) != CLIFFORD_GROUP_SIZE:
        raise RuntimeError(
            f"Clifford closure produced {len(matrices)} elements, expected "
            f"{CLIFFORD_GROUP_SIZE}"
        )
    return matrices, decompositions


def clifford_matrix(index: int) -> np.ndarray:
    """The unitary of Clifford *index*."""
    matrices, _ = _group()
    return matrices[index % CLIFFORD_GROUP_SIZE]


def clifford_to_gates(clifford_index: int) -> list[NativeGate]:
    """Clifford *index* as a list of native ``(theta, phi)`` rotations."""
    _, decompositions = _group()
    return list(decompositions[clifford_index % CLIFFORD_GROUP_SIZE])


def _phase_normalised(matrix: np.ndarray) -> np.ndarray:
    """*matrix* with its global phase divided out."""
    flat = matrix.reshape(-1)
    significant = flat[np.argmax(np.abs(flat) > 1e-9)]
    return matrix * np.exp(-1j * np.angle(significant))


def _index_of(matrix: np.ndarray) -> int:
    """Which Clifford *matrix* is, up to global phase.

    Compared within a tolerance rather than by exact key: composing a long
    sequence accumulates floating-point error, and an exact match would start
    failing on the deep sequences RB depends on most.
    """
    matrices, _ = _group()
    target = _phase_normalised(matrix)
    for index, candidate in enumerate(matrices):
        if np.allclose(_phase_normalised(candidate), target, atol=1e-6):
            return index
    raise ValueError("matrix is not a single-qubit Clifford")


def compose(sequence: list[int]) -> np.ndarray:
    """The unitary of *sequence* applied left to right."""
    total = _I
    for index in sequence:
        total = clifford_matrix(index) @ total
    return total


def generate_clifford_sequence(
    length: int, rng: random.Random | None = None
) -> list[int]:
    """*length* uniformly random Clifford indices.

    Takes a :class:`random.Random` rather than a seed so that seeding is the
    caller's to control — RB needs many independent sequences, and a function
    that reseeds the global RNG per call would make them identical.
    """
    source = rng or random
    return [source.randrange(CLIFFORD_GROUP_SIZE) for _ in range(length)]


def compute_inverse_clifford(sequence: list[int]) -> int:
    """The Clifford that returns *sequence* to the identity.

    Exact: the sequence's product is composed and its conjugate transpose looked
    up in the group, which is closed under inversion.
    """
    return _index_of(compose(sequence).conj().T)


def sequence_with_recovery(sequence: list[int]) -> list[int]:
    """*sequence* followed by its recovery Clifford — what RB actually plays."""
    return [*sequence, compute_inverse_clifford(sequence)]

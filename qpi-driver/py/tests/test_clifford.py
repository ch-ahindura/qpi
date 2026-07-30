"""The single-qubit Clifford group (RFC 0004 §6.7).

RB measures nothing unless the recovery gate really inverts the sequence, so
these assert the group properties rather than a decomposition table: an
approximate inverse turns the decay into a measurement of the inverse's own
error.
"""

import random

import numpy as np
import pytest
from qpi_driver.tuners.utils.clifford import (
    CLIFFORD_GROUP_SIZE,
    _index_of,
    _phase_normalised,
    clifford_matrix,
    clifford_to_gates,
    compose,
    compute_inverse_clifford,
    generate_clifford_sequence,
    sequence_with_recovery,
)

IDENTITY = np.eye(2, dtype=complex)


def _is_identity(matrix: np.ndarray) -> bool:
    return np.allclose(_phase_normalised(matrix), IDENTITY, atol=1e-6)


def test_the_group_has_twenty_four_distinct_elements():
    matrices = [clifford_matrix(i) for i in range(CLIFFORD_GROUP_SIZE)]
    for i in range(CLIFFORD_GROUP_SIZE):
        for j in range(i + 1, CLIFFORD_GROUP_SIZE):
            assert not np.allclose(
                _phase_normalised(matrices[i]),
                _phase_normalised(matrices[j]),
                atol=1e-6,
            ), f"Cliffords {i} and {j} are the same operation"


def test_every_element_is_unitary():
    for index in range(CLIFFORD_GROUP_SIZE):
        matrix = clifford_matrix(index)
        assert np.allclose(matrix @ matrix.conj().T, IDENTITY, atol=1e-9)


def test_the_group_is_closed_under_multiplication():
    for a in range(CLIFFORD_GROUP_SIZE):
        for b in range(CLIFFORD_GROUP_SIZE):
            _index_of(clifford_matrix(a) @ clifford_matrix(b))


def test_every_element_has_an_inverse_in_the_group():
    for index in range(CLIFFORD_GROUP_SIZE):
        inverse = compute_inverse_clifford([index])
        assert _is_identity(clifford_matrix(inverse) @ clifford_matrix(index))


@pytest.mark.parametrize("depth", [1, 2, 5, 13, 40, 128])
def test_the_recovery_gate_returns_any_sequence_to_the_identity(depth):
    """The whole of RB rests on this being exact, not approximate."""
    rng = random.Random(depth)
    for _ in range(25):
        sequence = generate_clifford_sequence(depth, rng)
        assert _is_identity(compose(sequence_with_recovery(sequence)))


def test_a_decomposition_reproduces_its_element():
    """The native rotations must build the matrix they claim to."""
    for index in range(CLIFFORD_GROUP_SIZE):
        built = IDENTITY
        for theta, phi in clifford_to_gates(index):
            built = _rxy(theta, phi) @ built
        assert np.allclose(
            _phase_normalised(built),
            _phase_normalised(clifford_matrix(index)),
            atol=1e-9,
        ), f"decomposition of Clifford {index} does not reproduce it"


def _rxy(theta_deg: float, phi_deg: float) -> np.ndarray:
    """The rotation both schedulers' ``Rxy(theta, phi)`` performs, in degrees."""
    theta, phi = np.deg2rad(theta_deg), np.deg2rad(phi_deg)
    return np.array(
        [
            [np.cos(theta / 2), -1j * np.exp(-1j * phi) * np.sin(theta / 2)],
            [-1j * np.exp(1j * phi) * np.sin(theta / 2), np.cos(theta / 2)],
        ],
        dtype=complex,
    )


def test_decompositions_are_short():
    """Every extra rotation adds its own error to every Clifford in every sequence."""
    lengths = [len(clifford_to_gates(i)) for i in range(CLIFFORD_GROUP_SIZE)]
    assert max(lengths) <= 5
    assert lengths[0] == 0  # the identity plays nothing


def test_decompositions_are_in_degrees():
    """Both schedulers' Rxy takes degrees; radians would silently mis-rotate."""
    for index in range(CLIFFORD_GROUP_SIZE):
        for theta, phi in clifford_to_gates(index):
            assert theta in (90.0,)
            assert phi in (0.0, 90.0)


def test_generation_is_reproducible_from_a_seeded_source():
    assert generate_clifford_sequence(20, random.Random(4)) == (
        generate_clifford_sequence(20, random.Random(4))
    )


def test_generation_uses_the_given_source_not_the_global_rng():
    """Many independent sequences are needed; reseeding globally makes them identical."""
    random.seed(1)
    first = generate_clifford_sequence(30, random.Random(99))
    random.seed(1)
    second = generate_clifford_sequence(30, random.Random(99))
    assert first == second


def test_sequences_only_contain_valid_indices():
    sequence = generate_clifford_sequence(200, random.Random(0))
    assert all(0 <= index < CLIFFORD_GROUP_SIZE for index in sequence)


def test_a_non_clifford_matrix_is_rejected():
    with pytest.raises(ValueError, match="not a single-qubit Clifford"):
        _index_of(_rxy(37.0, 0.0))

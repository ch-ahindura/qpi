"""Coupling graphs to group against, by topology (RFC 0009 §10.2).

Each returns edge names, which is what `couplings_of` reads and what a device config
holds. Parameterised by size so a test can assert that a group count depends on the
topology and not on how big it is — the claim the whole RFC rests on.
"""


def chain(qubits: int) -> list[str]:
    """A line of *qubits*, as every small bring-up chip is."""
    return [f"q{i}_q{i + 1}" for i in range(qubits - 1)]


def lattice(side: int) -> list[str]:
    """A square grid, ``side`` by ``side`` — degree 4 in the interior."""
    edges = [
        f"q{row * side + column}_q{row * side + column + 1}"
        for row in range(side)
        for column in range(side - 1)
    ]
    edges += [
        f"q{row * side + column}_q{(row + 1) * side + column}"
        for row in range(side - 1)
        for column in range(side)
    ]
    return edges


def heavy_hex() -> list[str]:
    """Three fused hexagons — degree at most 3, as the larger vendors' chips are."""
    return [
        "q0_q1", "q1_q2", "q2_q3", "q3_q4", "q4_q5", "q5_q0",
        "q2_q6", "q6_q7", "q7_q8", "q8_q9", "q9_q3",
        "q5_q10", "q10_q11", "q11_q12", "q12_q13", "q13_q0",
    ]  # fmt: skip


def qubits_of(edges: list[str]) -> list[str]:
    """Every qubit the *edges* name, in index order."""
    names = {qubit for edge in edges for qubit in edge.split("_")}
    return sorted(names, key=lambda name: int(name[1:]))

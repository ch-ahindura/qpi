"""A device the QPI driver SDK does not ship (RFC 0003 §6).

Two things are defined here, and either one is enough on its own:

``QuantumXExecutor``
    An :class:`~qpi_driver.executors.Executor`. Nothing registers it — for a
    ``process`` device, being an executor *is* being a device, so it can be run
    straight away by import path::

        qpi-driver start --operation process --device mylab_devices:QuantumXExecutor \\
          --token "$QPI_ACCESS_TOKEN" --ca-fingerprint "$QPI_CA_FINGERPRINT" \\
          -o data_dir=./bin/data -o qubit_count=4

    ``qubit_count`` is not an option this SDK has heard of. A device named by
    import path has no declared schema, so undeclared options are passed to the
    executor's constructor as the strings they were typed as.

``QUANTUM_X``
    A :class:`~qpi_driver.builtins.DeviceSpec`, which is the same executor plus a
    name, a summary and a declared option schema. Being described as data is what
    earns it a line in ``--help``, an entry in ``catalog --json``, and checked and
    converted ``-o`` values — ``qubit_count`` arrives as an ``int``, and a typo in
    it is an error rather than a surprise.

Ship the spec by advertising it in your own ``pyproject.toml``::

    [project.entry-points."qpi_driver.devices"]
    quantum_x = "mylab_devices:QUANTUM_X"

Then ``pip install mylab-devices`` and it is simply there: in ``--help``, in
``catalog --json``, and to ``--device quantum_x``, with nothing to change in the
SDK or the CLI.
"""

from typing import Any

import numpy as np
import xarray as xr

from qpi_driver import Executor, JobPayload, OptionSpec
from qpi_driver.builtins.qpu import device_spec


class QuantumXExecutor(Executor):
    """Stands in for whatever your lab actually runs a job on.

    Every keyword argument the QPU driver was given reaches this constructor, so
    an executor declares its settings the way any other class does. ``name`` and
    ``data_dir`` always arrive; the rest are the ``-o`` options.
    """

    def __init__(
        self,
        name: str = "quantum_x",
        qubit_count: int = 1,
        **options: Any,
    ) -> None:
        super().__init__(name=name)
        self.qubit_count = int(qubit_count)

    def execute(self, payload: JobPayload) -> xr.Dataset:
        """Run one job and return what the hardware produced, as an ``xr.Dataset``.

        The dataset is the contract between this method and
        :meth:`process_result`, and it is the shape the built-in executors return
        too — so it is a dataset rather than a convenient dict even for a machine
        as imaginary as this one. Here every shot lands in the ground state; a real
        executor returns its own measurement record.
        """
        ground_state = "0" * self.qubit_count
        return xr.Dataset(
            {"memory": ("shot", np.full(payload.shots, ground_state))},
            coords={"shot": np.arange(payload.shots)},
            attrs={
                "shots": payload.shots,
                "n_qubits": self.qubit_count,
                "backend": self.name,
            },
        )

    def process_result(self, dataset: xr.Dataset, job_id: str) -> dict[str, Any]:
        """Turn that into the Qiskit-shaped counts QPI-UI stores."""
        states, counts = np.unique(dataset["memory"].values, return_counts=True)
        return {
            "job_id": job_id,
            "counts": {str(state): int(count) for state, count in zip(states, counts)},
            "shots": int(dataset.attrs["shots"]),
            "backend": dataset.attrs["backend"],
            "success": True,
        }


# The same executor, described as data. `device_spec` is the QPU module's helper:
# every process device is the one built-in QPU driver over a different executor.
QUANTUM_X = device_spec(
    "quantum_x",
    executor=QuantumXExecutor,
    summary="MyLab's QuantumX control system, as an example of a custom device.",
    options=(
        OptionSpec(
            key="qubit_count",
            help="How many qubits the chip has.",
            parse=int,
            default="1",
            example="4",
        ),
    ),
)

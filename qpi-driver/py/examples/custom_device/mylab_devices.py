"""A device the QPI driver SDK does not ship (RFC 0003 §6).

Two things are defined here, and either one is enough on its own:

``ThermometerExecutor``
    An :class:`~qpi_driver.executors.Executor`. Nothing registers it — for a
    ``process`` device, being an executor *is* being a device, so it can be run
    straight away by import path::

        qpi-driver start --operation process --device mylab_devices:ThermometerExecutor \\
          --token "$QPI_ACCESS_TOKEN" --ca-fingerprint "$QPI_CA_FINGERPRINT" \\
          -o data_dir=./bin/data -o probe_count=4

    ``probe_count`` is not an option this SDK has heard of. A device named by
    import path has no declared schema, so undeclared options are passed to the
    executor's constructor as the strings they were typed as.

``THERMOMETER``
    A :class:`~qpi_driver.builtins.DeviceSpec`, which is the same executor plus a
    name, a summary and a declared option schema. Being described as data is what
    earns it a line in ``--help``, an entry in ``catalog --json``, and checked and
    converted ``-o`` values — ``probe_count`` arrives as an ``int``, and a typo in
    it is an error rather than a surprise.

Ship the spec by advertising it in your own ``pyproject.toml``::

    [project.entry-points."qpi_driver.devices"]
    thermometer = "mylab_devices:THERMOMETER"

Then ``pip install mylab-devices`` and it is simply there: in ``--help``, in
``catalog --json``, and to ``--device thermometer``, with nothing to change in the
SDK or the CLI.
"""

from typing import Any

from qpi_driver import Executor, JobPayload, OptionSpec
from qpi_driver.builtins.qpu import device_spec


class ThermometerExecutor(Executor):
    """Stands in for whatever your lab actually runs a job on.

    Every keyword argument the QPU driver was given reaches this constructor, so
    an executor declares its settings the way any other class does. ``name`` and
    ``data_dir`` always arrive; the rest are the ``-o`` options.
    """

    def __init__(
        self,
        name: str = "thermometer",
        probe_count: int = 1,
        **options: Any,
    ) -> None:
        super().__init__(name=name)
        self.probe_count = int(probe_count)

    def execute(self, payload: JobPayload) -> Any:
        """Run one job and return whatever your hardware produced."""
        return {"probes": self.probe_count, "job": payload.id}

    def process_result(self, dataset: Any, job_id: str) -> dict[str, Any]:
        """Turn that into the Qiskit-shaped counts QPI-UI stores."""
        return {"counts": {"0": dataset["probes"]}, "job_id": job_id}


# The same executor, described as data. `device_spec` is the QPU module's helper:
# every process device is the one built-in QPU driver over a different executor.
THERMOMETER = device_spec(
    "thermometer",
    executor=ThermometerExecutor,
    summary="Reads a made-up thermometer, as an example of a custom device.",
    options=(
        OptionSpec(
            key="probe_count",
            help="How many probes to read per job.",
            parse=int,
            default="1",
            example="4",
        ),
    ),
)

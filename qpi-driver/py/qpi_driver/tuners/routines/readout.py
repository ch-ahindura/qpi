"""Calibrating the readout chain itself (RFC 0005 §7).

The routines in `spectroscopy` find the resonator and how hard to drive it. This one
answers the question that comes after: given the two clouds the readout produces,
which line separates them?

That line is `measure.acq_rotation` and `measure.acq_threshold`, and until now
nothing produced them. They default to zero, the reference device config does not
carry them at all, and **every** ``meas_level=2`` shot is assigned by comparing a
rotated real part against them — so the default job path was discriminating with an
uncalibrated rule. Zero is correct only for a readout chain that happens to place the
clouds either side of the imaginary axis, which nothing arranges.

This needs the state-resolved readout the simulator gained alongside it: with two
clouds placed by hand there was no rotation to find, because the placement *was* the
answer.
"""

from typing import Any

import numpy as np
import xarray as xr

from qpi_driver.tuners.base.backend import SchedulerBackend
from qpi_driver.tuners.base.config import RoutineConfig
from qpi_driver.tuners.base.device import write_path
from qpi_driver.tuners.base.routines import (
    CalibrationRoutine,
    CheckOutcome,
    RoutineError,
)
from qpi_driver.tuners.fitting import fit_readout_discrimination


class ReadoutDiscrimination(CalibrationRoutine):
    """Prepare ``|0>`` and ``|1>``, then find the line that tells them apart.

    Depends on `rabi` rather than sitting with the other readout routines, and that
    is the ordering point RFC 0005 §1 makes: preparing ``|1>`` needs a calibrated π
    pulse, so the readout chain cannot be finished before the qubit chain starts. It
    straddles it.

    Reports the assignment fidelity from the same shots rather than leaving it to a
    separate node. Splitting them would mean measuring the same two clouds twice, and
    the fidelity is exactly what says whether the fitted line is any good.
    """

    name = "readout_discrimination"
    depends_on = ("rabi",)
    updates = ("measure.acq_rotation", "measure.acq_threshold")

    def build_schedule(
        self, target: str, device: Any, config: RoutineConfig, backend: SchedulerBackend
    ) -> Any:
        shots = int(config.get("shots", 2000))
        schedule = backend.new_schedule(f"{self.name}", repetitions=shots)
        # Single shots, not an average: the whole measurement is the *distribution* of
        # each cloud, and its width is what sets the threshold and the fidelity. An
        # averaged acquisition gives two points and no way to say how often they are
        # confused.
        for index, prepare in enumerate((0, 1)):
            schedule.add(backend.Reset(target))
            if prepare:
                schedule.add(backend.X(target))
            schedule.add(
                backend.Measure(
                    target, acq_index=index, bin_mode=backend.BinMode.APPEND
                )
            )
        return schedule

    def analyse(
        self, dataset: xr.Dataset, target: str, device: Any, config: RoutineConfig
    ) -> dict[str, Any]:
        ground, excited = _shot_clouds(dataset)
        return fit_readout_discrimination(ground, excited)

    def apply(self, device: Any, target: str, params: dict[str, Any]) -> None:
        element = device.get_element(target)
        write_path(element, "measure.acq_rotation", params["acq_rotation"])
        write_path(element, "measure.acq_threshold", params["acq_threshold"])

    #: Assignment fidelity below which the discriminator counts as stale. A readout
    #: that has drifted far enough to misassign one shot in twenty is worth
    #: re-fitting; tighter than this and normal shot noise on a few thousand shots
    #: would trip it.
    CHECK_MIN_FIDELITY = 0.95

    def build_check_schedule(
        self, target: str, device: Any, config: RoutineConfig, backend: SchedulerBackend
    ) -> Any:
        """The same experiment with fewer shots — the one case where that is right.

        A check is normally a *different* experiment because a cheaper version of the
        calibration usually cannot answer the question. Here it can: the question is
        "how often does the stored line misassign a shot?", and that is answered by
        counting misassignments over a few hundred shots rather than the few thousand
        needed to place the line precisely.
        """
        return self.build_schedule(
            target,
            device,
            RoutineConfig(params={"shots": int(config.get("check_shots", 400))}),
            backend,
        )

    def analyse_check(
        self, dataset: xr.Dataset, target: str, device: Any, config: RoutineConfig
    ) -> CheckOutcome:
        ground, excited = _shot_clouds(dataset)
        fitted = fit_readout_discrimination(ground, excited)
        floor = float(config.get("check_min_fidelity", self.CHECK_MIN_FIDELITY))
        fidelity = float(fitted["assignment_fidelity"])
        return CheckOutcome(
            passed=fidelity >= floor,
            margin=(1.0 - fidelity) / max(1.0 - floor, 1e-9),
            detail=f"assignment fidelity {fidelity:.4f}",
        )


def _shot_clouds(dataset: Any) -> tuple[np.ndarray, np.ndarray]:
    """The ``|0>`` and ``|1>`` shot clouds, as complex arrays.

    `signal_of` is the wrong reducer: it takes magnitudes and flattens, and both the
    rotation and the threshold live in the complex plane. Nor can the two clouds be
    flattened together — which shot belongs to which prepared state is the entire
    content of the measurement.
    """
    data_vars = getattr(dataset, "data_vars", None)
    if data_vars is None or not list(data_vars):
        raise RoutineError("the discrimination acquisition returned no data variables")

    values = np.asarray(dataset[list(data_vars)[0]].values)
    if values.ndim < 2 or values.shape[-1] < 2:
        raise RoutineError(
            f"expected single shots of two prepared states, got shape {values.shape} "
            "— the acquisition was averaged rather than appended, so the width of "
            "each cloud is gone and no threshold can be placed"
        )
    return values[..., 0].reshape(-1), values[..., 1].reshape(-1)

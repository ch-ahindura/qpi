"""Write a `quantify.device.yml` that matches the simulated chip.

The checked-in fixture is deliberately *mis*calibrated — its ``f01`` is 214 MHz
from where the simulated transmon actually is — because the calibration tests
exist to find the qubit. That is the wrong starting point for the dashboard's
e2e: a job console driven by an uncalibrated chip returns wrong counts for every
circuit, which looks like a broken dashboard rather than an uncalibrated one.

So this writes the same fixture with the parameters a successful calibration
would have written, taken from the simulator itself. Jobs submitted through the
dashboard then return what the circuit actually means.

Usage:
    python simulated_device.py <output.yml>
"""

import shutil
import sys
from pathlib import Path

import yaml

FIXTURES = Path(__file__).resolve().parents[1] / "qpi-driver/py/tests/fixtures"


def _calibrate_edge(name: str, element: dict, pair) -> None:
    """Give an edge a working CZ, by whichever mechanism it uses.

    Two edge types, two gates. A `FluxTunableCoupler` drives its coupler at
    microwave frequency and a sideband bridges ``|11⟩``–``|02⟩``; a stock
    `CompositeSquareEdge` puts DC flux on a qubit's own port and walks it onto
    the crossing. They need different amplitudes, durations and drive settings.

    Both need the two ``<qubit>_phase_correction`` values. The single-qubit
    phase a CZ leaves behind is large — an uncorrected gate is conditional-phase
    correct and still builds the wrong Bell state — so a device without them is
    not calibrated, and the dashboard's default circuit is a Bell state.
    """
    from qpi_driver.simulation import GHZ

    parent, child = name.split("_", 1)
    is_coupler = "flux_tunable_coupler" in str(
        (element.get("element_type") or {}).get("path", "")
    )

    if is_coupler:
        amplitude = 0.554
        drive, duration = pair.parametric_operating_point(amplitude)
        duration = round(duration)
        phases = pair.parametric_phases(amplitude, drive, duration)
        element["clock_freqs"] = {"cz": float(drive * GHZ)}
    else:
        amplitude = float(pair.resonant_amplitude)
        duration = round(pair.cz_duration_ns)
        phases = pair.dc_phases(amplitude, duration)

    element["cz"] = {
        "square_amp": float(amplitude),
        # Whole nanoseconds: the compiler plays on a 1 ns grid and refuses
        # anything else.
        "square_duration": float(duration) * 1e-9,
        f"{parent}_phase_correction": float(phases["parent"]),
        f"{child}_phase_correction": float(phases["child"]),
    }


def _calibrate_discriminator(qubit: str, element: dict, simulator) -> None:
    """Give the element the readout line that separates its two clouds.

    Without this the dashboard's job console reads every shot as ``|1>``. The two
    clouds land wherever the simulated amplifier chain puts them, and
    ``acq_rotation``/``acq_threshold`` default to zero — a rule that is only right for
    a chain straddling the imaginary axis, which this one does not. That is the honest
    state of an uncalibrated chip and the wrong starting point for an e2e whose
    subject is the dashboard.

    Computed the way `readout_discrimination` measures it: rotate so the two centres
    separate along the real axis, then take the midpoint.
    """
    import numpy as np

    measure = element.setdefault("measure", {})
    readout = float((element.get("clock_freqs") or {}).get("readout") or 0.0)
    if readout <= 0:
        return
    resonator = simulator.resonator(qubit, configured_ghz=readout / 1e9)
    amplitude = float(measure.get("pulse_amp", 0.25))
    # Per unit amplitude, as the coordinator's own chain is.
    chain = (
        simulator.readout_gain
        * amplitude
        * np.exp(1j * np.deg2rad(simulator.readout_phase_deg))
    )
    ground, excited = (
        chain * resonator.reflection(readout / 1e9, amplitude, level)
        for level in (0, 1)
    )
    # Into [0, 360): the instrument refuses a negative rotation, and it refuses it in
    # every schedule that follows rather than where it was written.
    rotation = float(np.angle(excited - ground, deg=True) % 360.0)
    turn = np.exp(-1j * np.deg2rad(rotation))
    measure["acq_rotation"] = rotation
    measure["acq_threshold"] = float(
        0.5 * ((ground * turn).real + (excited * turn).real)
    )


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2

    from qpi_driver.simulation import GHZ, TransmonSimulator

    destination = Path(sys.argv[1])
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURES / "quantify.device.yml", destination)

    from qpi_driver.simulation.coupled import CoupledTransmons

    simulator = TransmonSimulator()
    pair = CoupledTransmons()
    config = yaml.safe_load(destination.read_text())

    for name, element in config.items():
        if not isinstance(element, dict):
            continue
        if "_" in name:
            _calibrate_edge(name, element, pair)
            continue
        clocks = element.get("clock_freqs")
        if not isinstance(clocks, dict):
            continue
        clocks["f01"] = float(simulator.f01 * GHZ)
        clocks["f12"] = float((simulator.f01 + simulator.anharmonicity) * GHZ)
        # The drive strength the simulated instrument applies puts a pi rotation
        # at 0.2; this is the number Rabi would have found.
        element.setdefault("rxy", {})["amp180"] = 0.2
        _calibrate_discriminator(name, element, simulator)

    destination.write_text(yaml.safe_dump(config, sort_keys=False))
    print(f"[e2e] wrote calibrated device config to {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

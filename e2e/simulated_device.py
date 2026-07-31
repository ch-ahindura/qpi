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

    destination.write_text(yaml.safe_dump(config, sort_keys=False))
    print(f"[e2e] wrote calibrated device config to {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

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


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2

    from qpi_driver.simulation import GHZ, TransmonSimulator

    destination = Path(sys.argv[1])
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURES / "quantify.device.yml", destination)

    simulator = TransmonSimulator()
    config = yaml.safe_load(destination.read_text())

    for name, element in config.items():
        if "_" in name or not isinstance(element, dict):
            continue  # an edge, not a qubit
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

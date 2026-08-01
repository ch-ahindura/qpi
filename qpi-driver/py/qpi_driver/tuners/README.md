# Calibration Tuners

Superconducting transmon qubits drift. Frequencies, pulse amplitudes and
coherence times move on timescales of minutes to hours, so parameters that were
right this morning are approximately right by lunchtime and wrong by evening.

The `calibrate` operation is how a QPI node keeps up: it runs calibration
experiments against the chip, fits the results, and writes the fitted parameters
back to the same `quantify.device.yml` the `process` driver reads for every job.

Design and rationale: [RFC 0004](https://github.com/sopherapps/qpi/blob/main/docs/rfcs/0004-calibration-tuners.md).

## Running one

```bash
qpi-driver start --operation calibrate --device quantify_tuner \
    -o calibration_config=./calibration.yml \
    -o quantify_device_config=./quantify.device.yml \
    -o quantify_hardware_config=./quantify.hardware.json \
    -o is_dummy=true
```

Install the extra that matches the device: `qpi-driver[cli,quantify_tuner]` or
`qpi-driver[cli,qblox_tuner]`.

A tuner registers as its own driver, separate from the QPU driver on the same
node — as a cryostat monitor does. The two share the device YAML through the
filesystem and nothing else.

### Options

| Option | Default | Meaning |
|--------|---------|---------|
| `calibration_config` | `./calibration.yml` | Which routines run, over what, and how far. |
| `quantify_device_config` | `./quantify.device.yml` | The device calibration file, read at start and written back. |
| `quantify_hardware_config` | `./quantify.hardware.json` | Hardware connectivity. |
| `is_dummy` | `false` | Run against the vendor's dummy cluster. Compiles and runs; every acquisition is `nan`, so every routine fails. |
| `is_simulated` | `false` | Run against a simulated chip instead — see below. Both tuners. |
| `spi_rack_address` | *(none)* | Serial address of the SPI rack holding the couplers' S4g current sources. Only `coupler_anticrossing` uses it, and only when the couplers say `bias.source: spi`; without it that node declines rather than sweeping a bias it cannot hold. |
| `drift_check_interval` | `0` | Seconds between periodic benchmark runs. `0` disables them. |
| `fidelity_threshold` | `0.999` | 1Q fidelity below which a recalibration is triggered. |
| `fidelity_2q_threshold` | `0.99` | 2Q fidelity below which a recalibration is triggered. |

All of them have working defaults except the calibration config, which must
exist: a tuner with no config would have nothing to run, and a run that does
nothing raises nothing, so it would report success.

### What the graph needs from a device file

Every node runs against a stock `BasicTransmonElement` except the ones whose result
has nowhere to go. Those *decline* rather than fail, so a chip configured before
RFC 0005 calibrates everything it can and reports success rather than a column of
red for parameters its elements were never going to have:

| Needs | Nodes that decline without it |
|-------|-------------------------------|
| `spec` on a `CalibratedTransmon` | none — `qubit_spectroscopy` still sweeps power, it just does not remember the answer |
| `measure_2state` | `readout_operating_point` |
| `r12` | `rabi_12`, `resonator_spectroscopy_second_excited` |
| `measure_3state` | `ramsey_12`, `drag_12`, `fine_amplitude_12`, `three_state_operating_point`, `three_state_discrimination` |
| `bias.parking_current` **and** a rack that can hold it | `coupler_anticrossing` |
| `clock_freqs.cz` on the edge | `cz_spectroscopy`, `cz_parametrization` — a `CompositeSquareEdge` has no drive frequency, and `cz_chevron` is its counterpart |

Point an element at
`qpi_driver.executors.quantify.elements.calibrated_transmon.CalibratedTransmon` (or the
`qblox` twin) to opt that qubit into the rest.

## A node with no hardware

`-o is_simulated=true` puts a simulated chip where the cluster goes. The
compiled schedule is played through a transmon built from a real Hamiltonian
(`scqubits`) and evolved with the Lindblad master equation (`qutip`), so the
routines fit real physics and the calibration written back is a calibration of
*something*.

```bash
pip install 'qpi-driver[cli,quantify_tuner]' scqubits qutip

qpi-driver start --operation calibrate --device quantify_tuner \
    -o calibration_config=./calibration.yml -o is_simulated=true
```

The `process` driver takes the same option, and that is the point — point both
at the same `quantify.device.yml` and the whole node runs with no instruments:

```bash
qpi-driver start --operation process --device quantify -o is_simulated=true
```

Use it to rehearse a bring-up, to develop against, or to check a change to a
routine before taking the cryostat down for it. It is not a substitute for
hardware — see the limits below.

**Not the same as `is_dummy`.** The vendor's dummy cluster compiles and runs a
schedule and then returns `nan` for every acquisition, so nothing distinguishes
a correct calibration from a wrong one. The simulator reads the schedule it was
given, so an `amp180` off by a factor of ten produces the wrong population,
measured.

**What it models, and what it does not.** One- and two-qubit gates, clock
detuning, relaxation and dephasing, and a readout discriminated from two IQ
blobs. A CZ is a flux pulse walking the pair through the `|11>`-`|02>` avoided
crossing, so the conditional phase is integrated rather than asserted and a Bell
state comes out entangled. Both mechanisms the hardware has: DC flux on a qubit's
own port, and a parametric drive on a coupler, where the drive *frequency* is the
resonance condition rather than a carrier detail.

Not modelled: crosstalk and any readout chain beyond the blobs. The qubits are
all the same simulated transmon, so it will never show you a chip whose qubits
differ — which on real hardware is most of what calibration is for.

**Both schedulers.** quantify reaches its hardware through an instrument
coordinator, which the simulator replaces outright. qblox reaches it through a
`HardwareAgent` that compiles and runs in one call — but `compile` needs no
instrument, so a real agent keeps compilation and only execution is simulated.
A schedule that would not assemble for a cluster does not assemble here either.

**The two-qubit model is weaker than the one-qubit one.** A transmon's levels
come from diagonalising a real Cooper-pair-box Hamiltonian: nothing in it was
chosen. The coupler's exchange strength, its flux-to-frequency curve and the transition
its parametric drive bridges *were* chosen, because this project has no device to
measure them from — though an edge that has characterised its own gap can declare
it (`clock_freqs.sideband_gap`) and be simulated against that instead. The dynamics
they produce are real and a routine still has to find a crossing it was not told
the location of, but read a two-qubit result as "against a plausible coupler",
not "against a real one".

Only `quantify_tuner` and the `quantify` executor support it. qblox-scheduler
reaches its hardware through a different interface, and passing the flag there
is an error rather than a silent fallback.

## The calibration graph

Routines declare what they depend on, and a full calibration is a topological
walk of that graph. Nothing else encodes the order.

```
resonator_spectroscopy → resonator_punchout → qubit_spectroscopy → rabi
  rabi → ramsey → drag → { allxy, fine_amplitude → rb }
  rabi → { t1, t2_echo }
  qubit_spectroscopy → flux_spectroscopy
  { rb, flux_spectroscopy } → cz_chevron → conditional_phase → interleaved_rb
```

| Routine | Writes | Notes |
|---------|--------|-------|
| `resonator_spectroscopy` | `clock_freqs.readout` | Lorentzian fit over a readout sweep. |
| `resonator_punchout` | `measure.pulse_amp`, `clock_freqs.readout` | Highest power still in the dressed regime — and the resonance *at* that power, since choosing a power moves it. |
| `qubit_spectroscopy` | `clock_freqs.f01` | Two-tone, weak drive. |
| `rabi` | `rxy.amp180` | π amplitude is half the oscillation period. |
| `ramsey` | `clock_freqs.f01` | Fine frequency, plus T2*. |
| `t1`, `t2_echo` | — | Characterisation; nothing is tuned from them. |
| `drag` | `rxy.motzoi` | Zero crossing of X90-Y180 against Y90-X180. |
| `allxy` | — | 21-pair diagnostic against the ideal staircase. |
| `fine_amplitude` | `rxy.amp180` | Amplifies a small error over repeated π pulses. |
| `rb`, `interleaved_rb`, `allxy_check` | — | Benchmarks. Their fidelity is what the drift check reads. |
| `flux_spectroscopy` | — | Coupler frequency against flux. |
| `cz_chevron` | `cz.amp`, `cz.duration` | Population transfer maximum. |
| `conditional_phase` | `cz.phase_correction` | Brings the conditional phase to π. |

Two-qubit routines target edges rather than qubits, and need `target_edges` and
a flux-tunable coupler. They are disabled in the shipped example config, because
on a chip without one they are pure failure noise.

## Configuring it

See [`calibration.example.yml`](https://github.com/sopherapps/qpi/blob/main/qpi-driver/py/calibration.example.yml). The shortest
useful file is just `target_qubits` — every routine is enabled unless it says
otherwise, and every sweep has a default.

Two rules are worth knowing because they are the opposite of what a config file
usually does:

- **A routine the file does not mention runs.** The alternative makes an empty
  file mean "calibrate nothing", and since that raises nothing it reports
  success — the worst answer a calibration can give.
- **A routine name that matches no routine is a startup error.** A typo like
  `rabi_oscillations` for `rabi` would otherwise enable nothing and leave the
  real routine at its default, with nothing in the run mentioning it.

## Drift monitoring

With `-o drift_check_interval=1800` the driver runs the benchmarks every half
hour. If a fidelity falls below its threshold — the two-qubit one for an edge,
the one-qubit one for a qubit — it queues a partial recalibration rather than a
full run.

A partial run is narrower in two ways. It targets only the affected qubits and
the edges touching them, and it runs only `qubit_spectroscopy` and the routines
downstream of it. The readout chain is skipped: finding the resonator is a
bring-up step rather than a drift one, and it is most of what makes a full
calibration long. So a drift the qubit routines cannot fix needs a full
calibration, and that is your call, not the driver's.

## What happens to your device file

The write-back is the part worth understanding, because a tuner writes what
every subsequent job reads.

- Nothing is written unless at least one routine produced a result. A failed run
  leaves the file exactly as it was.
- The candidate is written to a temporary file, checked that it reads back as
  written and is shaped as the loader expects, and only then moved into place.
- The previous file is kept as `quantify.device.yml.prev`, so a calibration that
  makes things worse can be undone without a second run:
  `qpi_driver.tuners.utils.persistence.restore_backup`.
- A fitted value outside the sweep that produced it is treated as a failed fit,
  not as a new parameter.

## Adding a tuner

A tuner is to `calibrate` what an executor is to `process`. Subclass `Tuner`,
supply a `SchedulerBackend` and a device, and name it by import path:

```bash
qpi-driver start --operation calibrate --device mylab.tuners:MyTuner …
```

The routines are backend-agnostic — both supported schedulers expose the same
gate vocabulary, so one routine set serves both, and `tuners/base/device.py`
absorbs the differences that remain. A new backend supplies the schedule class
and a way to run one; it does not reimplement the experiments.

## Status

The routines build real swept schedules and compile against both schedulers'
dummy clusters. The fits are checked three ways: against synthetic data, against
a physics simulator (`make test-py-sim`, which builds the transmon from a real
Hamiltonian with scqubits and integrates the master equation with qutip), and —
for randomized benchmarking — against sequences composed from this package's own
Clifford decomposition and evolved as unitaries.

`make test-py-sim` also runs a whole calibration against that simulator: a full
run, a partial one, a drift check and the recalibration it queues, and the
write-back. Two faults came out of it — a benchmark fit that reported the same
fidelity for every chip better than about 3% error per Clifford, and a partial
recalibration that quietly ran the full graph — neither of which any test of a
single routine could see.

Two gaps remain, and they are the ones that matter:

- The simulator supplies the acquisition rather than interpreting the compiled
  schedule, so a schedule that does not produce the physics its fit assumes
  would still pass.
- Nothing here has been run against physical hardware. The manual verification
  in RFC 0004 §9 is what remains before this is trustworthy in a lab.

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
| `is_dummy` | `false` | Run against the vendor's dummy cluster. |
| `drift_check_interval` | `0` | Seconds between periodic benchmark runs. `0` disables them. |
| `fidelity_threshold` | `0.999` | 1Q fidelity below which a recalibration is triggered. |
| `fidelity_2q_threshold` | `0.99` | 2Q fidelity below which a recalibration is triggered. |

All of them have working defaults except the calibration config, which must
exist: a tuner with no config would have nothing to run, and a run that does
nothing raises nothing, so it would report success.

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
| `resonator_punchout` | `measure.pulse_amp` | Highest power still in the dressed regime. |
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
the one-qubit one for a qubit — it queues a partial recalibration of the
affected qubits and their dependent routines, rather than a full run.

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
dummy clusters, and the fits recover known parameters from synthetic data. They
have not been validated against physical hardware — the manual verification
steps in RFC 0004 §9 are what remain before this is trustworthy in a lab.

# Calibration Tuners

Superconducting transmon qubits drift. Frequencies, pulse amplitudes and
coherence times move on timescales of minutes to hours, so parameters that were
right this morning are approximately right by lunchtime and wrong by evening.

The `calibrate` operation is how a QPI node keeps up: it runs calibration
experiments against the chip, fits the results, and writes the fitted parameters
back to the same `quantify.device.yml` the `process` driver reads for every job.

Design and rationale:
[RFC 0004](https://github.com/sopherapps/qpi/blob/main/docs/rfcs/0004-calibration-tuners.md)
for the machinery,
[RFC 0005](https://github.com/sopherapps/qpi/blob/main/docs/rfcs/0005-calibration-graph-completion.md)
for the rest of the graph.

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
| `quantify_device_config` | `./quantify.device.yml` | The device calibration file, re-read at the start of each calibration and written back. |
| `quantify_hardware_config` | `./quantify.hardware.json` | Hardware connectivity. Changing it needs a restart; the tuner warns and keeps running on the one it started with. |
| `is_dummy` | `false` | Run against the vendor's dummy cluster. Compiles and runs; every acquisition is `nan`, so every routine fails. |
| `is_simulated` | `false` | Run against a simulated chip instead — see below. Both tuners. |
| `spi_rack_address` | *(none)* | Serial address of the SPI rack holding the couplers' S4g current sources. Only `coupler_anticrossing` uses it, and only when the couplers say `bias.source: spi`; without it that node fails rather than sweeping a bias it cannot hold. |
| `save_raw_data` | `false` | `qblox_tuner` only: keep each routine's acquisition and an instrument snapshot under `--data-dir`. Off because nothing here reads them back and nothing prunes them. |
| `drift_check_interval` | `0` | Seconds between periodic benchmark runs. `0` disables them. |
| `fidelity_threshold` | `0.999` | 1Q fidelity below which a recalibration is triggered. |
| `fidelity_2q_threshold` | `0.99` | 2Q fidelity below which a recalibration is triggered. |

All of them have working defaults except the calibration config, which must
exist: a tuner with no config would have nothing to run, and a run that does
nothing raises nothing, so it would report success.

Where a tuner writes is `--data-dir` rather than an `-o`, because it is the same
directory for every driver on the node; a systemd install sets it, and the config paths
above, to `/var/qpi-driver/<service-name>`. It reaches each scheduler's own data-dir
global — quantify-core's `set_datadir`, qblox-scheduler's `OutputDirectoryManager` —
which otherwise default to `<cwd>/data` (`/data` for a service) and `~/qblox_data`.

Neither tuner keeps its acquisitions: they come back from the scheduler in memory, get
fitted, and what is kept is the fitted parameter in the device YAML. qblox-scheduler
would save a `dataset.hdf5` and an instrument snapshot per schedule — one per routine
per target, 13 per coupler edge — and is told not to unless `-o save_raw_data=true`;
quantify-scheduler has no equivalent, so that option does nothing for `quantify_tuner`.
What lands in the directory either way is whatever a scheduler was separately asked for:
a hardware config with `sequence_to_file: true`, a hardware-log download, a diagnostics
report.

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
| `bias.parking_current` on the edge | `coupler_anticrossing` — and with one but no rack to deliver it, that node *fails* rather than declining: an edge declaring a bias nothing can hold is a misconfiguration, not an absent feature |
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
pip install 'qpi-driver[cli,quantify_tuner,sim]'

qpi-driver start --operation calibrate --device quantify_tuner \
    -o calibration_config=./calibration.yml -o is_simulated=true
```

The `sim` extra supplies `scqubits` and `qutip`. Without it, `-o is_simulated=true`
fails at startup saying so.

The `process` driver takes the same option, and that is the point — point both
at the same `quantify.device.yml` and the whole node runs with no instruments:

```bash
pip install 'qpi-driver[cli,quantify,sim]'

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
detuning, relaxation and dephasing, and a readout resonator each qubit level pulls
to its own frequency — so the IQ clouds are derived from a dispersive response and
an amplifier chain rather than placed, and reading away from resonance costs
contrast. The transmon has three rungs and is driven as a ladder, so leakage into
`|2>` is real and the EF chain has something to measure. A CZ is a flux pulse
walking the pair through the `|11>`-`|02>` avoided crossing, so the conditional
phase is integrated rather than asserted and a Bell state comes out entangled. Both
mechanisms the hardware has: DC flux on a qubit's own port, and a parametric drive
on a coupler, where the drive *frequency* is the resonance condition rather than a
carrier detail. A coupler is a mode of its own, tuning with the DC bias a routine
parks it at.

Not modelled: crosstalk. Each qubit's readout chain differs — its own resonance
and its own rotation — but the transmons themselves are one Hamiltonian repeated,
so it will never show you a chip whose *qubits* differ, which on real hardware is
most of what calibration is for.

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
resonator_spectroscopy → { time_of_flight, resonator_relaxation }
resonator_spectroscopy → resonator_punchout → qubit_spectroscopy → rabi

rabi → resonator_spectroscopy_excited
rabi → readout_operating_point → readout_discrimination → readout_fidelity
rabi → ramsey → drag → { allxy, fine_amplitude → { rb, allxy_check } }
rabi → { t1, t2_echo }

rabi → f12_spectroscopy → rabi_12 → resonator_spectroscopy_second_excited
{ rabi_12, readout_operating_point } → three_state_operating_point
three_state_operating_point → { ramsey_12 → drag_12,
                                fine_amplitude_12,
                                three_state_discrimination }

qubit_spectroscopy → flux_spectroscopy
rabi → coupler_anticrossing
rabi → cz_spectroscopy → cz_parametrization
{ rb, flux_spectroscopy } → cz_chevron → conditional_phase → interleaved_rb
```

The readout chain **straddles** the qubit chain rather than preceding it, which is
the one piece of the shape worth knowing: finding the resonator needs no qubit
control, but measuring its resonance with the qubit in `|1>` needs a calibrated π
pulse, so half the readout nodes sit below `rabi`.

**Readout.**

| Routine | Writes | Notes |
|---------|--------|-------|
| `time_of_flight` | `measure.acq_delay` | How long the signal takes back through the cables, read off a raw trace. |
| `resonator_spectroscopy` | `clock_freqs.readout` | Lorentzian fit over a readout sweep. |
| `resonator_relaxation` | — | The resonator linewidth, which nothing else measures. |
| `resonator_punchout` | `measure.pulse_amp`, `clock_freqs.readout` | Highest power still in the dressed regime — and the resonance *at* that power, since choosing a power moves it. |
| `resonator_spectroscopy_excited` | — | The dispersive shift: the resonance with the qubit in `\|1>`. |
| `readout_operating_point` | `measure_2state.frequency`, `measure_2state.pulse_amp` | Where to read for *discrimination*, which is not where the most signal comes back. Frequency and amplitude together, since the resonance moves with power. |
| `readout_discrimination` | `measure.acq_rotation`, `measure.acq_threshold` | The line every `meas_level=2` shot is assigned against. Fitted at the point above. |
| `readout_fidelity` | — | Benchmark. Assignment fidelity, so readout drift is visible to the drift check. |

**The qubit.**

| Routine | Writes | Notes |
|---------|--------|-------|
| `qubit_spectroscopy` | `clock_freqs.f01`, `spec.amplitude` | Two-tone, sweeping drive power alongside frequency rather than guessing it. |
| `rabi` | `rxy.amp180` | π amplitude is half the oscillation period. |
| `ramsey` | `clock_freqs.f01` | Fine frequency, plus T2*. |
| `t1`, `t2_echo` | — | Characterisation; nothing is tuned from them. |
| `drag` | `rxy.motzoi` (`rxy.beta` under qblox) | Zero crossing of X90-Y180 against Y90-X180. |
| `allxy` | — | 21-pair diagnostic against the ideal staircase. |
| `fine_amplitude` | `rxy.amp180` | Amplifies a small error over repeated π pulses. |
| `rb`, `interleaved_rb`, `allxy_check` | — | Benchmarks. Their fidelity is what the drift check reads. |

**The 1-2 transition.** A transmon is a ladder used as a qubit, and every gate leaks
a little population onto the third rung — where a two-state readout does not lose
those shots but *reports* them as one of the other two. This chain is the only route
to that number.

| Routine | Writes | Notes |
|---------|--------|-------|
| `f12_spectroscopy` | `clock_freqs.f12` | Prepares `\|1>` and sweeps the `.12` clock. Reports the anharmonicity. |
| `rabi_12` | `r12.ef_amp180` | The EF π pulse. Read out on the two-state chain, which is what lets it precede three-state readout. |
| `resonator_spectroscopy_second_excited` | — | Checks the pull goes as `chi(1-2n)` — that the ladder is evenly spaced, which three-state readout rests on. |
| `three_state_operating_point` | `measure_3state.frequency`, `measure_3state.pulse_amp` | Where all three levels separate. Ranked on the *closest* pair, since a classifier is only as good as the two states it confuses most. |
| `ramsey_12` | `clock_freqs.f12` | Refines f12 from megahertz to kilohertz, plus a 1-2 T2*. |
| `drag_12` | `r12.ef_motzoi` | Cancels the 0-1 excitation an EF pulse causes off-resonantly. |
| `fine_amplitude_12` | `r12.ef_amp180` | Refines the EF π the way `fine_amplitude` refines the 0-1 one. |
| `three_state_discrimination` | — | Leakage. Classifies by nearest centre, not by a line: three clouds do not have one. |

**Two qubits.**

| Routine | Writes | Notes |
|---------|--------|-------|
| `flux_spectroscopy` | — | Coupler frequency against flux. |
| `coupler_anticrossing` | `bias.parking_current` | Walks the coupler down through the qubit over a DC bias and parks it a stated fraction short of the crossing. Sets instrument state between acquisitions, so it runs its own measurement loop. |
| `cz_spectroscopy` | `clock_freqs.cz` | The frequency a parametric CZ has to be driven at — frequency decides whether the gate runs at all. |
| `cz_parametrization` | `cz.square_amp`, `cz.square_duration` | Its amplitude and duration, from the exchange rate against drive. |
| `cz_chevron` | `cz.square_amp`, `cz.square_duration` | The baseband counterpart: a DC-flux CZ is pushed onto the crossing by amplitude, so it needs the 2D sweep. |
| `conditional_phase` | `cz.parent_phase_correction`, `cz.child_phase_correction` | Brings the conditional phase to π. Two corrections, because each qubit's single-qubit phase is its own. |

Two-qubit routines target edges rather than qubits, and need `target_edges` and
a flux-tunable coupler. They are disabled in the shipped example config, because
on a chip without one they are pure failure noise. An edge whose two qubits are
not both in `target_qubits` is refused at startup: a chevron over an uncalibrated
qubit still fits a curve and still writes an amplitude, so the wrong answer here is
a calibrated-*looking* gate rather than an error.

### Checks

A routine may carry a cheap **check** beside the sweep that derives its parameter —
"is this still right?" rather than "what is it?". `recalibrate` runs the checks and
re-runs only what fails, and if a failing node's own dependency also fails the blame
moves up, because recalibrating a node whose input is wrong measures the wrong thing
twice (Kelly et al., [arXiv:1803.03226](https://arxiv.org/abs/1803.03226)).

| Check | What it asks |
|-------|--------------|
| `resonator_spectroscopy` | Three points across the line: how far the configured frequency sits from the peak. |
| `resonator_punchout` | Two short scans, at the configured power and half of it. Dressed, the line does not move; punched through, it walks. |
| `rabi` | Five π pulses, so a small `amp180` error grows past what one π pulse can show. |
| `readout_discrimination` | The same experiment at a few hundred shots — the one case where a cheaper version answers the question, since counting misassignments needs far fewer shots than placing the line. |

A routine with **no** check is unknown rather than stale, and cannot be blamed —
otherwise every diagnosis would walk to the root and a partial recalibration would
cost more than a full one.

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

A partial run is narrower in two ways. It targets only the affected qubits, the
edges touching them, and — because an edge is refused unless both its ends are
targeted — those edges' far qubits. And it runs only what the checks blame:
`diagnose` asks each routine whether its parameter still holds, takes the
shallowest node with evidence against it, and re-runs that node and everything
downstream of it. Downstream because recalibrating a frequency invalidates the
gates tuned against it.

With no checks it falls back to seeding at `qubit_spectroscopy`, which is what
RFC 0004 did unconditionally: the readout chain is skipped because finding the
resonator is a bring-up step rather than a drift one, and it is most of what makes
a full calibration long. The difference now is that a readout drift is not
*invisible* — `resonator_spectroscopy` has a check, so if the readout has moved the
blame walks up into it and the report says so. A run where every check passes
recalibrates nothing, and says that too.

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
- **The `process` driver beside it picks the new file up on its next job** — no
  restart, no signal; the file is the whole channel. A restored `.prev`, or
  parameters worked out by hand, arrive the same way.
- The tuner re-reads it at the start of each calibration, so a chip something else
  moved is not calibrated from stale values and then overwritten with them.
- Beside it, `quantify.device.provenance.yml` records **which routine last measured
  each parameter, and when** (RFC 0008). It holds no values, nothing that runs a
  circuit reads it, and it is safe to delete — every parameter then simply reads as
  never-measured, which is what a fresh chip has.

That last file is what lets a report tell a measurement from a number somebody typed
in. A device config cannot: `clock_freqs.f01: 4735509751.238763` has nine significant
figures whether it was fitted or copied from a design document, and on the August 2026
chip it was the latter, 302 MHz from the qubit. A calibration now says outright which of
its inputs nothing had ever measured — in the report's notes, per target, and on each
routine result — and, when a node is skipped, how old the values it left standing are.

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

`make test-py-loop` runs whole calibrations against that simulator, under both
schedulers: the full graph, a partial recalibration, a drift check and the
recalibration it queues, and the write-back. Faults came out of it that no test of a
single routine could see — a benchmark fit that reported the same fidelity for every
chip better than about 3% error per Clifford, a partial recalibration that quietly
ran the full graph, and a parametric CZ that had never once worked because a held
flux offset dropped its drive frequency.

Two gaps remain, and they are the ones that matter:

- The two-qubit model is weaker than the one-qubit one, and the qubits do not
  differ from each other. A routine can be wrong in a way this simulator agrees
  with.
- **Nothing here has been run against physical hardware.** The graph is wired for
  it — a real bias rack, and nodes that decline rather than fail on an element that
  cannot hold their result — but wired for it is not verified against it. The manual
  verification in RFC 0004 §9 is what remains before this is trustworthy in a lab.
